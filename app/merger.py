"""Merger service: independent replay, previews, drafts and commit.

Port 8302 by default.  It never writes observation data; revisions and
revocations are committed through the intake service, then the local replay
cache is invalidated.  Drafts live in this service so a half-finished merge
survives a browser restart.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import intervals  # noqa: E402
from app.clients import IntakeClient  # noqa: E402
from app.common import AppError, BaseHandler, Router, parse_args, serve  # noqa: E402
from app.merge import (  # noqa: E402
    Segment,
    apply_entry,
    diff_state,
    entry_span,
    integral,
    replay,
    residue_view,
    state_stats,
)

EXPAND_CAP_TICKS = 50_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    source_id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ReplayCache:
    """Small TTL cache of ledger entries and replayed states per source."""

    def __init__(self, intake: IntakeClient):
        self.intake = intake
        self._lock = threading.Lock()
        self._entries: dict[int, tuple[float, int, list[dict], dict[int, int]]] = {}
        self._states: dict[tuple[int, int, tuple[int, ...]], list[Segment]] = {}

    def invalidate(self, source_id: int) -> None:
        with self._lock:
            self._entries.pop(source_id, None)
            for key in [k for k in self._states if k[0] == source_id]:
                self._states.pop(key, None)

    def ledger(self, source_id: int, *, force: bool = False) -> tuple[list[dict], dict[int, int], int]:
        """Entries plus revocations, validated against the intake head.

        Head equality, not a TTL, decides freshness: a preview must never be
        based on a ledger the intake service has already moved past.
        """
        now = time.monotonic()
        with self._lock:
            cached = self._entries.get(source_id)
            if cached and not force and self.intake.head_seq(source_id) == cached[1]:
                return cached[2], cached[3], cached[1]
        entries = self.intake.all_entries(source_id)
        revoked = {
            int(e["seq"]): int(e["revoked_by"])
            for e in entries
            if e["kind"] == "revision" and e.get("revoked_by") is not None
        }
        head = int(entries[-1]["seq"]) if entries else 0
        with self._lock:
            self._entries[source_id] = (now, head, entries, revoked)
        return entries, revoked, head

    def state(
        self,
        source_id: int,
        *,
        include_revoked: bool = False,
        override: list[dict] | None = None,
        force: bool = False,
    ) -> tuple[list[Segment], list[dict], dict[int, int], int]:
        entries, revoked, head = self.ledger(source_id, force=force)
        if override is not None:
            entries = override
            revoked = {
                int(e["seq"]): int(e["revoked_by"])
                for e in entries
                if e["kind"] == "revision" and e.get("revoked_by") is not None
            }
        skipped = set() if include_revoked else set(revoked)
        key = (source_id, head, tuple(sorted(skipped)))
        if override is None:
            with self._lock:
                cached = self._states.get(key)
            if cached is not None and not force:
                return list(cached), entries, revoked, head
        state = replay(entries, skipped)
        if override is None:
            with self._lock:
                self._states[key] = list(state)
        return state, entries, revoked, head


class DraftStore:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "merger.sqlite3")
        with self._connect() as con:
            con.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        return con

    def get(self, source_id: int) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT payload FROM drafts WHERE source_id = ?", (source_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def put(self, source_id: int, payload: dict) -> dict:
        payload = dict(payload)
        payload["updated_at"] = now_iso()
        with self._connect() as con:
            con.execute(
                "INSERT INTO drafts(source_id, payload, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(source_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                (source_id, json.dumps(payload), payload["updated_at"]),
            )
        return payload

    def delete(self, source_id: int) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM drafts WHERE source_id = ?", (source_id,))


INTAKE: IntakeClient
CACHE: ReplayCache
DRAFTS: DraftStore


def _seg_json(seg: Segment) -> dict:
    return seg.as_dict()


def _parse_span(body: dict, *, require_value: bool = True) -> tuple[int, int, int | None]:
    start, end = body.get("start"), body.get("end")
    for name, value in (("start", start), ("end", end)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise AppError(400, "invalid_span", f"'{name}' must be an integer tick")
    if not (0 <= start < end <= 10_000_000):
        raise AppError(400, "invalid_span", "Require 0 <= start < end <= 10000000")
    value = body.get("value")
    if value is None and require_value:
        raise AppError(400, "invalid_value", "'value' is required")
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not (0 <= value <= 999_999)):
        raise AppError(400, "invalid_value", "value must be an integer between 0 and 999999")
    return start, end, value


def _describe_span(state: list[Segment], start: int, end: int) -> dict:
    residues = residue_view(state, start, end)
    before_integral = integral(state, start, end)
    # Coverage is measured on the clipped pieces inside the span; a run that
    # merely touches the span must not contribute its full length.
    covered = sum(c.ticks for s in state for c in [s.clip(start, end)] if c)
    return {
        "residues": {
            "left": [_seg_json(s) for s in residues["left"]],
            "right": [_seg_json(s) for s in residues["right"]],
        },
        "replaced": [_seg_json(s) for s in residues["replaced"]],
        "old_integral": before_integral,
        "span_ticks": end - start,
        "coverage_before": covered,
    }


def _bucket_compare(
    state: list[Segment],
    lo: int,
    hi: int,
    granularity: int,
    *,
    expand: bool = False,
) -> dict:
    """Aggregate the window compressed, and optionally time the expanded path.

    The expanded path is bounded: a caller asking for a month at one-tick
    granularity must not silently materialise a million rows on the read path.
    Over the cap we report ``skipped`` rather than pretending the comparison ran.
    """
    segments = [
        intervals.Run(s.start, s.end, s.value)
        for s in state
        if s.start < hi and s.end > lo
    ]
    if hi - lo > EXPAND_CAP_TICKS:
        raise AppError(
            400,
            "window_too_large",
            f"Expanded comparison is capped at {EXPAND_CAP_TICKS} ticks; use a narrower window",
            {"ticks": hi - lo, "cap": EXPAND_CAP_TICKS},
        )
    t0 = time.perf_counter()
    buckets, scanned = intervals.bucket_totals_compressed(segments, lo, hi, granularity)
    compressed_ms = (time.perf_counter() - t0) * 1000
    result: dict[str, Any] = {
        "granularity": granularity,
        "buckets": buckets,
        "totals": [b["total"] for b in buckets],
        "method": "compressed",
        "cost": {
            "segments_scanned": scanned,
            "ticks_expanded": 0,
            "compressed_ms": round(compressed_ms, 4),
        },
    }
    if expand:
        points = list(intervals.iter_points(segments, lo, hi))
        t1 = time.perf_counter()
        expanded, expanded_scanned = intervals.bucket_totals_points(points, lo, hi, granularity)
        expanded_ms = (time.perf_counter() - t1) * 1000
        result["cost"].update(
            {
                "ticks_expanded": expanded_scanned,
                "expanded_ms": round(expanded_ms, 4),
            }
        )
        result["expanded_totals"] = [b["total"] for b in expanded]
        result["matches_expanded"] = result["totals"] == result["expanded_totals"]
    return result


def _verify_pointwise(
    state: list[Segment], lo: int, hi: int, granularity: int
) -> dict:
    """Small-window truth: expand every tick and re-bucket; compare."""

    segments = [intervals.Run(s.start, s.end, s.value) for s in state if s.start < hi and s.end > lo]
    compressed, _ = intervals.bucket_totals_compressed(segments, lo, hi, granularity)
    points = list(intervals.iter_points(segments, lo, hi))
    from collections import defaultdict

    point_totals: dict[int, int] = defaultdict(int)
    point_covered: dict[int, int] = defaultdict(int)
    for tick, value in points:
        b = (tick // granularity) * granularity
        point_totals[b] += value
        point_covered[b] += 1
    mismatches = []
    for bucket in compressed:
        key = (bucket["start"] // granularity) * granularity
        if point_totals.get(key, 0) != bucket["total"]:
            mismatches.append(
                {
                    "start": bucket["start"],
                    "end": bucket["end"],
                    "compressed": bucket["total"],
                    "pointwise": point_totals.get(key, 0),
                }
            )
    # Also enforce half-open point lookups at both edges.
    edge_checks = []
    if points:
        ticks = {t for t, _ in points}
        for tick in ticks:
            if tick in (lo, hi - 1, lo + 1):
                pass
        first_tick = lo
        val = intervals.value_at(segments, first_tick)
        edge_checks.append(
            {
                "tick": first_tick,
                "value": val,
                "expected_present": any(s.start <= first_tick < s.end for s in segments),
            }
        )
        last_tick = hi - 1
        val = intervals.value_at(segments, last_tick)
        edge_checks.append(
            {
                "tick": last_tick,
                "value": val,
                "expected_present": any(s.start <= last_tick < s.end for s in segments),
            }
        )
    return {
        "lo": lo,
        "hi": hi,
        "granularity": granularity,
        "ticks": len(points),
        "buckets": len(compressed),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],
        "edge_checks": edge_checks,
        "ok": not mismatches,
    }


class MergeHandler(BaseHandler):
    service_name = "merger"
    routes = Router()

    def _source_id(self, key: str = "source_id") -> int:
        try:
            return int(self.params[key])
        except ValueError:
            raise AppError(400, "invalid_source", "Source id must be an integer")

    def health(self) -> dict:
        upstream = "ok"
        try:
            INTAKE.health()
        except AppError:
            upstream = "unreachable"
        return {"ok": True, "service": "merger", "intake": upstream, "time": now_iso()}

    def overview(self) -> dict:
        sources = INTAKE.list_sources()
        out = []
        for src in sources:
            try:
                state, entries, revoked, head = CACHE.state(src["id"])
                stats = state_stats(state)
                out.append({**src, **stats, "revoked_revisions": len(revoked), "head_seq": head})
            except AppError as exc:
                out.append({**src, "error": exc.message})
        return {"sources": out}

    def state(self) -> dict:
        source_id = self._source_id()
        lo = self.q_int("lo", required=True)
        hi = self.q_int("hi", required=True)
        granularity = self.q_int("granularity", 5) or 5
        assert lo is not None and hi is not None
        if not (0 <= lo < hi):
            raise AppError(400, "invalid_span", "Require 0 <= lo < hi")
        if granularity < 1 or granularity > 1440:
            raise AppError(400, "invalid_granularity", "granularity must be 1..1440 ticks")
        include_revoked = (self.q("include_revoked") or "") in {"1", "true", "yes"}
        expand = (self.q("expand") or "") in {"1", "true", "yes"}
        state, entries, revoked, head = CACHE.state(source_id, include_revoked=include_revoked)
        visible = [s for s in state if s.start < hi and s.end > lo]
        buckets = _bucket_compare(state, lo, hi, granularity, expand=expand)
        return {
            "source_id": source_id,
            "head_seq": head,
            "include_revoked": include_revoked,
            "revoked_revisions": sorted(revoked),
            "extent": state_stats(state),
            "window": {"lo": lo, "hi": hi, "granularity": granularity},
            "segments": [_seg_json(c) for s in visible if (c := s.clip(lo, hi))],
            "buckets": buckets["buckets"],
            "totals": buckets["totals"],
            "method": "compressed",
            "cost": buckets["cost"],
            "expanded_totals": buckets.get("expanded_totals"),
            "matches_expanded": buckets.get("matches_expanded"),
            "entries": [
                {
                    "seq": e["seq"],
                    "kind": e["kind"],
                    "active": e.get("active"),
                    "revoked_by": e.get("revoked_by"),
                    "start": e.get("start"),
                    "end": e.get("end"),
                    "value": e.get("value"),
                    "runs": e.get("runs"),
                    "note": e.get("note"),
                    "created_at": e.get("created_at"),
                }
                for e in entries
            ],
        }

    def verify(self) -> dict:
        source_id = self._source_id()
        lo = self.q_int("lo", required=True)
        hi = self.q_int("hi", required=True)
        granularity = self.q_int("granularity", 1) or 1
        assert lo is not None and hi is not None
        if not (0 <= lo < hi):
            raise AppError(400, "invalid_span", "Require 0 <= lo < hi")
        if hi - lo > 20_000:
            raise AppError(400, "window_too_large", "Pointwise verification is capped at 20,000 ticks")
        if granularity < 1:
            raise AppError(400, "invalid_granularity", "granularity must be positive")
        state, entries, revoked, head = CACHE.state(source_id)
        result = _verify_pointwise(state, lo, hi, granularity)
        result["head_seq"] = head
        result["segments"] = [
            _seg_json(s) for s in state if s.start < hi and s.end > lo
        ]
        return result

    def preview(self) -> dict:
        source_id = self._source_id()
        body = self.body_dict()
        start, end, value = _parse_span(body)
        assert value is not None
        base_seq = body.get("base_seq")
        state, entries, revoked, head = CACHE.state(source_id, force=True)
        if base_seq is not None:
            if isinstance(base_seq, bool) or not isinstance(base_seq, int):
                raise AppError(400, "invalid_base_seq", "base_seq must be an integer")
            if base_seq != head:
                raise AppError(
                    409,
                    "stale_preview",
                    "Ledger changed since the editor loaded; refresh before previewing",
                    {"head_seq": head, "base_seq": base_seq},
                )
        synthetic = {
            "id": head + 1,
            "kind": "revision",
            "start": start,
            "end": end,
            "value": value,
        }
        after = apply_entry(state, synthetic)
        diff = diff_state(state, after, start, end)
        detail = _describe_span(state, start, end)
        affected = []
        for seg in state:
            if seg.end > start and seg.start < end:
                affected.append(_seg_json(seg))
        return {
            "source_id": source_id,
            "head_seq": head,
            "proposed": {"start": start, "end": end, "value": value, "seq": head + 1},
            "affected": affected,
            "residues": detail["residues"],
            "replaced": detail["replaced"],
            "old_integral": detail["old_integral"],
            "new_integral": value * (end - start),
            "span_ticks": end - start,
            "coverage_before": detail["coverage_before"],
            "diff": diff,
            "after_segments": [_seg_json(s) for s in after if s.start < end and s.end > start],
            "invalidates_preview": bool(diff["changed"]),
            "would_cover_gaps": (end - start) > detail["coverage_before"],
        }

    def audit_entry(self) -> dict:
        source_id = self._source_id()
        seq = self.q_int("seq", required=True)
        assert seq is not None
        state, entries, revoked, head = CACHE.state(source_id, include_revoked=True)
        target = next((e for e in entries if int(e["seq"]) == seq), None)
        if target is None:
            raise AppError(404, "entry_not_found", f"Entry {seq} not found")
        span = entry_span(target)
        if span is None:
            raise AppError(400, "no_span", "Entry has no span")
        lo, hi = span
        before_state = []
        for entry in sorted(entries, key=lambda e: int(e["seq"])):
            if int(entry["seq"]) >= seq:
                break
            if entry["kind"] == "revision" and int(entry["seq"]) in revoked:
                continue
            if entry["kind"] == "revoke":
                continue
            before_state = apply_entry(before_state, entry)
        after_state = before_state
        if target["kind"] != "revoke" and int(target["seq"]) not in revoked:
            after_state = apply_entry(before_state, target)
        diff = diff_state(before_state, after_state, lo, hi)
        detail = _describe_span(before_state, lo, hi)
        return {
            "source_id": source_id,
            "entry": {
                "seq": target["seq"],
                "kind": target["kind"],
                "start": target.get("start"),
                "end": target.get("end"),
                "value": target.get("value"),
                "note": target.get("note"),
                "created_at": target.get("created_at"),
                "active": target.get("active"),
                "revoked_by": target.get("revoked_by"),
            },
            "head_seq": head,
            "span": {"lo": lo, "hi": hi},
            "residues": detail["residues"],
            "replaced": detail["replaced"],
            "diff": diff,
            "revoked": int(target["seq"]) in revoked,
        }

    def get_draft(self) -> dict:
        source_id = self._source_id()
        draft = DRAFTS.get(source_id)
        if draft is None:
            raise AppError(404, "no_draft", "No saved draft for this source")
        return {"draft": draft}

    def put_draft(self) -> dict:
        source_id = self._source_id()
        body = self.body_dict()
        start, end, value = _parse_span(body, require_value=False)
        payload = {
            "start": start,
            "end": end,
            "value": value,
            "note": str(body.get("note") or "")[:500],
            "base_seq": body.get("base_seq"),
            "selected": body.get("selected"),
            "view": body.get("view"),
        }
        return {"draft": DRAFTS.put(source_id, payload), "persisted": True}

    def delete_draft(self) -> dict:
        source_id = self._source_id()
        DRAFTS.delete(source_id)
        return {"deleted": True}

    def commit(self) -> dict:
        source_id = self._source_id()
        body = self.body_dict()
        start, end, value = _parse_span(body)
        assert value is not None
        base_seq = body.get("base_seq")
        client_id = body.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise AppError(400, "invalid_client_id", "client_id is required for idempotent commit")
        note = str(body.get("note") or "")[:500]
        payload = {
            "start": start,
            "end": end,
            "value": value,
            "client_id": client_id,
            "note": note,
        }
        if base_seq is not None:
            payload["base_seq"] = base_seq
        entry, created = INTAKE.create_revision(source_id, payload)
        CACHE.invalidate(source_id)
        # A committed draft is done; a stale one would only mislead.
        DRAFTS.delete(source_id)
        state, _, _, head = CACHE.state(source_id, force=True)
        return {
            "entry": entry,
            "created": created,
            "head_seq": head,
            "segments": [_seg_json(s) for s in state if s.start < end and s.end > start],
            "draft_cleared": True,
        }

    def revoke(self) -> dict:
        source_id = self._source_id()
        seq = self.q_int("seq", required=True)
        assert seq is not None
        body = self.body_dict()
        client_id = body.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise AppError(400, "invalid_client_id", "client_id is required")
        payload = {
            "target_seq": seq,
            "client_id": client_id,
            "note": str(body.get("note") or "")[:500],
        }
        if body.get("base_seq") is not None:
            payload["base_seq"] = body["base_seq"]
        entry, created = INTAKE.revoke(source_id, payload)
        CACHE.invalidate(source_id)
        state, _, _, head = CACHE.state(source_id, force=True)
        return {
            "entry": entry,
            "created": created,
            "head_seq": head,
            "segments": [_seg_json(s) for s in state],
            "stats": state_stats(state),
        }


router = MergeHandler.routes
router.add("GET", r"/health", "health")
router.add("GET", r"/api/overview", "overview")
router.add("GET", r"/api/sources/(?P<source_id>\d+)/state", "state")
router.add("GET", r"/api/sources/(?P<source_id>\d+)/verify", "verify")
router.add("GET", r"/api/sources/(?P<source_id>\d+)/audit", "audit_entry")
router.add("POST", r"/api/sources/(?P<source_id>\d+)/preview", "preview")
router.add("GET", r"/api/sources/(?P<source_id>\d+)/draft", "get_draft")
router.add("PUT", r"/api/sources/(?P<source_id>\d+)/draft", "put_draft")
router.add("DELETE", r"/api/sources/(?P<source_id>\d+)/draft", "delete_draft")
router.add("POST", r"/api/sources/(?P<source_id>\d+)/commit", "commit")
router.add("POST", r"/api/sources/(?P<source_id>\d+)/revoke", "revoke")


def main() -> None:
    global INTAKE, CACHE, DRAFTS
    args = parse_args("Interval ledger merger service")
    INTAKE = IntakeClient(os.environ.get("INTAKE_URL"))
    CACHE = ReplayCache(INTAKE)
    DRAFTS = DraftStore(args.data_dir)
    serve("merger", MergeHandler, args.port)


if __name__ == "__main__":
    main()
