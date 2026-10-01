"""Backup/restore: a tar.gz holding stored files plus a JSONL dump of every table (no pg_dump needed).

    python -m app.backup create [DEST_DIR]
    python -m app.backup restore ARCHIVE      # into an EMPTY database/data directory only
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import events, store
from .config import get_settings
from .db import close_pool, connection, init_schema
from .errors import AppError

TABLES = ["files", "events", "sign_requests", "signers", "sign_fields"]
FILE_DIRS = ["originals", "outputs"]


def _json_default(o):
    return str(o)


def create_backup(dest_dir: Path | None = None) -> Path:
    s = get_settings()
    dest_dir = dest_dir or s.backup_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = dest_dir / f"ipdf-backup-{stamp}.tar.gz"
    hashes: dict[str, str] = {}
    counts: dict[str, int] = {}
    with connection() as conn, tarfile.open(archive, "w:gz") as tar:
        # REPEATABLE READ gives one consistent snapshot of every table.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        for t in TABLES:
            rows = conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()  # noqa: S608 - fixed table names
            data = "\n".join(json.dumps(r, default=_json_default) for r in rows).encode()
            counts[t] = len(rows)
            hashes[f"db/{t}.jsonl"] = hashlib.sha256(data).hexdigest()
            info = tarfile.TarInfo(f"db/{t}.jsonl")
            info.size = len(data)
            info.mtime = int(datetime.now().timestamp())
            tar.addfile(info, io.BytesIO(data))
        live = conn.execute("SELECT stored_path FROM files WHERE stored_path IS NOT NULL").fetchall()
        for r in live:
            p = store.abs_path(r["stored_path"])
            if p.exists():
                arc = f"data/{r['stored_path']}"
                hashes[arc] = store.sha256_file(p)
                tar.add(p, arcname=arc)
        manifest = json.dumps({"format": 1, "created": stamp, "counts": counts, "sha256": hashes}, indent=1).encode()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        events.append(conn, "backup.created", details={"archive": archive.name, "files": len(live), "counts": counts})
    os.chmod(archive, 0o600)
    return archive


def _coltypes(conn, table: str) -> dict[str, str]:
    rows = conn.execute("""SELECT a.attname AS n, format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a
                           WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped""", (table,)).fetchall()
    return {r["n"]: r["t"] for r in rows}


def restore_backup(archive: Path) -> dict:
    s = get_settings()
    init_schema()
    with connection() as conn:
        if conn.execute("SELECT (SELECT count(*) FROM files) + (SELECT count(*) FROM events) AS n").fetchone()["n"]:
            raise AppError("not_empty", "Restore only works into an empty database.", 409,
                           "Point DATABASE_URL/DATA_DIR at a fresh location, then restore.")
    with tarfile.open(archive, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers() if m.isfile()}
        for name in members:
            if name.startswith("/") or ".." in Path(name).parts:
                raise AppError("bad_archive", f"Unsafe path in archive: {name}", 422)
        manifest = json.loads(tar.extractfile(members["MANIFEST.json"]).read())
        for name, want in manifest["sha256"].items():
            if hashlib.sha256(tar.extractfile(members[name]).read()).hexdigest() != want:
                raise AppError("corrupt_archive", f"Checksum mismatch for {name}.", 422)
        with connection() as conn:
            for t in TABLES:
                types = _coltypes(conn, t)
                text = tar.extractfile(members[f"db/{t}.jsonl"]).read().decode()
                for line in filter(None, text.split("\n")):
                    row = json.loads(line)
                    cols = list(types)
                    ph, vals = [], []
                    for c in cols:
                        v = row.get(c)
                        typ = types[c]
                        if typ == "jsonb":
                            ph.append("%s::jsonb")
                            vals.append(json.dumps(v))
                        elif typ.endswith("[]"):
                            ph.append(f"%s::{typ}")
                            vals.append("{" + ",".join(v) + "}" if v is not None else None)
                        else:
                            ph.append(f"%s::{typ}")
                            vals.append(None if v is None else str(v))
                    conn.execute(f"INSERT INTO {t} ({','.join(cols)}) VALUES ({','.join(ph)})", vals)  # noqa: S608
            conn.execute("SELECT setval(pg_get_serial_sequence('events','id'), COALESCE((SELECT max(id) FROM events),1))")
            chain = events.verify_chain(conn)
        for name, m in members.items():
            if name.startswith("data/"):
                dest = s.data_dir / name[len("data/"):]
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(dest, 0o444)
                os.chmod(dest.parent, 0o555)
    return {"counts": manifest["counts"], "event_chain": chain}


def list_backups() -> list[dict]:
    d = get_settings().backup_dir
    if not d.exists():
        return []
    return [{"name": p.name, "size": p.stat().st_size, "created": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
            for p in sorted(d.glob("ipdf-backup-*.tar.gz"), reverse=True)]


if __name__ == "__main__":
    try:
        cmd = sys.argv[1] if len(sys.argv) > 1 else ""
        if cmd == "create":
            print(create_backup(Path(sys.argv[2]) if len(sys.argv) > 2 else None))
        elif cmd == "restore" and len(sys.argv) > 2:
            print(json.dumps(restore_backup(Path(sys.argv[2])), indent=2))
        else:
            raise SystemExit(__doc__)
    except AppError as e:
        raise SystemExit(f"error: {e.message} {e.hint or ''}")
    finally:
        close_pool()
