"""Query service: independent aggregation over the compressed ledger.

Port 8303 by default.  It talks to the intake service for the raw ledger and
replays it locally, so a merger outage never blocks reads.  Two aggregation
paths are exposed side by side:

* ``compressed`` - integrate directly over runs, cost O(runs + buckets)
* ``expanded``   - materialise one row per tick, cost O(window ticks)

Both must agree; the response reports the tick count, segment count, and the
time each path took so the trade-off is visible in the product, not just in a
design note.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import intervals  # noqa: E402
from app.clients import IntakeClient  # noqa: E402
from app.common import AppError, BaseHandler, Router, minute_iso, parse_args, serve  # noqa: E402
from app.merge import Segment, replay, state_stats  # noqa: E402

INTAKE: IntakeClient
EPOCH_ISO = "2026-11-01T00:00:00Z"
_cache: dict[int, tuple[float, list[dict], dict[int, int], int]] = {}


def load_ledger(source_id: int, force: bool = False) -> tuple[list[dict], dict[int, int], int]:
    """Load the ledger, validating the cached copy against the intake head.

    A TTL alone would serve stale reads after a write, so head equality is the
    authority.  The head probe is one small request; the ledger transfer is
    skipped whenever the head is unchanged.
    """
    now = time.monotonic()
    cached = _cache.get(source_id)
    if cached and not force and INTAKE.head_seq(source_id) == cached[3]:
        return cached[1], cached[2], cached[3]
    entries = INTAKE.all_entries(source_id)
    revoked = {
        int(e["seq"]): int(e["revoked_by"])
        for e in entries
        if e["kind"] == "revision" and e.get("revoked_by") is not None
    }
    head = int(entries[-1]["seq"]) if entries else 0
    _cache[source_id] = (now, entries, revoked, head)
    return entries, revoked, head


def state_for(
    source_id: int, *, include_revoked: bool = False, force: bool = False
) -> tuple[list[Segment], dict[int, int], int]:
    entries, revoked, head = load_ledger(source_id, force=force)
    skip = set() if include_revoked else set(revoked)
    return replay(entries, skip), revoked, head


def _runs(state: list[Segment]) -> list[intervals.Run]:
    return [intervals.Run(s.start, s.end, s.value) for s in state]


def _aggregate(
    state: list[Segment],
    lo: int,
    hi: int,
    granularity: int,
    method: str,
) -> dict[str, Any]:
    runs = _runs(state)
    t0 = time.perf_counter()
    if method == "compressed":
        buckets, scanned = intervals.bucket_totals_compressed(runs, lo, hi, granularity)
        ticks = 0
    else:
        points = list(intervals.iter_points(runs, lo, hi))
        buckets, scanned = intervals.bucket_totals_points(points, lo, hi, granularity)
        ticks = len(points)
    elapsed = (time.perf_counter() - t0) * 1000
    total = sum(b["total"] for b in buckets)
    covered = sum(b["covered"] for b in buckets)
    return {
        "method": method,
        "buckets": buckets,
        "totals": [b["total"] for b in buckets],
        "total": total,
        "covered_ticks": covered,
        "span_ticks": max(0, hi - lo),
        "coverage_ratio": round(covered / (hi - lo), 6) if hi > lo else 0,
        "cost": {
            "segments_scanned": scanned,
            "ticks_expanded": ticks,
            "elapsed_ms": round(elapsed, 4),
        },
    }


def _rollup(state: list[Segment], lo: int, hi: int) -> dict:
    by_value: dict[int, dict] = {}
    for seg in state:
        c = seg.clip(lo, hi)
        if c is None:
            continue
        slot = by_value.setdefault(
            c.value, {"value": c.value, "ticks": 0, "integral": 0, "segments": 0}
        )
        slot["ticks"] += c.ticks
        slot["integral"] += c.value * c.ticks
        slot["segments"] += 1
    rows = sorted(by_value.values(), key=lambda r: (-r["integral"], r["value"]))
    for row in rows:
        row["share"] = (
            round(row["integral"] / sum(r["integral"] for r in rows), 6)
            if sum(r["integral"] for r in rows)
            else 0
        )
    return {
        "values": rows,
        "covered_ticks": sum(r["ticks"] for r in rows),
        "integral": sum(r["integral"] for r in rows),
    }


def _pointwise_check(state: list[Segment], lo: int, hi: int, granularity: int) -> dict:
    runs = _runs(state)
    compressed, _ = intervals.bucket_totals_compressed(runs, lo, hi, granularity)
    from collections import defaultdict

    totals: dict[int, int] = defaultdict(int)
    points = 0
    for tick, value in intervals.iter_points(runs, lo, hi):
        points += 1
        totals[(tick // granularity) * granularity] += value
    mismatches = [
        {
            "start": b["start"],
            "end": b["end"],
            "compressed": b["total"],
            "pointwise": totals.get((b["start"] // granularity) * granularity, 0),
        }
        for b in compressed
        if b["total"] != totals.get((b["start"] // granularity) * granularity, 0)
    ]
    edge_checks = []
    for tick in (lo, lo + 1, hi - 1):
        value = intervals.value_at(runs, tick)
        edge_checks.append(
            {
                "tick": tick,
                "value": value,
                "expected_present": any(r.start <= tick < r.end for r in runs),
            }
        )
    # Half-open proof: every bucket must sit inside [lo, hi), and the tick at
    # `hi` is outside the window even when a run continues past it.
    inside = all(b["start"] >= lo and b["end"] <= hi for b in compressed)
    last_covered = compressed[-1]["end"] if compressed else lo
    return {
        "lo": lo,
        "hi": hi,
        "granularity": granularity,
        "point_rows": points,
        "compressed_buckets": len(compressed),
        "mismatches": mismatches,
        "edge_checks": edge_checks,
        "buckets_inside_window": inside,
        "last_bucket_end": last_covered,
        "excluded_tick": hi,
        "ok": not mismatches and inside,
    }


class QueryHandler(BaseHandler):
    service_name = "query"
    routes = Router()
    proxy_map: dict[str, str] = {}

    def _source_id(self) -> int:
        try:
            return int(self.params["source_id"])
        except ValueError:
            raise AppError(400, "invalid_source", "Source id must be an integer")

    def health(self) -> dict:
        return {
            "ok": True,
            "service": "query",
            "cache": "head-validated",
            "epoch": EPOCH_ISO,
            "tick_seconds": 60,
        }

    def sources(self) -> dict:
        return {"sources": INTAKE.list_sources()}

    def aggregate(self) -> dict:
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
        method = self.q("method") or "both"
        if method not in {"compressed", "expanded", "both"}:
            raise AppError(400, "invalid_method", "method must be compressed, expanded or both")
        state, revoked, head = state_for(source_id, include_revoked=include_revoked)
        result: dict[str, Any] = {
            "source_id": source_id,
            "head_seq": head,
            "include_revoked": include_revoked,
            "revoked_revisions": sorted(revoked),
            "extent": state_stats(state),
            "window": {"lo": lo, "hi": hi, "granularity": granularity},
            "segments": [c.as_dict() for s in state if (c := s.clip(lo, hi))],
            "rollup": _rollup(state, lo, hi),
        }
        if method in {"compressed", "both"}:
            result["compressed"] = _aggregate(state, lo, hi, granularity, "compressed")
        if method in {"expanded", "both"}:
            result["expanded"] = _aggregate(state, lo, hi, granularity, "expanded")
        if method == "both":
            result["match"] = result["compressed"]["totals"] == result["expanded"]["totals"]
            result["cost_ratio"] = round(
                result["expanded"]["cost"]["elapsed_ms"]
                / max(result["compressed"]["cost"]["elapsed_ms"], 0.0001),
                2,
            )
        return result

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
        state, revoked, head = state_for(source_id)
        result = _pointwise_check(state, lo, hi, granularity)
        result["head_seq"] = head
        result["segments"] = [s.as_dict() for s in state if s.overlaps(lo, hi)]
        return result

    def point(self) -> dict:
        source_id = self._source_id()
        tick = self.q_int("tick", required=True)
        assert tick is not None
        state, revoked, head = state_for(source_id)
        runs = _runs(state)
        value = intervals.value_at(runs, tick)
        return {
            "source_id": source_id,
            "tick": tick,
            "minute": minute_iso(tick),
            "value": value,
            "present": value is not None,
            "containing": [
                s.as_dict() for s in state if s.start <= tick < s.end
            ],
            "head_seq": head,
        }


router = QueryHandler.routes
router.add("GET", r"/health", "health")
for prefix in (r"/api", r"/api/query"):
    router.add("GET", prefix + r"/sources", "sources")
    router.add("GET", prefix + r"/health", "health")
    router.add("GET", prefix + r"/sources/(?P<source_id>\d+)/aggregate", "aggregate")
    router.add("GET", prefix + r"/sources/(?P<source_id>\d+)/verify", "verify")
    router.add("GET", prefix + r"/sources/(?P<source_id>\d+)/point", "point")


def main() -> None:
    global INTAKE
    args = parse_args("Interval ledger query service")
    INTAKE = IntakeClient(os.environ.get("INTAKE_URL"))
    # The query service is the browser's single origin: it serves the static
    # app and forwards /api/merger and /api/intake to the other two services.
    QueryHandler.proxy_map = {
        "/api/merger": os.environ.get("MERGER_URL", "http://127.0.0.1:8302") + "/api",
        "/api/intake": os.environ.get("INTAKE_URL", "http://127.0.0.1:8301") + "/api",
    }
    QueryHandler.static_dir = os.path.abspath(args.web_dir)
    serve("query", QueryHandler, args.port)


if __name__ == "__main__":
    main()
