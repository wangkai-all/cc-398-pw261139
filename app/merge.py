"""Merge engine: provenance-carrying runs, residues, replay and diffs.

A ``Segment`` is a run plus the ledger entry that last wrote it.  Merging a
revision only rewrites its own span; runs that stick out on either side stay
alive as residues with their original provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    value: int
    source_entry: int
    source_kind: str

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError("segment must be non-empty")
        if self.value < 0:
            raise ValueError("segment value must be non-negative")

    @property
    def ticks(self) -> int:
        return self.end - self.start

    def clip(self, lo: int, hi: int) -> "Segment | None":
        start, end = max(self.start, lo), min(self.end, hi)
        if start >= end:
            return None
        return Segment(start, end, self.value, self.source_entry, self.source_kind)

    def overlaps(self, lo: int, hi: int) -> bool:
        return self.start < hi and self.end > lo

    def as_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "value": self.value,
            "ticks": self.ticks,
            "source_entry": self.source_entry,
            "source_kind": self.source_kind,
        }


def coalesce_segments(segments: Iterable[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for seg in sorted(segments, key=lambda s: (s.start, s.end)):
        if seg.start >= seg.end:
            raise ValueError("empty segment")
        if out:
            prev = out[-1]
            if seg.start < prev.end:
                raise ValueError(f"overlapping segments {prev} and {seg}")
            if (
                seg.start == prev.end
                and seg.value == prev.value
                and seg.source_entry == prev.source_entry
                and seg.source_kind == prev.source_kind
            ):
                out[-1] = Segment(
                    prev.start, seg.end, seg.value, prev.source_entry, prev.source_kind
                )
                continue
        out.append(seg)
    return out


def overlay_segments(
    state: Sequence[Segment],
    start: int,
    end: int,
    value: int,
    source_entry: int,
    source_kind: str,
) -> list[Segment]:
    """Assign [start, end) to ``value``, keeping both residues intact.

    The caller always asserts complete coverage of [start, end): an
    observation's runs carry their own extent, and a revision replaces its whole
    span.  Runs sticking out on either side keep their original provenance.
    """
    if start >= end:
        raise ValueError("overlay span must be non-empty")
    new = Segment(start, end, value, source_entry, source_kind)
    out: list[Segment] = []
    placed = False
    for seg in state:
        if seg.end <= start or seg.start >= end:
            out.append(seg)
            continue
        if seg.start < start:
            out.append(Segment(seg.start, start, seg.value, seg.source_entry, seg.source_kind))
        if seg.end > end:
            out.append(Segment(end, seg.end, seg.value, seg.source_entry, seg.source_kind))
        if not placed:
            out.append(new)
            placed = True
    if not placed:
        out.append(new)
    return coalesce_segments(out)


def apply_entry(state: Sequence[Segment], entry: dict) -> list[Segment]:
    """Apply one ledger entry (observation or revision) to a run state."""
    if entry["kind"] == "observation":
        out = list(state)
        for run in entry.get("runs") or []:
            out = overlay_segments(
                out,
                int(run["start"]),
                int(run["end"]),
                int(run["value"]),
                int(entry["id"]),
                "observation",
            )
        return out
    if entry["kind"] == "revision":
        return overlay_segments(
            list(state),
            int(entry["start"]),
            int(entry["end"]),
            int(entry["value"]),
            int(entry["id"]),
            "revision",
        )
    if entry["kind"] == "revoke":
        return list(state)
    raise ValueError(f"unknown entry kind {entry['kind']!r}")


def entry_span(entry: dict) -> tuple[int, int] | None:
    if entry["kind"] in {"revision", "revoke"}:
        return int(entry["start"]), int(entry["end"])
    runs = entry.get("runs") or []
    if not runs:
        return None
    return min(int(r["start"]) for r in runs), max(int(r["end"]) for r in runs)


def replay(entries: Sequence[dict], revoked: set[int]) -> list[Segment]:
    """Rebuild state from the ledger, skipping revoked revisions."""
    state: list[Segment] = []
    for entry in sorted(entries, key=lambda e: int(e["id"])):
        if entry["kind"] == "revoke":
            continue
        if entry["kind"] == "revision" and int(entry["id"]) in revoked:
            continue
        state = apply_entry(state, entry)
    return state


def state_stats(state: Sequence[Segment]) -> dict:
    if not state:
        return {"seg_count": 0, "covered_ticks": 0, "extent_start": None, "extent_end": None}
    return {
        "seg_count": len(state),
        "covered_ticks": sum(s.ticks for s in state),
        "extent_start": state[0].start,
        "extent_end": state[-1].end,
    }


def integral(state: Sequence[Segment], lo: int, hi: int) -> int:
    if lo >= hi:
        return 0
    total = 0
    for seg in state:
        if seg.end <= lo:
            continue
        if seg.start >= hi:
            break
        s, e = max(seg.start, lo), min(seg.end, hi)
        if s < e:
            total += seg.value * (e - s)
    return total


def _clip(state: Sequence[Segment], lo: int, hi: int) -> list[Segment]:
    return [c for seg in state if (c := seg.clip(lo, hi)) is not None]


def residue_view(state: Sequence[Segment], start: int, end: int) -> dict:
    """Parts of the old runs that a revision leaves behind on either side."""
    left: list[Segment] = []
    right: list[Segment] = []
    replaced: list[Segment] = []
    for seg in state:
        if seg.end <= start or seg.start >= end:
            continue
        if seg.start < start:
            c = seg.clip(seg.start, start)
            if c:
                left.append(c)
        if seg.end > end:
            c = seg.clip(end, seg.end)
            if c:
                right.append(c)
        c = seg.clip(max(seg.start, start), min(seg.end, end))
        if c:
            replaced.append(c)
    return {"left": left, "right": right, "replaced": replaced}


def diff_state(before: Sequence[Segment], after: Sequence[Segment], lo: int, hi: int) -> dict:
    """Tick-level diff inside [lo, hi), keeping provenance separate.

    ``changed`` lists sub-intervals whose *value* (or coverage) differs.
    ``provenance_only`` lists sub-intervals where the value is unchanged but a
    different ledger entry now owns the span.  Boundary re-cuts with the same
    value and owner are not data changes and are deliberately not listed.
    """
    b = _clip(before, lo, hi)
    a = _clip(after, lo, hi)
    bounds = sorted({lo, hi} | {s.start for s in b + a} | {s.end for s in b + a})

    def value_at(segs: Sequence[Segment], tick: int) -> Segment | None:
        for seg in segs:
            if seg.start <= tick < seg.end:
                return seg
            if seg.start > tick:
                break
        return None

    changed: list[dict] = []
    provenance: list[dict] = []
    for start, end in zip(bounds, bounds[1:]):
        if start >= end:
            continue
        sb, sa = value_at(b, start), value_at(a, start)
        vb = sb.value if sb else None
        va = sa.value if sa else None
        row = {
            "start": start,
            "end": end,
            "ticks": end - start,
            "before": {"value": vb, "source_entry": sb.source_entry} if sb else None,
            "after": {"value": va, "source_entry": sa.source_entry} if sa else None,
        }
        if vb != va:
            changed.append(row)
        elif sb and sa and sb.source_entry != sa.source_entry:
            provenance.append(row)
    return {
        "segments_before": [s.as_dict() for s in b],
        "segments_after": [s.as_dict() for s in a],
        "changed": changed,
        "provenance_only": provenance,
        "changed_ticks": sum(row["ticks"] for row in changed),
        "integral_before": integral(before, lo, hi),
        "integral_after": integral(after, lo, hi),
        "covered_before": sum(s.ticks for s in b),
        "covered_after": sum(s.ticks for s in a),
    }
