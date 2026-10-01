"""Human-readable export: plain files plus manifest and event log, no app needed to open it."""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import store
from .config import get_settings
from .db import connection

README = """iPDF export
===========
originals/   files exactly as you uploaded them (never modified)
outputs/     every operation result, named v<version>-<operation>-<name>.pdf
manifest.json  one record per file: SHA-256, size, operation, parameters, source files, warnings
events.csv / events.jsonl  the append-only event log (each entry hash-chains to the previous one)
Verify any file with: sha256sum <file>  and compare with manifest.json.
"""


def build_export(root_id: str | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = store.tmp_dir() / f"ipdf-export-{stamp}-{__import__('uuid').uuid4().hex[:6]}.zip"
    with connection() as conn:
        if root_id:
            rows = conn.execute("SELECT * FROM files WHERE root_id=%s ORDER BY version", (root_id,)).fetchall()
            ev = conn.execute("SELECT * FROM events WHERE file_id = ANY(%s::uuid[]) OR details->>'request_id' IN "
                              "(SELECT id::text FROM sign_requests WHERE file_id = ANY(%s::uuid[])) ORDER BY id",
                              ([r["id"] for r in rows], [r["id"] for r in rows])).fetchall()
        else:
            rows = conn.execute("SELECT * FROM files ORDER BY created_at").fetchall()
            ev = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    manifest = []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README)
        for r in rows:
            entry = {k: (str(v) if not isinstance(v, (int, float, dict, list, type(None), str)) else v)
                     for k, v in store.public(r).items()}
            if r["deleted_at"] is None and r["stored_path"]:
                folder = "originals" if r["kind"] == "original" else "outputs"
                arc = f"{folder}/{Path(r['stored_path']).name}"
                z.write(store.path_of(r), arc)
                entry["export_path"] = arc
            manifest.append(entry)
        z.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "timestamp_utc", "type", "outcome", "file_id", "sha256", "details", "prev_hash", "hash"])
        for e in ev:
            w.writerow([e["id"], e["ts"].isoformat(), e["type"], e["outcome"], e["file_id"] or "", e["sha256"] or "",
                        json.dumps(e["details"], sort_keys=True), e["prev_hash"], e["hash"]])
        z.writestr("events.csv", buf.getvalue())
        z.writestr("events.jsonl", "\n".join(json.dumps({**e, "ts": e["ts"].isoformat(), "file_id": str(e["file_id"]) if e["file_id"] else None},
                                                        default=str) for e in ev))
    return dest
