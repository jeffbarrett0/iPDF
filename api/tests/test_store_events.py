"""Immutability and audit-log guarantees (need PostgreSQL; conftest starts a throwaway cluster)."""
import io
import os

import psycopg
import pytest

from app import events, ingest, store
from app.db import connection, init_schema
from conftest import make_pdf


@pytest.fixture(scope="module", autouse=True)
def db(environment):
    from app.config import get_settings

    for sub in ("originals", "outputs", "tmp", "cache"):
        (get_settings().data_dir / sub).mkdir(parents=True, exist_ok=True)
    init_schema()


def upload(name="doc.pdf", texts=("hello",)):
    row, _ = ingest.validate_and_store(io.BytesIO(make_pdf(list(texts))), name)
    return row


def test_original_is_read_only_and_hash_recorded():
    row = upload()
    p = store.path_of(row)
    assert (p.stat().st_mode & 0o222) == 0
    assert store.sha256_file(p) == row["sha256"]
    assert store.verify_file(row)["ok"]
    assert row["expires_at"] is not None


def test_db_blocks_rewriting_file_records():
    row = upload()
    with pytest.raises(psycopg.errors.RaiseException):
        with connection() as conn:
            conn.execute("UPDATE files SET sha256='0' WHERE id=%s", (row["id"],))
    with pytest.raises(psycopg.errors.RaiseException):
        with connection() as conn:
            conn.execute("DELETE FROM files WHERE id=%s", (row["id"],))


def test_tampering_on_disk_is_detected():
    row = upload()
    p = store.path_of(row)
    os.chmod(p.parent, 0o755)
    os.chmod(p, 0o644)
    p.write_bytes(p.read_bytes() + b"\n%tampered")
    assert not store.verify_file(row)["ok"]


def test_event_log_is_append_only_and_chain_detects_tampering():
    upload()
    with connection() as conn:
        assert events.verify_chain(conn)["ok"]
    for sql in ("UPDATE events SET outcome='x'", "DELETE FROM events", "TRUNCATE events"):
        with pytest.raises(psycopg.errors.RaiseException):
            with connection() as conn:
                conn.execute(sql)
    # A superuser who disables the trigger and edits history is still caught by the hash chain.
    def tamper(details_sql):
        with connection() as conn:
            conn.execute("ALTER TABLE events DISABLE TRIGGER events_no_change")
            conn.execute(f"UPDATE events SET details = {details_sql} WHERE id = (SELECT min(id) FROM events)")
            conn.execute("ALTER TABLE events ENABLE TRIGGER events_no_change")

    with connection() as conn:
        original = conn.execute("SELECT details FROM events ORDER BY id LIMIT 1").fetchone()["details"]
    tamper("'{\"evil\": true}'::jsonb")
    with connection() as conn:
        res = events.verify_chain(conn)
    assert not res["ok"] and res["broken_at"] is not None
    tamper(f"'{__import__('json').dumps(original)}'::jsonb")  # undo so later tests share a healthy log
    with connection() as conn:
        assert events.verify_chain(conn)["ok"]


def test_upload_validation():
    from app.errors import AppError

    with pytest.raises(AppError) as e:
        ingest.validate_and_store(io.BytesIO(b"not a pdf"), "x.pdf")
    assert e.value.status == 422
    with pytest.raises(AppError) as e:
        ingest.validate_and_store(io.BytesIO(b"MZ..."), "evil.exe")
    assert e.value.status == 415
    with pytest.raises(AppError):
        ingest.validate_and_store(io.BytesIO(b""), "empty.pdf")
    row = upload("../../weird name?.pdf")
    assert row["display_name"] == "weird_name.pdf"
    assert ".." not in row["stored_path"]
