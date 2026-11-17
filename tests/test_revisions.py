"""Ledger and replay edge cases that the happy path does not cover."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import intervals as it  # noqa: E402
from app.merge import apply_entry, integral, replay  # noqa: E402


class RevocationTests(unittest.TestCase):
    def base(self) -> list[dict]:
        return [
            {"id": 1, "kind": "observation", "runs": [{"start": 0, "end": 100, "value": 5}]},
            {"id": 2, "kind": "revision", "start": 10, "end": 30, "value": 9},
            {"id": 3, "kind": "revision", "start": 20, "end": 40, "value": 7},
        ]

    def test_revoking_the_earlier_overlap_keeps_the_later_override(self):
        # Without #2, #3 still applies to [20,40); [10,20) reverts to 5.
        state = replay(self.base(), {2})
        # [0,20) is one maximal run again: the earlier revision's residue and
        # the untouched head both carry entry 1 and the same value.
        self.assertEqual(
            [(s.start, s.end, s.value) for s in state],
            [(0, 20, 5), (20, 40, 7), (40, 100, 5)],
        )
        self.assertEqual(integral(state, 0, 100), 20 * 5 + 20 * 7 + 60 * 5)

    def test_revoking_the_later_override_restores_the_earlier(self):
        state = replay(self.base(), {3})
        self.assertEqual(
            [(s.start, s.end, s.value) for s in state],
            [(0, 10, 5), (10, 30, 9), (30, 100, 5)],
        )

    def test_revoking_every_revision_is_the_original(self):
        state = replay(self.base(), {2, 3})
        self.assertEqual([(s.start, s.end, s.value) for s in state], [(0, 100, 5)])

    def test_revoked_revision_id_kept_in_ledger_order(self):
        entries = self.base() + [{"id": 4, "kind": "revoke", "target_seq": 2}]
        # A revoke entry itself is inert during replay; the revoked set is what
        # removes #2.  Order of replay therefore stays linear over ledger seq.
        self.assertEqual(replay(entries, {2}), replay(self.base(), {2}))

    def test_reapply_after_revocation_is_a_new_entry(self):
        entries = self.base() + [
            {"id": 4, "kind": "revision", "start": 30, "end": 35, "value": 1},
        ]
        state = replay(entries, {3, 2})
        self.assertEqual(
            [(s.start, s.end, s.value) for s in state],
            [(0, 30, 5), (30, 35, 1), (35, 100, 5)],
        )


class OrderingTests(unittest.TestCase):
    def test_last_revision_in_sequence_wins_regardless_of_span_size(self):
        entries = [
            {"id": 1, "kind": "observation", "runs": [{"start": 0, "end": 60, "value": 1}]},
            {"id": 2, "kind": "revision", "start": 0, "end": 60, "value": 2},
            {"id": 3, "kind": "revision", "start": 25, "end": 26, "value": 3},
        ]
        state = replay(entries, set())
        self.assertEqual(
            [(s.start, s.end, s.value) for s in state],
            [(0, 25, 2), (25, 26, 3), (26, 60, 2)],
        )

    def test_a_narrow_revision_cannot_be_swallowed_by_a_wider_one_before_it(self):
        # #2 covers #1 entirely.  If merge accidentally replaces whole
        # overlapping runs instead of clipping, #1's [10,20) fragment leaks.
        entries = [
            {"id": 1, "kind": "revision", "start": 10, "end": 20, "value": 4},
            {"id": 2, "kind": "revision", "start": 0, "end": 30, "value": 8},
        ]
        state = replay(entries, set())
        self.assertEqual([(s.start, s.end, s.value) for s in state], [(0, 30, 8)])

    def test_observation_wholly_inside_a_run_does_not_duplicate(self):
        entries = [
            {"id": 1, "kind": "observation", "runs": [{"start": 0, "end": 30, "value": 4}]},
            {"id": 2, "kind": "observation", "runs": [{"start": 10, "end": 20, "value": 4}]},
        ]
        state = replay(entries, set())
        # Same value, different entry: both spans must still total 30 ticks.
        self.assertEqual(sum(s.ticks for s in state), 30)
        self.assertEqual(integral(state, 0, 30), 120)
        self.assertEqual(
            [(s.start, s.end, s.source_entry) for s in state],
            [(0, 10, 1), (10, 20, 2), (20, 30, 1)],
        )

    def test_observation_with_multiple_runs_keeps_them_in_order(self):
        entry = {
            "id": 1,
            "kind": "observation",
            "runs": [
                {"start": 0, "end": 5, "value": 1},
                {"start": 7, "end": 9, "value": 2},
                {"start": 9, "end": 12, "value": 3},
            ],
        }
        state = apply_entry([], entry)
        self.assertEqual(
            [(s.start, s.end, s.value) for s in state], [(0, 5, 1), (7, 9, 2), (9, 12, 3)]
        )
        # [5,7) is a real gap, not a zero.
        self.assertEqual(integral(state, 0, 12), 5 + 4 + 9)

    def test_zero_value_revision_is_coverage_not_a_gap(self):
        entries = [
            {"id": 1, "kind": "observation", "runs": [{"start": 0, "end": 10, "value": 3}]},
            {"id": 2, "kind": "revision", "start": 4, "end": 6, "value": 0},
        ]
        state = replay(entries, set())
        self.assertEqual(
            [(s.start, s.end, s.value) for s in state],
            [(0, 4, 3), (4, 6, 0), (6, 10, 3)],
        )
        self.assertEqual(it.integral([it.Run(s.start, s.end, s.value) for s in state], 0, 10), 24)


class GranularityTests(unittest.TestCase):
    def test_partial_buckets_at_both_edges(self):
        runs = [it.Run(0, 100, 1)]
        buckets, _ = it.bucket_totals_compressed(runs, 7, 93, 30)
        self.assertEqual([(b["start"], b["end"]) for b in buckets], [(7, 30), (30, 60), (60, 90), (90, 93)])
        self.assertEqual(sum(b["total"] for b in buckets), 86)

    def test_one_tick_granularity_equals_total(self):
        runs = [it.Run(0, 5, 2), it.Run(5, 9, 0), it.Run(9, 12, 7)]
        buckets, _ = it.bucket_totals_compressed(runs, 0, 12, 1)
        self.assertEqual([b["total"] for b in buckets], [2, 2, 2, 2, 2, 0, 0, 0, 0, 7, 7, 7])
        self.assertEqual(sum(b["total"] for b in buckets), it.integral(runs, 0, 12))


if __name__ == "__main__":
    unittest.main()
