# Interval Ledger

Run-length observation storage with exact half-open interval arithmetic, auditable
revisions that keep both residues, revocation by replay, and a query path that lets
you choose between compressed and expanded aggregation with the cost visible.

Time is measured in integer **ticks**; one tick = one minute. Epoch is
`2026-11-01T00:00:00Z` (tick 0). Every interval is **left-closed, right-open**:
`[start, end)`. A run occupying `[0, 30)` covers ticks 0 through 29 and the tick
30 belongs to whatever comes next.

## Run it

```bash
python3 run.py                       # intake 8301, merger 8302, query+web 8303
python3 run.py --data-dir /tmp/ldg   # separate data directory
python3 run.py --no-seed             # start with an empty ledger
```

Open <http://127.0.0.1:8303/>. No third-party dependencies; Python 3.11 standard
library only.

```bash
python3 -m unittest discover -s tests        # 40 tests, unit + end-to-end
```

The seed is reproducible (`--seed 20261117`). It writes 47 observation batches,
one 2-run back-fill that crosses six older run boundaries, four revisions
(including one that only touches the middle of a long run), and one revocation.

## The three services

| Service | Port | Owns |
|---|---|---|
| `intake` | 8301 | SQLite ledger; assigns per-source sequence numbers; enforces idempotency |
| `merger` | 8302 | Replay, previews, split-basis audits, drafts, commit, revoke |
| `query`  | 8303 | Independent replay and aggregation; serves the web app and proxies `/api/merger`, `/api/intake` |

They share no memory. `merger` and `query` each replay the ledger they fetch from
`intake`; neither trusts the other's state. The query service serves the browser
from one origin and forwards the two proxy prefixes so the UI has no CORS or
port-configuration story. Each service is also reachable directly.

### Ledger model

Entries are append-only and totally ordered per source (`seq`):

* **observation** — one or more runs, each `[start, end) = value`. Adjacent runs
  inside a batch may not overlap, and equal-valued neighbours must be coalesced;
  both are rejected with a clear code.
* **revision** — `[start, end) = value`, overwriting exactly that span.
* **revoke** — points at a revision sequence number.

`client_id` is unique per source, so a retried POST returns the original entry
with `201` → `200` and `created: false` rather than double-applying.

## Exactness, residues, and the split basis

A revision does not touch whole runs. The merge engine clips each overlapping run
into the piece before the span (**left residue**), the piece after it (**right
residue**), and the piece inside it (**replaced**). Residues keep the provenance
of the entry that originally wrote them. That is what the UI's *split basis* panel
shows: which runs contributed a left residue, which a right residue, which were
wholly replaced, and the tick-by-tick value change.

A revision asserts complete coverage of its own span, so a span that partly sat in
a source gap fills that part. An observation asserts only its runs; a gap after it
stays a gap.

**Revocation is replay, not compensation.** A revoked revision is skipped during
replay, so the state before it reappears exactly — including residues that had
been split by it. No negative rows, no double counting. The revoke itself is an
entry, so the audit trail stays linear.

Revisions that overlap each other are harmless: replay applies them in ledger
order and the last one covering a tick wins. Nothing is counted twice because
every write assigns a span rather than adding to a counter.

## Query shape: compressed vs expanded

Both paths are exposed and are compared live in the UI:

| | compressed | expanded |
|---|---|---|
| Cost | `O(runs + buckets)` | `O(window ticks)` |
| Memory | the runs themselves | one row per tick |
| Boundary precision | exact, any granularity | exact, but only the granularity you chose |
| Update handling | a revision re-cuts local runs; cost is local | rewrites every affected row |

The **compressed** path integrates each run against the bucket bounds. Because
buckets are epoch-aligned and clipped, the first and last bucket may be partial;
the reported `covered` per bucket is what makes a gap distinguishable from a zero.
Its cost is runs plus buckets, not ticks: on a two-day window with 600 runs and
60-tick buckets it takes ~2 ms, while expanding the 172,800 ticks takes ~14 ms.
Widening the window at fixed bucket width still grows the bucket count (measured:
2.1 ms at 172,800 ticks, 3.7 ms at 345,600, 9.2 ms at 691,200), so the honest
statement is *proportional to runs + buckets*, not *flat*. That is still why it is
the default and the only path the merger uses for its own rollups.

The **expanded** path exists for consumers that need dense rows. It is capped at
50,000 ticks on the merger's comparison endpoint and 20,000 on verification, so a
wide window fails loudly instead of silently materialising a million rows.

**When to choose which:** keep runs compressed as the source of truth and for
integrals and rollups at any granularity — long constant stretches make it close
to free. Expand only into a derived cache when a downstream tool demands dense
rows, and treat that cache as disposable: a revision invalidates the affected
rows, but the compressed state is what you replay.

The "Cost of the query shape" card reports runs scanned, rows materialised,
estimated memory, and wall-clock time for both paths on the current window.

## The web app

* **Query window** — from/to ticks and granularity (1, 5, 15, 60), with presets.
* **Integral chart** — one bar per bucket; partially-covered buckets are rendered
  lighter so a quiet source never reads as a zero. Hover gives the exact span,
  integral, and coverage.
* **Coverage strip** — runs scaled by value, with hatched gaps for source silence.
* **Revise a span** — accepts ticks or clock time; previews residues, replaced
  runs, the integral delta, and the tick-level change before writing. A preview
  is bound to the ledger head, so a commit after someone else wrote is refused as
  `stale_preview` rather than silently applying to a moved ledger.
* **Drafts** — the editor autosaves to the *merger* service (SQLite), not just to
  `localStorage`. Close the tab mid-merge, reopen: the draft, its span, and its
  preview come back. Committing clears the server-side draft; discarding deletes it.
* **Ledger** — newest first, with revoked revisions shown as revoked, plus
  *split basis*, *revoke*, and *load into editor* actions.
* **Show revoked revisions as applied** — replay with the revoked set empty, so you
  can see exactly what the revoked revision had been doing.
* **Pointwise check** — expands the first 240 ticks and re-buckets them
  independently, then reports mismatches and half-open edge checks. It does not
  just draw a timeline; it proves the aggregate against a per-tick baseline.

Colours use a validated three-slot categorical set (blue / orange / aqua) that
passes the CVD and normal-vision gates in both light and dark mode; the light-mode
aqua is below 3:1 on the surface, so every use of it is accompanied by a visible
label or a table row.

## API sketch

```
GET  /api/sources                                     intake: sources and heads
POST /api/sources                                     intake: create source
GET  /api/sources/:id/entries?after_seq=&limit=       intake: ledger page
POST /api/sources/:id/observations                    intake: {client_id, runs[]}
POST /api/sources/:id/revisions                       intake: {client_id, start, end, value, base_seq?}
POST /api/sources/:id/revocations                     intake: {client_id, target_seq}
GET  /api/sources/:id/state?lo&hi&granularity          merger: runs, buckets, cost
POST /api/sources/:id/preview                         merger: residues + diff, no write
GET  /api/sources/:id/audit?seq=                      merger: split basis for one entry
GET|PUT|DELETE /api/sources/:id/draft                 merger: server-side draft
POST /api/sources/:id/commit                          merger: commit a revision
POST /api/sources/:id/revoke?seq=                     merger: append a revoke entry
GET  /api/sources/:id/aggregate?lo&hi&granularity     query: compressed + expanded
GET  /api/sources/:id/verify?lo&hi&granularity        query: pointwise baseline
```

## Error codes worth knowing

`stale_preview` (409) — the ledger head moved past the preview's `base_seq`.
`runs_overlap` / `runs_not_coalesced` — malformed observation batch.
`invalid_span` — `start >= end`, i.e. the interval is empty under half-open rules.
`already_revoked` (409) — a revision can only be revoked once.
`window_too_large` — the expanded/verify path was asked to materialise too much.

## Layout

```
app/        intervals.py  exact run algebra and bucketing
            merge.py      provenance segments, replay, residues, diff
            intake.py     ledger service (SQLite)
            merger.py     preview/audit/draft/commit service
            query.py      independent aggregation service + static host
            seed.py       reproducible fictional data via the public API
            common.py     HTTP toolkit and router
web/        index.html, styles.css, app.js
tests/      test_intervals.py, test_revisions.py, test_e2e.py
run.py      starts all three, seeds an empty ledger, stops them together
```
