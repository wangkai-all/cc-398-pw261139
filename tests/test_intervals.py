"""Interval algebra and merge-engine tests.  Run: python3 -m unittest discover tests"""

from __future__ import annotations

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import intervals as it  # noqa: E402
from app.merge import (  # noqa: E402
    Segment,
    apply_entry,
    coalesce_segments,
    diff_state,
    integral,
    replay,
    residue_view,
)
from app.query import _pointwise_check  # noqa: E402


def _runs_from_ticks(ticks: dict[int, int]) -> list[it.Run]:
    runs: list[it.Run] = []
    for tick in sorted(ticks):
        if runs and runs[-1].end == tick and runs[-1].value == ticks[tick]:
            previous = runs[-1]
            runs[-1] = it.Run(previous.start, tick + 1, previous.value)
        else:
            runs.append(it.Run(tick, tick + 1, ticks[tick]))
    return runs


def ref_apply(runs: dict[int, int], start: int, end: int, value: int) -> dict[int, int]:
    """Trivial unit-tick reference model; the oracle for every exactness test."""
    return {**runs, **{t: value for t in range(start, end)}}


class RunAlgebraTests(unittest.TestCase):
    def test_half_open_boundaries(self):
        runs = [it.Run(10, 20, 5)]
        self.assertEqual(it.value_at(runs, 10), 5)
        self.assertEqual(it.value_at(runs, 19), 5)
        self.assertIsNone(it.value_at(runs, 20))
        self.assertIsNone(it.value_at(runs, 9))
        self.assertEqual(it.integral(runs, 0, 100), 50)

    def test_partial_overlay_keeps_both_residues(self):
        out = it.overlay([it.Run(10, 20, 5)], 14, 16, 9)
        self.assertEqual(
            out,
            [it.Run(10, 14, 5), it.Run(14, 16, 9), it.Run(16, 20, 5)],
        )

    def test_overlay_spanning_many_runs_counts_once(self):
        runs = [it.Run(0, 10, 1), it.Run(10, 25, 1), it.Run(25, 40, 3)]
        # Runs 0..10 and 10..25 were split by a prior revision; they must not
        # be counted twice when one revision covers both.
        out = it.overlay(runs, 5, 30, 7)
        self.assertEqual(
            out,
            [it.Run(0, 5, 1), it.Run(5, 30, 7), it.Run(30, 40, 3)],
        )
        self.assertEqual(it.integral(out, 0, 40), 5 * 1 + 25 * 7 + 10 * 3)

    def test_overlay_entirely_inside_gap(self):
        out = it.overlay([it.Run(0, 5, 1), it.Run(9, 12, 2)], 5, 9, 4)
        self.assertEqual(out, [it.Run(0, 5, 1), it.Run(5, 9, 4), it.Run(9, 12, 2)])

    def test_revision_can_extend_past_extent(self):
        out = it.overlay([it.Run(10, 20, 5)], 18, 30, 2)
        self.assertEqual(out, [it.Run(10, 18, 5), it.Run(18, 30, 2)])

    def test_rejects_bad_runs(self):
        with self.assertRaises(ValueError):
            it.Run(5, 5, 1)
        with self.assertRaises(ValueError):
            it.Run(6, 5, 1)
        with self.assertRaises(ValueError):
            it.Run(0, 5, -1)
        with self.assertRaises(ValueError):
            it.coalesce([it.Run(0, 10, 1), it.Run(5, 12, 2)])

    def test_coalesce_merges_touching_equal_runs(self):
        self.assertEqual(
            it.coalesce([it.Run(0, 5, 1), it.Run(5, 9, 1), it.Run(9, 10, 2)]),
            [it.Run(0, 9, 1), it.Run(9, 10, 2)],
        )

    def test_bucket_bounds_are_clipped(self):
        self.assertEqual(
            it.bucket_bounds(3, 14, 5),
            [(3, 5), (5, 10), (10, 14)],
        )

    def test_bucket_totals_agree_with_reference(self):
        rng = random.Random(7)
        ticks: dict[int, int] = {}
        cursor = 0
        while cursor < 300:
            value = rng.randrange(0, 9)
            length = rng.randrange(1, 15)
            for t in range(cursor, min(cursor + length, 300)):
                ticks[t] = value
            cursor += length
        runs = _runs_from_ticks(ticks)
        for granularity in (1, 2, 5, 7, 60):
            for lo, hi in ((0, 300), (3, 14), (17, 17 + 61)):
                buckets, _ = it.bucket_totals_compressed(runs, lo, hi, granularity)
                expected = {}
                for t, v in ticks.items():
                    if lo <= t < hi:
                        key = (t // granularity) * granularity
                        expected[key] = expected.get(key, 0) + v
                for b in buckets:
                    key = (b["start"] // granularity) * granularity
                    self.assertEqual(
                        b["total"], expected.get(key, 0), f"gran={granularity} lo={lo} hi={hi}"
                    )


class MergeReplayTests(unittest.TestCase):
    def test_segments_keep_provenance_and_residues(self):
        state = apply_entry([], {"id": 1, "kind": "observation", "runs": [{"start": 10, "end": 20, "value": 5}]})
        state = apply_entry(state, {"id": 2, "kind": "revision", "start": 14, "end": 16, "value": 9})
        self.assertEqual(
            [(s.start, s.end, s.value, s.source_entry) for s in state],
            [(10, 14, 5, 1), (14, 16, 9, 2), (16, 20, 5, 1)],
        )
        residues = residue_view(state, 14, 16)
        self.assertEqual([(s.start, s.end) for s in residues["left"]], [])
        self.assertEqual([(s.start, s.end) for s in residues["right"]], [])

        wider = residue_view(state, 13, 17)
        self.assertEqual([(s.start, s.end) for s in wider["left"]], [(10, 13)])
        self.assertEqual([(s.start, s.end) for s in wider["right"]], [(17, 20)])

    def test_overlapping_revisions_last_wins_and_no_double_count(self):
        state = apply_entry([], {"id": 1, "kind": "observation", "runs": [{"start": 0, "end": 100, "value": 1}]})
        state = apply_entry(state, {"id": 2, "kind": "revision", "start": 10, "end": 50, "value": 50})
        state = apply_entry(state, {"id": 3, "kind": "revision", "start": 40, "end": 60, "value": 60})
        expected = ref_apply({**{t: 1 for t in range(0, 100)}}, 10, 50, 50)
        expected = ref_apply(expected, 40, 60, 60)
        for t in range(0, 100):
            self.assertEqual(
                next((s.value for s in state if s.start <= t < s.end), None),
                expected[t],
                f"tick {t}",
            )
        self.assertEqual(integral(state, 0, 100), sum(expected.values()))

    def test_revoked_revision_restores_prior_state(self):
        entries = [
            {"id": 1, "kind": "observation", "runs": [{"start": 10, "end": 20, "value": 5}]},
            {"id": 2, "kind": "revision", "start": 12, "end": 18, "value": 9},
        ]
        with_rev = replay(entries, set())
        without = replay(entries, {2})
        self.assertEqual(integral(with_rev, 0, 30), 5 * 2 + 9 * 6 + 5 * 2)
        self.assertEqual(integral(without, 0, 30), 50)
        self.assertEqual(
            [(s.start, s.end, s.value) for s in without], [(10, 20, 5)]
        )

    def test_null_revision_does_not_create_coverage(self):
        entries = [
            {"id": 1, "kind": "observation", "runs": [{"start": 0, "end": 10, "value": 3}]},
            {"id": 2, "kind": "revision", "start": 10, "end": 20, "value": 0},
        ]
        state = replay(entries, set())
        self.assertEqual([(s.start, s.end, s.value) for s in state], [(0, 10, 3), (10, 20, 0)])
        self.assertEqual(integral(state, 0, 20), 30)
        entries_none = [
            {"id": 1, "kind": "observation", "runs": [{"start": 0, "end": 10, "value": 3}]},
        ]
        self.assertEqual(integral(replay(entries_none, set()), 0, 20), 30)

    def test_random_replay_matches_unit_reference(self):
        rng = random.Random(99)
        for _ in range(200):
            entries = []
            expected: dict[int, int] = {}
            seq = 1
            for _ in range(rng.randrange(1, 8)):
                start = rng.randrange(0, 30)
                end = start + rng.randrange(1, 12)
                value = rng.randrange(0, 10)
                if rng.random() < 0.5:
                    entries.append({"id": seq, "kind": "observation", "runs": [{"start": start, "end": end, "value": value}]})
                else:
                    entries.append({"id": seq, "kind": "revision", "start": start, "end": end, "value": value})
                expected = ref_apply(expected, start, end, value)
                seq += 1
            revoked = {e["id"] for e in entries if e["kind"] == "revision" and rng.random() < 0.3}
            # Rebuild the reference skipping revoked revisions, in ledger order.
            expected = {}
            for entry in entries:
                if entry["kind"] == "revision" and entry["id"] in revoked:
                    continue
                expected = ref_apply(
                    expected,
                    entry["start"] if "start" in entry else entry["runs"][0]["start"],
                    entry["end"] if "end" in entry else entry["runs"][0]["end"],
                    entry["value"] if "value" in entry else entry["runs"][0]["value"],
                )
            state = replay(entries, revoked)
            covered = {}
            for seg in state:
                for t in range(seg.start, seg.end):
                    covered[t] = seg.value
            self.assertEqual(covered, expected)

    def test_state_is_always_maximal(self):
        state = coalesce_segments(
            [Segment(0, 5, 1, 1, "observation"), Segment(5, 9, 1, 2, "observation")]
        )
        self.assertEqual(len(state), 2)  # different provenance, deliberately not merged

    def test_diff_reports_the_edges(self):
        before = [Segment(0, 10, 1, 1, "observation")]
        after = [Segment(0, 4, 1, 1, "observation"), Segment(4, 6, 2, 2, "revision"), Segment(6, 10, 1, 1, "observation")]
        diff = diff_state(before, after, 0, 10)
        self.assertEqual(diff["integral_before"], 10)
        self.assertEqual(diff["integral_after"], 12)
        self.assertEqual([(s["start"], s["end"]) for s in diff["changed"]], [(4, 6)])
        # Both edges are owned by entry 1 before and after, so nothing else moved.
        self.assertEqual(diff["provenance_only"], [])
        self.assertEqual(diff["changed_ticks"], 2)


class BucketExactnessTests(unittest.TestCase):
    def test_compressed_equals_pointwise_on_window(self):
        rng = random.Random(5)
        entries = []
        for i in range(1, 20):
            start = rng.randrange(0, 200)
            end = start + rng.randrange(1, 30)
            entries.append({"id": i, "kind": "revision", "start": start, "end": end, "value": rng.randrange(0, 7)})
        state = replay(entries, set())
        for granularity in (1, 3, 5, 10, 60):
            result = _pointwise_check(state, 0, 220, granularity)
            self.assertTrue(result["ok"], result["mismatches"])

    def test_empty_window(self):
        self.assertEqual(it.bucket_totals_compressed([], 10, 10, 5), ([], 0))
        self.assertEqual(it.bucket_bounds(10, 10, 5), [])


if __name__ == "__main__":
    unittest.main()
