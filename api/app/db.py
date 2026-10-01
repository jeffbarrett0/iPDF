from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from importlib import resources
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

log = logging.getLogger("ipdf.db")
_pool: ConnectionPool | None = None
_lock = threading.Lock()


def get_pool() -> ConnectionPool:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ConnectionPool(
                get_settings().database_url,
                min_size=1,
                max_size=8,
                kwargs={"row_factory": dict_row, "options": "-c timezone=UTC"},
                open=True,
                timeout=10,
            )
        return _pool


def close_pool() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Transactional connection: commits on success, rolls back on error."""
    with get_pool().connection() as conn:
        yield conn


def init_schema() -> None:
    sql = resources.files("app").joinpath("schema.sql").read_text()
    with connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7000)")
        conn.execute(sql)


def database_available() -> bool:
    try:
        with connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("database unavailable: %s", exc)
        return False
