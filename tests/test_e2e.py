"""End-to-end tests: real intake, merger and query processes over HTTP.

Everything here uses ephemeral ports and a temporary data directory, so it is
safe to run beside a development server.  Run: python3 -m unittest discover tests
"""

from __future__ import annotations

import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call(url: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else {}


class ServiceCluster:
    def __init__(self) -> None:
        self.data_dir = tempfile.mkdtemp(prefix="ledger-test-")
        self.ports = {"intake": free_port(), "merger": free_port(), "query": free_port()}
        self.procs: list[subprocess.Popen] = []
        env = dict(os.environ)
        env["INTAKE_URL"] = f"http://127.0.0.1:{self.ports['intake']}"
        env["MERGER_URL"] = f"http://127.0.0.1:{self.ports['merger']}"
        env["LEDGER_DATA_DIR"] = self.data_dir
        self.env = env

    @property
    def intake(self) -> str:
        return f"http://127.0.0.1:{self.ports['intake']}"

    @property
    def merger(self) -> str:
        return f"http://127.0.0.1:{self.ports['merger']}"

    @property
    def query(self) -> str:
        return f"http://127.0.0.1:{self.ports['query']}"

    def start(self) -> None:
        for name, extra in (
            ("app.intake", []),
            ("app.merger", []),
            ("app.query", ["--web-dir", os.path.join(ROOT, "web")]),
        ):
            proc = subprocess.Popen(
                [sys.executable, "-E", "-s", "-m", name, "--port", str(self.ports[name.split(".")[1]]), "--data-dir", self.data_dir, *extra],
                cwd=ROOT,
                env=self.env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.procs.append(proc)
        for port in self.ports.values():
            self._wait(port)

    def _wait(self, port: int, timeout: float = 10) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status, _ = call(f"http://127.0.0.1:{port}/health")
                if status == 200:
                    return
            except (urllib.error.URLError, ConnectionError):
                pass
            time.sleep(0.05)
        raise RuntimeError(f"service on {port} did not start")

    def stop(self) -> None:
        for proc in self.procs:
            proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cluster = ServiceCluster()
        cls.cluster.start()
        _, body = call(cls.cluster.intake + "/api/sources", "POST", {"name": "e2e", "description": "test"})
        cls.source_id = body["source"]["id"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.stop()

    # -- helpers -------------------------------------------------------
    def observe(self, client_id: str, runs: list[dict]) -> dict:
        status, body = call(
            f"{self.cluster.intake}/api/sources/{self.source_id}/observations",
            "POST",
            {"client_id": client_id, "runs": runs},
        )
        self.assertEqual(status, 201, body)
        return body["entry"]

    def revise(self, client_id: str, start: int, end: int, value: int, *, base_seq: int | None = None) -> dict:
        payload = {"client_id": client_id, "start": start, "end": end, "value": value}
        if base_seq is not None:
            payload["base_seq"] = base_seq
        status, body = call(
            f"{self.cluster.intake}/api/sources/{self.source_id}/revisions", "POST", payload
        )
        self.assertEqual(status, 201, body)
        return body["entry"]

    def aggregate(self, lo: int, hi: int, granularity: int = 1, **extra) -> dict:
        params = {"lo": lo, "hi": hi, "granularity": granularity, **extra}
        status, body = call(
            f"{self.cluster.query}/api/sources/{self.source_id}/aggregate?{urlencode(params)}"
        )
        self.assertEqual(status, 200, body)
        return body

    # -- tests ---------------------------------------------------------
    def test_1_ingest_and_pointwise_baseline(self):
        head = 0
        head = self.observe("e2e-obs-1", [{"start": 0, "end": 30, "value": 4}])["seq"]
        head = self.observe("e2e-obs-2", [{"start": 30, "end": 45, "value": 4}])["seq"]
        self.observe("e2e-obs-3", [{"start": 70, "end": 100, "value": 2}])
        result = self.aggregate(0, 100, 1)
        self.assertTrue(result["match"])
        self.assertEqual(result["compressed"]["total"], 4 * 45 + 2 * 30)
        verify = call(
            f"{self.cluster.query}/api/sources/{self.source_id}/verify?lo=0&hi=100&granularity=4"
        )[1]
        self.assertTrue(verify["ok"], verify["mismatches"])
        # gap [45,70) must stay uncovered, never a zero reading
        self.assertEqual(result["compressed"]["covered_ticks"], 75)

    def test_2_partial_revision_keeps_residues(self):
        before = self.aggregate(0, 100, 1)
        entry = self.revise("e2e-rev-mid", 12, 18, 9)
        after = self.aggregate(0, 100, 1)
        expected_delta = (9 - 4) * 6
        self.assertEqual(
            after["compressed"]["total"] - before["compressed"]["total"], expected_delta
        )
        segments = after["segments"]
        self.assertTrue(
            any(s["start"] == 0 and s["end"] == 12 and s["value"] == 4 for s in segments)
        )
        self.assertTrue(
            any(s["start"] == 18 and s["end"] == 30 and s["value"] == 4 for s in segments)
        )
        audit = call(f"{self.cluster.merger}/api/sources/{self.source_id}/audit?seq={entry['seq']}")[1]
        self.assertEqual([(r["start"], r["end"]) for r in audit["residues"]["left"]], [(0, 12)])
        self.assertEqual([(r["start"], r["end"]) for r in audit["residues"]["right"]], [(18, 30)])

    def test_3_revision_spanning_many_old_runs_counts_once(self):
        base = self.aggregate(0, 100, 1)["compressed"]["total"]
        # [10, 60) crosses several observation and revision boundaries.  The
        # old value there is read from the API, not guessed from test order.
        segments = self.aggregate(10, 60, 1)["segments"]
        old = sum(s["ticks"] * s["value"] for s in segments)
        self.revise("e2e-rev-wide", 10, 60, 5)
        after = self.aggregate(0, 100, 1)
        self.assertEqual(after["compressed"]["total"] - base, 5 * 50 - old)
        verify = call(
            f"{self.cluster.query}/api/sources/{self.source_id}/verify?lo=5&hi=65&granularity=3"
        )[1]
        self.assertTrue(verify["ok"])

    def test_4_preview_reports_split_basis_and_stale_head(self):
        state = call(
            f"{self.cluster.merger}/api/sources/{self.source_id}/state?lo=0&hi=100&granularity=1"
        )[1]
        head = state["head_seq"]
        status, preview = call(
            f"{self.cluster.merger}/api/sources/{self.source_id}/preview",
            "POST",
            {"start": 20, "end": 40, "value": 7, "base_seq": head},
        )
        self.assertEqual(status, 200, preview)
        self.assertGreater(len(preview["affected"]), 0)
        self.assertEqual(preview["span_ticks"], 20)
        self.assertEqual(preview["new_integral"], 140)
        self.assertLessEqual(preview["coverage_before"], preview["span_ticks"])
        self.assertGreaterEqual(preview["coverage_before"], 0)
        # A write advances the head; previewing against the old head is stale.
        self.revise("e2e-rev-stale", 55, 60, 1)
        status, stale = call(
            f"{self.cluster.merger}/api/sources/{self.source_id}/preview",
            "POST",
            {"start": 20, "end": 40, "value": 7, "base_seq": head},
        )
        self.assertEqual(status, 409, stale)
        self.assertEqual(stale["error"]["code"], "stale_preview")

    def test_5_idempotent_retry_does_not_double_count(self):
        before = self.aggregate(0, 100, 1)["compressed"]["total"]
        prior = sum(s["ticks"] * s["value"] for s in self.aggregate(80, 90, 1)["segments"])
        first = self.revise("e2e-idem", 80, 90, 8)
        status, again = call(
            f"{self.cluster.intake}/api/sources/{self.source_id}/revisions",
            "POST",
            {"client_id": "e2e-idem", "start": 80, "end": 90, "value": 8},
        )
        self.assertEqual(status, 200)
        self.assertFalse(again["created"])
        self.assertEqual(again["entry"]["seq"], first["seq"])
        after = self.aggregate(0, 100, 1)["compressed"]["total"]
        self.assertEqual(after - before, 8 * 10 - prior)

    def test_6_draft_survives_and_clears_on_commit(self):
        draft = {"start": 30, "end": 50, "value": 3, "note": "unsaved", "base_seq": 0}
        call(f"{self.cluster.merger}/api/sources/{self.source_id}/draft", "PUT", draft)
        fetched = call(f"{self.cluster.merger}/api/sources/{self.source_id}/draft")[1]["draft"]
        self.assertEqual((fetched["start"], fetched["end"], fetched["value"]), (30, 50, 3))
        status, committed = call(
            f"{self.cluster.merger}/api/sources/{self.source_id}/commit",
            "POST",
            {"start": 30, "end": 50, "value": 3, "client_id": "e2e-commit-1"},
        )
        self.assertEqual(status, 200, committed)
        self.assertTrue(committed["draft_cleared"])
        self.assertEqual(
            call(f"{self.cluster.merger}/api/sources/{self.source_id}/draft")[0], 404
        )

    def test_7_revoke_restores_exact_prior_residues(self):
        before = self.aggregate(0, 100, 1)["compressed"]["total"]
        entry = self.revise("e2e-revoke-target", 24, 28, 99)
        with_rev = self.aggregate(0, 100, 1)["compressed"]["total"]
        self.assertNotEqual(with_rev, before)
        status, body = call(
            f"{self.cluster.merger}/api/sources/{self.source_id}/revoke?seq={entry['seq']}",
            "POST",
            {"client_id": "e2e-revocation", "target_seq": entry["seq"]},
        )
        self.assertEqual(status, 200, body)
        restored = self.aggregate(0, 100, 1)["compressed"]["total"]
        self.assertEqual(restored, before)
        verify = call(
            f"{self.cluster.query}/api/sources/{self.source_id}/verify?lo=0&hi=100&granularity=1"
        )[1]
        self.assertTrue(verify["ok"])

    def test_8_invalid_inputs_are_rejected(self):
        cases = [
            ({"runs": [{"start": 5, "end": 5, "value": 1}]}, "invalid_runs"),
            ({"runs": [{"start": 0, "end": 10, "value": 1}, {"start": 5, "end": 12, "value": 2}]}, "runs_overlap"),
            ({"runs": [{"start": 0, "end": 10, "value": 1}, {"start": 10, "end": 12, "value": 1}]}, "runs_not_coalesced"),
            ({"runs": [{"start": 0, "end": 10, "value": -2}]}, "invalid_field"),
        ]
        for payload, code in cases:
            status, body = call(
                f"{self.cluster.intake}/api/sources/{self.source_id}/observations",
                "POST",
                {"client_id": f"bad-{code}", **payload},
            )
            self.assertEqual(status, 400, body)
            self.assertEqual(body["error"]["code"], code)
        status, body = call(
            f"{self.cluster.intake}/api/sources/{self.source_id}/revisions",
            "POST",
            {"client_id": "bad-span", "start": 9, "end": 9, "value": 1},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_span")

    def test_9_show_revoked_replays_the_revision(self):
        """`include_revoked` must replay what the revocation took away."""
        entry = self.revise("e2e-show-revoked", 61, 66, 77)
        window_before = call(
            f"{self.cluster.merger}/api/sources/{self.source_id}/state?lo=60&hi=70&granularity=1"
        )[1]
        call(
            f"{self.cluster.merger}/api/sources/{self.source_id}/revoke?seq={entry['seq']}",
            "POST",
            {"client_id": "e2e-show-revoked-revoke"},
        )
        hidden = call(
            f"{self.cluster.merger}/api/sources/{self.source_id}/state?lo=60&hi=70&granularity=1"
        )[1]
        shown = call(
            f"{self.cluster.merger}/api/sources/{self.source_id}/state?lo=60&hi=70&granularity=1&include_revoked=1"
        )[1]
        self.assertIn(entry["seq"], hidden["revoked_revisions"])
        self.assertEqual(hidden["revoked_revisions"], shown["revoked_revisions"])
        self.assertNotEqual(hidden["segments"], shown["segments"])
        # With the revoked revision replayed, the window equals its pre-revoke
        # reading (nothing else wrote in [60,70) in between).
        self.assertEqual(sum(shown["totals"]), sum(window_before["totals"]))
        self.assertEqual(
            sum(s["ticks"] * s["value"] for s in shown["segments"] if 61 <= s["start"] < 66),
            77 * 5,
        )

    def test_10_gap_fill_is_explicit_in_the_preview(self):
        # [45,70) was never observed; a revision there fills it and says so.
        state = call(
            f"{self.cluster.merger}/api/sources/{self.source_id}/state?lo=40&hi=75&granularity=1"
        )[1]
        preview = call(
            f"{self.cluster.merger}/api/sources/{self.source_id}/preview",
            "POST",
            {"start": 45, "end": 70, "value": 2, "base_seq": state["head_seq"]},
        )[1]
        self.assertEqual(preview["coverage_before"], 0)
        self.assertTrue(preview["would_cover_gaps"])
        self.assertEqual(preview["new_integral"], 2 * 25)

    def test_11_query_and_merger_disagree_never_silently(self):
        """Both services replay the same ledger; their totals must agree."""
        query_total = self.aggregate(0, 100, 5)["compressed"]["total"]
        merger = call(
            f"{self.cluster.merger}/api/sources/{self.source_id}/state?lo=0&hi=100&granularity=5&expand=1"
        )[1]
        self.assertEqual(query_total, sum(merger["totals"]))
        self.assertTrue(merger["matches_expanded"])


class ScaleTests(unittest.TestCase):
    """The compressed path must not degrade with window width."""

    def test_compressed_cost_is_window_independent(self):
        cluster = ServiceCluster()
        cluster.start()
        try:
            _, body = call(cluster.intake + "/api/sources", "POST", {"name": "scale"})
            sid = body["source"]["id"]
            # 40k ticks of alternating runs, sent in batches.
            rng = random.Random(11)
            cursor = 0
            seq = 0
            while cursor < 40_000:
                seq += 1
                length = rng.choice([1, 5, 50, 500])
                call(
                    f"{cluster.intake}/api/sources/{sid}/observations",
                    "POST",
                    {
                        "client_id": f"scale-{seq}",
                        "runs": [{"start": cursor, "end": min(cursor + length, 40_000), "value": seq % 5}],
                    },
                )
                cursor += length
            small = call(
                f"{cluster.query}/api/sources/{sid}/aggregate?lo=100&hi=200&granularity=1&method=both"
            )[1]
            large = call(
                f"{cluster.query}/api/sources/{sid}/aggregate?lo=0&hi=40000&granularity=1&method=both"
            )[1]
            self.assertEqual(small["expanded"]["cost"]["segments_scanned"], 100)
            self.assertEqual(large["expanded"]["cost"]["segments_scanned"], 40_000)
            self.assertLess(
                large["compressed"]["cost"]["segments_scanned"],
                large["expanded"]["cost"]["segments_scanned"],
            )
            self.assertTrue(large["match"])
        finally:
            cluster.stop()


if __name__ == "__main__":
    unittest.main()
