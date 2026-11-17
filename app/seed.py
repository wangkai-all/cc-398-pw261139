"""Create fictional, reproducible observation data through the public API.

The data is determined by a seeded PRNG.  It deliberately includes:
  - long constant stretches (the compression win)
  - natural source gaps (quiet, not zero)
  - one batch whose runs cross many old intervals
  - revisions that only touch the middle of a long run
The last two are what make the residue logic measurable in the UI.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.common import AppError, http_json  # noqa: E402

BASE_URL = os.environ.get("INTAKE_URL", "http://127.0.0.1:8301").rstrip("/")
EPOCH_TICK = 0  # 2026-11-01T00:00Z is tick 0


def log(msg: str) -> None:
    print(f"[seed] {msg}", file=sys.stderr)


def post(path: str, payload: dict) -> dict:
    status, body = http_json(BASE_URL + path, method="POST", payload=payload)
    if status not in (200, 201):
        raise RuntimeError(f"POST {path} failed with {status}")
    return body


def ensure_source(name: str, description: str) -> dict:
    _, body = http_json(BASE_URL + "/api/sources")
    for source in body["sources"]:
        if source["name"] == name:
            if source["head_seq"] > 0:
                log(f"source '{name}' already has {source['head_seq']} entries; leaving it alone")
                raise SystemExit(0)
            return source
    _, body = http_json(
        BASE_URL + "/api/sources", method="POST", payload={"name": name, "description": description}
    )
    return body["source"]


def build_observations(rng: random.Random) -> tuple[list[dict], int]:
    """Emit observations one batch per sampled run; returns (batches, cursor)."""
    batches: list[dict] = []
    cursor = EPOCH_TICK
    client_seq = 0
    while cursor < 480:  # 8 hours of minutes
        value = rng.choice([0, 0, 1, 2, 3, 5, 8, 13, 21])
        ticks = rng.choice([1, 2, 4, 6, 10, 15, 25, 40])
        end = min(cursor + ticks, 480)
        if rng.random() < 0.07:  # source silence, not a zero reading
            cursor = end
            continue
        client_seq += 1
        batches.append(
            {
                "client_id": f"seed-obs-{client_seq:04d}",
                "note": "fictional sensor",
                "runs": [{"start": cursor, "end": end, "value": value}],
            }
        )
        cursor = end
    return batches, cursor


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20261117)
    parser.add_argument("--source", default="demand-meter")
    args = parser.parse_args()
    rng = random.Random(args.seed)
    source = ensure_source(args.source, "Fictional minute-grained demand meter, half-open ticks")
    source_id = source["id"]
    log(f"source id={source_id} name={source['name']}")

    batches, cursor = build_observations(rng)
    for batch in batches:
        post(f"/api/sources/{source_id}/observations", batch)
    log(f"{len(batches)} observation batches covering ticks 0..{cursor}")

    # A back-fill batch with multiple runs that spans old boundaries.  Adjacent
    # equal values are deliberately coalesced: the intake service rejects
    # non-maximal runs, because a state that keeps them is ambiguous to read.
    backfill_runs = [
        {"start": 96, "end": 138, "value": 4},
        {"start": 138, "end": 190, "value": 2},
    ]
    post(
        f"/api/sources/{source_id}/observations",
        {
            "client_id": "seed-backfill-multi",
            "note": "late import, crosses six earlier runs",
            "runs": backfill_runs,
        },
    )
    log("inserted one 2-run back-fill across ticks 96..190")

    revisions = [
        # middle-only revision inside a long late run
        (392, 401, 17, "shift handover spike"),
        # revision crossing an entire short run plus both neighbours
        (52, 71, 6, "meter recalibrated"),
        # revision starting inside a run and ending in a gap
        (300, 318, 9, "reconstructed from second sensor"),
        # short revision deleting a peak entirely (value 0)
        (211, 216, 0, "transient peak rejected"),
    ]
    for start, end, value, note in revisions:
        body = post(
            f"/api/sources/{source_id}/revisions",
            {
                "client_id": f"seed-rev-{start}-{end}",
                "start": start,
                "end": end,
                "value": value,
                "note": note,
            },
        )
        log(f"revision {body['entry']['seq']}: [{start},{end}) = {value} ({note})")

    # Revoke one revision to prove undo restores the prior residues exactly.
    entries = http_json(BASE_URL + f"/api/sources/{source_id}/entries?limit=2000")[1]["entries"]
    target = next(e["seq"] for e in entries if e.get("start") == 211)
    post(
        f"/api/sources/{source_id}/revocations",
        {"client_id": "seed-revocation-1", "target_seq": target, "note": "supervisor reversed"},
    )
    log(f"revoked revision seq={target}")

    _, health = http_json(BASE_URL + "/health")
    log(
        json.dumps(
            {
                "source_id": source_id,
                "entries": len(batches) + 1 + len(revisions) + 1,
                "epoch": health["epoch"],
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except AppError as exc:
        print(json.dumps(exc.payload()), file=sys.stderr)
        raise SystemExit(1)
