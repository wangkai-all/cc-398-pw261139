"""Exact interval algebra for run-length observations.

Time is measured in integer *ticks* (one tick = one minute in this product).
Every interval is half-open: [start, end).  A run carries one integer value
over that span.  Runs in a state are sorted, non-overlapping and maximal
(no two touching runs share a value).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence


@dataclass(frozen=True, order=True)
class Run:
    start: int
    end: int
    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise TypeError("run boundaries must be integers")
        if not isinstance(self.value, int):
            raise TypeError("run value must be an integer")
        if self.start >= self.end:
            raise ValueError(f"empty or reversed run [{self.start}, {self.end})")
        if self.value < 0:
            raise ValueError("run value must be non-negative")

    @property
    def ticks(self) -> int:
        return self.end - self.start

    def clip(self, lo: int, hi: int) -> "Run | None":
        start, end = max(self.start, lo), min(self.end, hi)
        if start >= end:
            return None
        return Run(start, end, self.value)

    def overlaps(self, lo: int, hi: int) -> bool:
        return self.start < hi and self.end > lo


def validate_runs(runs: Sequence[Run]) -> None:
    """Raise if the state is unsorted, overlapping, or not maximal."""
    prev: Run | None = None
    for run in runs:
        if prev is not None:
            if run.start < prev.start:
                raise ValueError("runs are not sorted")
            if run.start < prev.end:
                raise ValueError(f"overlapping runs {prev} and {run}")
            if run.start == prev.end and run.value == prev.value:
                raise ValueError(f"adjacent equal runs not coalesced: {prev} / {run}")
        prev = run


def coalesce(runs: Iterable[Run]) -> list[Run]:
    """Sort, merge touching equal-valued runs, reject real overlaps."""
    ordered = sorted(runs)
    out: list[Run] = []
    for run in ordered:
        if run.start >= run.end:
            raise ValueError(f"empty run [{run.start}, {run.end})")
        if out:
            prev = out[-1]
            if run.start < prev.end:
                raise ValueError(f"overlapping runs {prev} and {run}")
            if run.start == prev.end and run.value == prev.value:
                out[-1] = Run(prev.start, run.end, prev.value)
                continue
        out.append(run)
    return out


def overlay(runs: Sequence[Run], start: int, end: int, value: int) -> list[Run]:
    """Return a state with [start, end) assigned ``value``.

    This is the heart of the product: a run that only partially overlaps the
    revision yields a left residue, the replaced middle, and a right residue.
    Only the first overlapping run contributes the new middle run; every
    further overlap is consumed by the same revision.
    """
    if start >= end:
        raise ValueError("revision span must be non-empty")
    out: list[Run] = []
    placed = False
    for run in runs:
        if run.end <= start or run.start >= end:
            out.append(run)
            continue
        if run.start < start:
            out.append(Run(run.start, start, run.value))
        if run.end > end:
            out.append(Run(end, run.end, run.value))
        if not placed:
            out.append(Run(start, end, value))
            placed = True
    if not placed:
        out.append(Run(start, end, value))
    return coalesce(out)


def integral(runs: Sequence[Run], lo: int | None = None, hi: int | None = None) -> int:
    """Exact integral over [lo, hi): sum(value * ticks)."""
    if lo is None:
        lo = runs[0].start if runs else 0
    if hi is None:
        hi = runs[-1].end if runs else lo
    if lo >= hi:
        return 0
    total = 0
    for run in runs:
        c = run.clip(lo, hi)
        if c is not None:
            total += c.value * c.ticks
    return total


def value_at(runs: Sequence[Run], tick: int) -> int | None:
    """Point lookup with half-open semantics: [start, end)."""
    for run in runs:
        if run.start <= tick < run.end:
            return run.value
        if run.start > tick:
            break
    return None


def iter_points(runs: Sequence[Run], lo: int, hi: int) -> Iterator[tuple[int, int]]:
    """Expand to one (tick, value) row per unit tick."""
    for run in runs:
        c = run.clip(lo, hi)
        if c is None:
            continue
        for tick in range(c.start, c.end):
            yield tick, c.value


def bucket_bounds(lo: int, hi: int, granularity: int) -> list[tuple[int, int]]:
    """Epoch-aligned buckets clipped to [lo, hi).

    The first and last bucket may be partial; the returned bounds are what is
    actually covered, which keeps totals independent of alignment.
    """
    if granularity <= 0:
        raise ValueError("granularity must be positive")
    if lo >= hi:
        return []
    first = (lo // granularity) * granularity
    bounds: list[tuple[int, int]] = []
    b = first
    while b < hi:
        bounds.append((max(b, lo), min(b + granularity, hi)))
        b += granularity
    return bounds


def bucket_totals_compressed(
    runs: Sequence[Run], lo: int, hi: int, granularity: int
) -> tuple[list[dict], int]:
    """Bucket an exact integral directly from compressed runs.

    Cost is O(runs intersecting the window + buckets), independent of window
    width.  Returns (buckets, runs_scanned).  ``covered`` counts ticks that
    actually carry an observation, so source gaps never masquerade as zeros.
    """
    bounds = bucket_bounds(lo, hi, granularity)
    totals = [0] * len(bounds)
    covered = [0] * len(bounds)
    if not bounds:
        return [], 0
    index = 0
    scanned = 0
    for run in runs:
        if run.end <= lo:
            continue
        if run.start >= hi:
            break
        scanned += 1
        while index < len(bounds) and bounds[index][1] <= run.start:
            index += 1
        j = index
        while j < len(bounds) and bounds[j][0] < run.end:
            b0, b1 = bounds[j]
            start, end = max(run.start, b0), min(run.end, b1)
            if start < end:
                totals[j] += run.value * (end - start)
                covered[j] += end - start
            j += 1
    buckets = [
        {
            "start": b0,
            "end": b1,
            "ticks": b1 - b0,
            "covered": covered[i],
            "total": totals[i],
        }
        for i, (b0, b1) in enumerate(bounds)
    ]
    return buckets, scanned


def bucket_totals_points(
    points: Iterable[tuple[int, int]], lo: int, hi: int, granularity: int
) -> tuple[list[dict], int]:
    """Bucket from expanded unit rows. Cost is O(window width)."""
    bounds = bucket_bounds(lo, hi, granularity)
    totals = [0] * len(bounds)
    covered = [0] * len(bounds)
    if not bounds:
        return [], 0
    index = 0
    scanned = 0
    for tick, value in points:
        if tick < lo or tick >= hi:
            continue
        scanned += 1
        while index < len(bounds) and bounds[index][1] <= tick:
            index += 1
        if index >= len(bounds):
            break
        totals[index] += value
        covered[index] += 1
    buckets = [
        {
            "start": b0,
            "end": b1,
            "ticks": b1 - b0,
            "covered": covered[i],
            "total": totals[i],
        }
        for i, (b0, b1) in enumerate(bounds)
    ]
    return buckets, scanned


def run_dict(run: Run) -> dict:
    return {"start": run.start, "end": run.end, "value": run.value, "ticks": run.ticks}
