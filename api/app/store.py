"""User-owned file store. Originals are never modified; every operation writes a new versioned file."""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import events
from .config import get_settings
from .db import connection
from .errors import AppError
from .safenames import safe_stem


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def abs_path(rel: str) -> Path:
    base = get_settings().data_dir.resolve()
    p = (base / rel).resolve()
    if base not in p.parents:
        raise AppError("bad_path", "Stored path escapes the data directory.", 500)
    return p


def tmp_dir() -> Path:
    d = get_settings().data_dir / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lock_file(path: Path) -> None:
    os.chmod(path, 0o444)
    os.chmod(path.parent, 0o555)


def _unlock_dir(path: Path) -> None:
    if path.exists():
        os.chmod(path, 0o755)
        for child in path.iterdir():
            os.chmod(child, 0o644)


def expiry_for(created: datetime) -> datetime | None:
    days = get_settings().file_ttl_days
    return created + timedelta(days=days) if days > 0 else None


def _next_version(conn: psycopg.Connection, root_id: Any) -> int:
    conn.execute("SELECT pg_advisory_xact_lock(7002)")
    row = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM files WHERE root_id = %s", (root_id,)).fetchone()
    return row["v"]


def _place(src_tmp: Path, folder: str, file_id: uuid.UUID, filename: str) -> tuple[str, Path]:
    rel_dir = Path(folder) / str(file_id)
    dest_dir = get_settings().data_dir / rel_dir
    dest_dir.mkdir(parents=True, exist_ok=False)
    dest = dest_dir / filename
    shutil.move(str(src_tmp), dest)
    _lock_file(dest)
    return str(rel_dir / filename), dest


def _rollback_placed(dest: Path) -> None:
    _unlock_dir(dest.parent)
    shutil.rmtree(dest.parent, ignore_errors=True)


def add_original(
    tmp_path: Path, *, display_name: str, stored_name: str, mime: str, page_count: int | None, analysis: dict, warnings: list
) -> dict:
    file_id = uuid.uuid4()
    digest = sha256_file(tmp_path)
    size = tmp_path.stat().st_size
    rel, dest = _place(tmp_path, "originals", file_id, stored_name)
    now = datetime.now(timezone.utc)
    try:
        with connection() as conn:
            row = conn.execute(
                """INSERT INTO files (id, kind, root_id, version, display_name, stored_path, mime, sha256,
                     size_bytes, page_count, analysis, warnings, created_at, expires_at)
                   VALUES (%s,'original',%s,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (file_id, file_id, display_name, rel, mime, digest, size, page_count,
                 Jsonb(analysis), Jsonb(warnings), now, expiry_for(now)),
            ).fetchone()
            events.append(conn, "file.uploaded", file_id=file_id, sha256=digest,
                          details={"name": display_name, "size": size, "pages": page_count})
    except Exception:
        _rollback_placed(dest)
        raise
    return row


def add_output(
    tmp_path: Path, *, operation: str, params: dict, sources: list[dict], page_count: int | None,
    analysis: dict, warnings: list, name_hint: str | None = None, extra_event: dict | None = None,
) -> dict:
    """Commit a produced file as the next immutable version in its lineage."""
    root_id = sources[0]["root_id"]
    file_id = uuid.uuid4()
    digest = sha256_file(tmp_path)
    size = tmp_path.stat().st_size
    now = datetime.now(timezone.utc)
    stem = safe_stem(name_hint or sources[0]["display_name"], "document", 60)
    # Reserve the version number inside the transaction that inserts the row.
    with connection() as conn:
        version = _next_version(conn, root_id)
        filename = f"v{version:03d}-{safe_stem(operation, 'op', 20)}-{stem}.pdf"
        rel, dest = _place(tmp_path, "outputs", file_id, filename)
        try:
            row = conn.execute(
                """INSERT INTO files (id, kind, root_id, version, display_name, stored_path, mime, sha256,
                     size_bytes, page_count, operation, params, source_ids, analysis, warnings, created_at, expires_at)
                   VALUES (%s,'output',%s,%s,%s,%s,'application/pdf',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (file_id, root_id, version, filename, rel, digest, size, page_count, operation,
                 Jsonb(params), [s["id"] for s in sources], Jsonb(analysis), Jsonb(warnings), now, expiry_for(now)),
            ).fetchone()
            events.append(conn, "file.created", file_id=file_id, sha256=digest, details={
                "operation": operation, "version": version, "params": params,
                "sources": [{"id": str(s["id"]), "sha256": s["sha256"]} for s in sources],
                "pages": page_count, "size": size, **(extra_event or {}),
            })
        except Exception:
            _rollback_placed(dest)
            raise
    return row


def get_file(conn: psycopg.Connection, file_id: Any, *, require_content: bool = True) -> dict:
    try:
        fid = uuid.UUID(str(file_id))
    except ValueError:
        raise AppError("bad_id", "Invalid file id.", 400) from None
    row = conn.execute("SELECT * FROM files WHERE id = %s", (fid,)).fetchone()
    if not row:
        raise AppError("not_found", "File not found.", 404, "It may have been deleted or expired.")
    if require_content and row["deleted_at"]:
        raise AppError("gone", f"This file was {row['deleted_reason'] or 'deleted'} and its content is gone.", 410,
                       "Restore it from a backup if you have one.")
    return row


def path_of(row: dict) -> Path:
    if not row["stored_path"]:
        raise AppError("gone", "File content no longer exists.", 410)
    p = abs_path(row["stored_path"])
    if not p.exists():
        raise AppError("missing", "The stored file is missing from disk.", 500,
                       "Restore the data directory from a backup.")
    return p


def verify_file(row: dict) -> dict:
    p = path_of(row)
    actual = sha256_file(p)
    return {"ok": actual == row["sha256"], "expected": row["sha256"], "actual": actual}


def delete_file(file_id: Any, reason: str = "deleted") -> dict:
    with connection() as conn:
        row = get_file(conn, file_id)
        if row["stored_path"]:
            folder = abs_path(row["stored_path"]).parent
            _unlock_dir(folder)
            shutil.rmtree(folder, ignore_errors=True)
        conn.execute(
            "UPDATE files SET stored_path = NULL, deleted_at = now(), deleted_reason = %s WHERE id = %s",
            (reason, row["id"]),
        )
        events.append(conn, "file.deleted" if reason == "deleted" else "file.expired",
                      file_id=row["id"], sha256=row["sha256"], details={"name": row["display_name"], "reason": reason})
    shutil.rmtree(get_settings().data_dir / "cache" / "previews" / row["sha256"], ignore_errors=True)
    return row


def public(row: dict) -> dict:
    """Serialisable subset of a file row for the API."""
    d = {k: v for k, v in row.items() if k != "stored_path"}
    d["id"] = str(d["id"])
    d["root_id"] = str(d["root_id"])
    d["source_ids"] = [str(s) for s in d["source_ids"]]
    return d
