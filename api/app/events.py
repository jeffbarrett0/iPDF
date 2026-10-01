"""Append-only, hash-chained event log.

Each entry's hash covers the previous entry's hash, so any edit or removal of history
(even by someone with direct database access) is detectable with `verify_chain`.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import psycopg

GENESIS = "0" * 64


def _canonical(ts: datetime, type_: str, file_id: Any, sha256: str | None, outcome: str, details: dict) -> str:
    return json.dumps(
        {
            "ts": ts.astimezone(timezone.utc).isoformat(timespec="microseconds"),
            "type": type_,
            "file_id": str(file_id) if file_id else None,
            "sha256": sha256,
            "outcome": outcome,
            "details": details,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _entry_hash(prev: str, canonical: str) -> str:
    return hashlib.sha256((prev + canonical).encode()).hexdigest()


def append(
    conn: psycopg.Connection,
    type_: str,
    *,
    file_id: Any = None,
    sha256: str | None = None,
    outcome: str = "ok",
    details: dict | None = None,
) -> dict:
    details = json.loads(json.dumps(details or {}, default=str))  # JSON-normalise
    conn.execute("SELECT pg_advisory_xact_lock(7001)")
    row = conn.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else GENESIS
    ts = datetime.now(timezone.utc)
    h = _entry_hash(prev, _canonical(ts, type_, file_id, sha256, outcome, details))
    return conn.execute(
        """INSERT INTO events (ts, type, file_id, sha256, outcome, details, prev_hash, hash)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (ts, type_, file_id, sha256, outcome, json.dumps(details), prev, h),
    ).fetchone()


def verify_chain(conn: psycopg.Connection) -> dict:
    prev = GENESIS
    count = 0
    cur = conn.execute("SELECT * FROM events ORDER BY id")
    for row in cur:
        canon = _canonical(row["ts"], row["type"], row["file_id"], row["sha256"], row["outcome"], row["details"])
        if row["prev_hash"] != prev or row["hash"] != _entry_hash(prev, canon):
            return {"ok": False, "checked": count, "broken_at": row["id"], "head": prev}
        prev = row["hash"]
        count += 1
    return {"ok": True, "checked": count, "broken_at": None, "head": prev}
