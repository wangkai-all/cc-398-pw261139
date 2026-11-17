"""Intake service: validates and persists the append-only observation ledger.

Port 8301 by default.  It assigns every entry a per-source sequence number in a
single transaction, so revisions always have a total order independent of the
clients that sent them.
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

from app.common import (  # noqa: E402
    AppError,
    BaseHandler,
    Router,
    parse_args,
    serve,
)

TICK_SECONDS = 60
EPOCH_ISO = "2026-11-01T00:00:00Z"
MAX_VALUE = 999_999
MAX_SPAN = 10_000_000
MAX_RUNS_PER_BATCH = 2_000

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    source_id INTEGER NOT NULL REFERENCES sources(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('observation','revision','revoke')),
    client_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, seq),
    UNIQUE (source_id, client_id)
);
CREATE INDEX IF NOT EXISTS entries_kind ON entries(source_id, kind, seq);
CREATE TABLE IF NOT EXISTS revocations (
    source_id INTEGER NOT NULL,
    target_seq INTEGER NOT NULL,
    revoked_by_seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, target_seq)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class IntakeStore:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "intake.sqlite3")
        self._lock = threading.Lock()
        with self._connect() as con:
            con.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=15000")
        return con

    # -- reads ---------------------------------------------------------
    def list_sources(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT s.id, s.name, s.description, s.created_at,
                       COALESCE(MAX(e.seq), 0) AS head_seq,
                       COUNT(e.seq) AS entry_count,
                       MAX(CASE WHEN e.kind = 'revision' THEN e.seq END) AS last_revision_seq,
                       COALESCE(SUM(CASE WHEN e.kind = 'observation' THEN 1 ELSE 0 END), 0) AS observation_count,
                       COALESCE(SUM(CASE WHEN e.kind = 'revision' THEN 1 ELSE 0 END), 0) AS revision_count
                FROM sources s LEFT JOIN entries e ON e.source_id = s.id
                GROUP BY s.id ORDER BY s.id
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_source(self, source_id: int) -> dict:
        for source in self.list_sources():
            if source["id"] == source_id:
                return source
        raise AppError(404, "source_not_found", f"Unknown source {source_id}")

    def find_source_by_name(self, name: str) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
        return self.get_source(int(row["id"])) if row else None

    def head(self, source_id: int) -> int:
        with self._connect() as con:
            row = con.execute(
                "SELECT COALESCE(MAX(seq), 0) AS head FROM entries WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        return int(row["head"])

    def list_entries(
        self, source_id: int, after_seq: int = 0, limit: int = 500
    ) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT e.source_id, e.seq, e.kind, e.client_id, e.payload, e.note, e.created_at,
                       r.revoked_by_seq
                FROM entries e
                LEFT JOIN revocations r
                  ON r.source_id = e.source_id AND r.target_seq = e.seq
                WHERE e.source_id = ? AND e.seq > ?
                ORDER BY e.seq LIMIT ?
                """,
                (source_id, after_seq, min(limit, 2000)),
            ).fetchall()
        out = []
        for row in rows:
            entry = {
                "source_id": row["source_id"],
                "id": row["seq"],
                "seq": row["seq"],
                "kind": row["kind"],
                "client_id": row["client_id"],
                "note": row["note"],
                "created_at": row["created_at"],
            }
            entry.update(json.loads(row["payload"]))
            if row["kind"] == "revision":
                entry["revoked_by"] = row["revoked_by_seq"]
                entry["active"] = row["revoked_by_seq"] is None
            out.append(entry)
        return out

    def get_entry(self, source_id: int, seq: int) -> dict:
        entries = self.list_entries(source_id, after_seq=seq - 1, limit=1)
        if not entries or entries[0]["seq"] != seq:
            raise AppError(404, "entry_not_found", f"Entry {seq} not found in source {source_id}")
        return entries[0]

    def revocations(self, source_id: int) -> dict[int, int]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT target_seq, revoked_by_seq FROM revocations WHERE source_id = ?",
                (source_id,),
            ).fetchall()
        return {int(r["target_seq"]): int(r["revoked_by_seq"]) for r in rows}

    # -- writes --------------------------------------------------------
    def create_source(self, name: str, description: str = "") -> dict:
        name = name.strip()
        if not name or len(name) > 80:
            raise AppError(400, "invalid_source", "Source name must be 1-80 characters")
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(
                    "INSERT INTO sources(name, description, created_at) VALUES (?,?,?)",
                    (name, description, now_iso()),
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        found = self.find_source_by_name(name)
        assert found is not None
        return found

    def append(self, source_id: int, kind: str, client_id: str, payload: dict, note: str) -> tuple[dict, bool]:
        """Append or return an existing entry with the same client_id.

        Returns (entry, created).  The uniqueness constraint makes retries safe.
        """
        self.get_source(source_id)
        if not isinstance(client_id, str) or not (1 <= len(client_id) <= 120):
            raise AppError(400, "invalid_client_id", "client_id must be a 1-120 character string")
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                existing = con.execute(
                    "SELECT * FROM entries WHERE source_id = ? AND client_id = ?",
                    (source_id, client_id),
                ).fetchone()
                if existing:
                    con.execute("ROLLBACK")
                    return self.get_entry(source_id, int(existing["seq"])), False
                head = int(
                    con.execute(
                        "SELECT COALESCE(MAX(seq), 0) AS h FROM entries WHERE source_id = ?",
                        (source_id,),
                    ).fetchone()["h"]
                )
                if kind == "revision":
                    base_seq = payload.get("base_seq")
                    if base_seq is not None and int(base_seq) < head:
                        con.execute("ROLLBACK")
                        raise AppError(
                            409,
                            "stale_preview",
                            "The ledger advanced after this preview was computed",
                            {"head_seq": head, "base_seq": int(base_seq), "source_id": source_id},
                        )
                if kind == "revoke":
                    target = int(payload["target_seq"])
                    target_row = con.execute(
                        "SELECT kind FROM entries WHERE source_id = ? AND seq = ?",
                        (source_id, target),
                    ).fetchone()
                    if target_row is None:
                        con.execute("ROLLBACK")
                        raise AppError(404, "entry_not_found", f"Revision {target} not found")
                    if target_row["kind"] != "revision":
                        con.execute("ROLLBACK")
                        raise AppError(400, "not_a_revision", f"Entry {target} is not a revision")
                    already = con.execute(
                        "SELECT revoked_by_seq FROM revocations WHERE source_id = ? AND target_seq = ?",
                        (source_id, target),
                    ).fetchone()
                    if already:
                        con.execute("ROLLBACK")
                        raise AppError(
                            409,
                            "already_revoked",
                            f"Revision {target} was already revoked by entry {already['revoked_by_seq']}",
                        )
                seq = head + 1
                con.execute(
                    "INSERT INTO entries(source_id, seq, kind, client_id, payload, note, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (source_id, seq, kind, client_id, json.dumps(payload, sort_keys=True), note, now_iso()),
                )
                if kind == "revoke":
                    con.execute(
                        "INSERT INTO revocations(source_id, target_seq, revoked_by_seq, created_at)"
                        " VALUES (?,?,?,?)",
                        (source_id, int(payload["target_seq"]), seq, now_iso()),
                    )
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
        return self.get_entry(source_id, seq), True


STORE: IntakeStore


def _int_field(obj: Any, name: str, *, lo: int | None = None, hi: int | None = None) -> int:
    if not isinstance(obj, dict):
        raise AppError(400, "invalid_body", "Expected a JSON object")
    value = obj.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppError(400, "invalid_field", f"'{name}' must be an integer")
    if lo is not None and value < lo:
        raise AppError(400, "invalid_field", f"'{name}' must be >= {lo}")
    if hi is not None and value > hi:
        raise AppError(400, "invalid_field", f"'{name}' must be <= {hi}")
    return value


def validate_runs_payload(body: dict) -> list[dict]:
    runs = body.get("runs")
    if not isinstance(runs, list) or not runs:
        raise AppError(400, "invalid_runs", "'runs' must be a non-empty array")
    if len(runs) > MAX_RUNS_PER_BATCH:
        raise AppError(400, "too_many_runs", f"At most {MAX_RUNS_PER_BATCH} runs per batch")
    out: list[dict] = []
    prev_end: int | None = None
    prev_value: int | None = None
    for i, raw in enumerate(runs):
        if not isinstance(raw, dict):
            raise AppError(400, "invalid_runs", f"runs[{i}] must be an object")
        start = _int_field(raw, "start", lo=0, hi=MAX_SPAN)
        end = _int_field(raw, "end", lo=0, hi=MAX_SPAN)
        value = _int_field(raw, "value", lo=0, hi=MAX_VALUE)
        if start >= end:
            raise AppError(400, "invalid_runs", f"runs[{i}] is empty or reversed: [{start}, {end})")
        if prev_end is not None:
            if start < prev_end:
                raise AppError(
                    400,
                    "runs_overlap",
                    f"runs[{i}] overlaps the previous run inside the same observation",
                )
            if start == prev_end and value == prev_value:
                raise AppError(
                    400,
                    "runs_not_coalesced",
                    f"runs[{i}] touches an equal-valued run; coalesce before sending",
                )
        out.append({"start": start, "end": end, "value": value})
        prev_end, prev_value = end, value
    return out


class IntakeHandler(BaseHandler):
    service_name = "intake"
    routes = Router()

    def _source(self, key: str) -> dict:
        raw = self.params[key]
        try:
            source_id = int(raw)
        except ValueError:
            raise AppError(400, "invalid_source", "Source id must be an integer")
        return STORE.get_source(source_id)

    def health(self) -> dict:
        return {
            "ok": True,
            "service": "intake",
            "epoch": EPOCH_ISO,
            "tick_seconds": TICK_SECONDS,
            "time": now_iso(),
        }

    def list_sources(self) -> dict:
        return {"sources": STORE.list_sources(), "epoch": EPOCH_ISO, "tick_seconds": TICK_SECONDS}

    def get_source(self) -> dict:
        source = self._source("source_id")
        return {
            "source": source,
            "entry_count": source["head_seq"],
            "revocations": STORE.revocations(source["id"]),
        }

    def create_source(self) -> tuple[int, dict]:
        body = self.body_dict()
        name = body.get("name")
        if not isinstance(name, str):
            raise AppError(400, "invalid_source", "name is required")
        description = body.get("description") or ""
        if not isinstance(description, str):
            raise AppError(400, "invalid_source", "description must be a string")
        return 201, {"source": STORE.create_source(name, description)}

    def list_entries(self) -> dict:
        source = self._source("source_id")
        after_seq = self.q_int("after_seq", 0) or 0
        limit = self.q_int("limit", 500) or 500
        return {
            "source_id": source["id"],
            "entries": STORE.list_entries(source["id"], after_seq=after_seq, limit=limit),
            "head_seq": STORE.head(source["id"]),
            "revocations": STORE.revocations(source["id"]),
        }

    def get_entry(self) -> dict:
        source = self._source("source_id")
        seq = self.q_int("seq", required=True)
        assert seq is not None
        return {"entry": STORE.get_entry(source["id"], seq)}

    def post_observation(self) -> tuple[int, dict]:
        source = self._source("source_id")
        body = self.body_dict()
        runs = validate_runs_payload(body)
        note = body.get("note") or ""
        client_id = body.get("client_id")
        entry, created = STORE.append(
            source["id"],
            "observation",
            client_id,
            {"runs": runs},
            str(note)[:500],
        )
        return (201 if created else 200), {
            "entry": entry,
            "created": created,
            "head_seq": STORE.head(source["id"]),
        }

    def post_revision(self) -> tuple[int, dict]:
        source = self._source("source_id")
        body = self.body_dict()
        start = _int_field(body, "start", lo=0, hi=MAX_SPAN)
        end = _int_field(body, "end", lo=0, hi=MAX_SPAN)
        value = _int_field(body, "value", lo=0, hi=MAX_VALUE)
        if start >= end:
            raise AppError(400, "invalid_span", "start must be < end (half-open interval)")
        if end - start > MAX_SPAN:
            raise AppError(400, "span_too_large", f"Revision span capped at {MAX_SPAN} ticks")
        base_seq = body.get("base_seq")
        if base_seq is not None:
            if isinstance(base_seq, bool) or not isinstance(base_seq, int) or base_seq < 0:
                raise AppError(400, "invalid_base_seq", "base_seq must be a non-negative integer")
        note = body.get("note") or ""
        payload = {"start": start, "end": end, "value": value}
        if base_seq is not None:
            payload["base_seq"] = base_seq
        entry, created = STORE.append(
            source["id"], "revision", body.get("client_id"), payload, str(note)[:500]
        )
        return (201 if created else 200), {
            "entry": entry,
            "created": created,
            "head_seq": STORE.head(source["id"]),
        }

    def post_revocation(self) -> tuple[int, dict]:
        source = self._source("source_id")
        body = self.body_dict()
        target = _int_field(body, "target_seq", lo=1)
        base_seq = body.get("base_seq")
        if base_seq is not None and (isinstance(base_seq, bool) or not isinstance(base_seq, int)):
            raise AppError(400, "invalid_base_seq", "base_seq must be an integer")
        note = body.get("note") or ""
        payload = {"target_seq": target}
        if base_seq is not None:
            payload["base_seq"] = base_seq
        entry, created = STORE.append(
            source["id"], "revoke", body.get("client_id"), payload, str(note)[:500]
        )
        return (201 if created else 200), {
            "entry": entry,
            "created": created,
            "head_seq": STORE.head(source["id"]),
        }


router = IntakeHandler.routes
router.add("GET", r"/health", "health")
router.add("GET", r"/api/sources", "list_sources")
router.add("POST", r"/api/sources", "create_source")
router.add("GET", r"/api/sources/(?P<source_id>\d+)", "get_source")
router.add("GET", r"/api/sources/(?P<source_id>\d+)/entries", "list_entries")
router.add("GET", r"/api/sources/(?P<source_id>\d+)/entries/(?P<seq>\d+)", "get_entry")
router.add("POST", r"/api/sources/(?P<source_id>\d+)/observations", "post_observation")
router.add("POST", r"/api/sources/(?P<source_id>\d+)/revisions", "post_revision")
router.add("POST", r"/api/sources/(?P<source_id>\d+)/revocations", "post_revocation")


def main() -> None:
    global STORE
    args = parse_args("Interval ledger intake service")
    STORE = IntakeStore(args.data_dir)
    serve("intake", IntakeHandler, args.port)


if __name__ == "__main__":
    main()
