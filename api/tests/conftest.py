from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Dictionary, Name, Pdf, Stream


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def environment():
    """Throwaway data dir + throwaway local PostgreSQL cluster (or TEST_DATABASE_URL if provided)."""
    root = Path(tempfile.mkdtemp(prefix="ipdf-test-"))
    os.chmod(root, 0o755)
    os.environ.update({
        "DATA_DIR": str(root / "data"), "BACKUP_DIR": str(root / "backups"), "SECRET_KEY": "test-secret",
        "FILE_TTL_DAYS": "30", "SMTP_HOST": "", "PUBLIC_BASE_URL": "http://localhost:3000", "PG_PASSWORD": "test-pw",
    })
    from app import localpg
    from app.config import reset_settings_cache

    if os.environ.get("TEST_DATABASE_URL"):
        os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
        reset_settings_cache()
        stop = lambda: None  # noqa: E731
    else:
        port = _free_port()
        os.environ["PG_PORT"] = str(port)
        os.environ.pop("DATABASE_URL", None)
        reset_settings_cache()
        pgdata = root / "pg"
        localpg.start(pgdata=pgdata, port=port, password="test-pw")
        stop = lambda: localpg.stop(pgdata)  # noqa: E731
    yield root
    from app.db import close_pool

    close_pool()
    stop()


@pytest.fixture()
def tmp(tmp_path):
    return tmp_path


def make_pdf(texts: list[str], *, rotate: int = 0, page_size=(612, 792)) -> bytes:
    """Build a small PDF with one line of Helvetica text per page."""
    import io

    pdf = Pdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    for t in texts:
        pdf.add_blank_page(page_size=page_size)
        page = pdf.pages[-1]
        page.Resources = Dictionary(Font=Dictionary(F1=font))
        page.Contents = Stream(pdf, f"BT /F1 24 Tf 72 700 Td ({t}) Tj ET".encode())
        if rotate:
            page.Rotate = rotate
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


@pytest.fixture()
def pdf_factory(tmp_path):
    def make(name: str, texts: list[str], **kw) -> Path:
        p = tmp_path / name
        p.write_bytes(make_pdf(texts, **kw))
        return p

    return make
