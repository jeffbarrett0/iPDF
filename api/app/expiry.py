from __future__ import annotations

import logging
import threading
import time

from . import store
from .config import get_settings
from .db import connection

log = logging.getLogger("ipdf.expiry")


def sweep() -> int:
    """Purge content of expired files (records and event history stay). Files in open signing requests are spared."""
    with connection() as conn:
        rows = conn.execute(
            """SELECT id FROM files f WHERE deleted_at IS NULL AND expires_at IS NOT NULL AND expires_at < now()
               AND NOT EXISTS (SELECT 1 FROM sign_requests r WHERE r.status IN ('draft','sent','completed')
                               AND (r.file_id = f.id OR r.sealed_file_id = f.id))""").fetchall()
    n = 0
    for r in rows:
        try:
            store.delete_file(r["id"], reason="expired")
            n += 1
        except Exception:  # noqa: BLE001
            log.exception("could not expire %s", r["id"])
    tmp = get_settings().data_dir / "tmp"
    if tmp.exists():
        cutoff = time.time() - 86400
        for p in tmp.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
    return n


def start_background(interval: int = 600) -> threading.Event:
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(interval):
            try:
                sweep()
            except Exception:  # noqa: BLE001
                log.exception("expiry sweep failed")

    threading.Thread(target=loop, daemon=True, name="expiry").start()
    return stop
