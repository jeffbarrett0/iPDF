"""Validated intake of uploads."""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from typing import BinaryIO

from . import analysis, pdfops, store
from .config import get_settings
from .errors import AppError
from .safenames import extension_of, safe_filename
from .tools import OFFICE_EXTS

MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
OOXML = {"docx", "xlsx", "pptx"}
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def read_upload(stream: BinaryIO, dest: Path) -> int:
    limit = get_settings().max_upload_bytes
    total = 0
    with open(dest, "wb") as f:
        while chunk := stream.read(1 << 20):
            total += len(chunk)
            if total > limit:
                raise AppError("too_large", f"File exceeds the {get_settings().max_upload_mb} MB limit.", 413,
                               "Raise MAX_UPLOAD_MB in .env if this is intentional.")
            f.write(chunk)
    if total == 0:
        raise AppError("empty_file", "The uploaded file is empty.", 422)
    return total


def validate_and_store(stream: BinaryIO, filename: str) -> tuple[dict, bool]:
    """Returns (file row, is_office). Raises AppError with recoverable messages."""
    ext = extension_of(filename or "")
    if ext != "pdf" and ext not in OFFICE_EXTS:
        raise AppError("unsupported_type", f"'.{ext or '?'}' files are not supported.", 415,
                       "Upload a PDF or an Office/OpenDocument file (docx, xlsx, pptx, odt, rtf, txt…).")
    tmp_root = get_settings().data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    fd = tempfile.NamedTemporaryFile(dir=tmp_root, delete=False, suffix=".upload")
    fd.close()
    tmp = Path(fd.name)
    try:
        read_upload(stream, tmp)
        head = tmp.read_bytes()[:1024]
        stored_name = safe_filename(filename, ext)
        if ext == "pdf":
            if b"%PDF-" not in head:
                raise AppError("not_a_pdf", "This file does not look like a PDF.", 422)
            with pdfops.open_pdf(tmp) as pdf:
                an = analysis.analyze_pdf(pdf)
            row = store.add_original(tmp, display_name=stored_name, stored_name=stored_name, mime=MIME["pdf"],
                                     page_count=an["page_count"], analysis=an,
                                     warnings=analysis.warnings_for("view", [an]))
            return row, False
        if ext in OOXML:
            if not zipfile.is_zipfile(tmp):
                raise AppError("bad_office", f"This is not a valid .{ext} file.", 422)
        elif ext in {"doc", "xls", "ppt"} and head[:8] != OLE_MAGIC:
            raise AppError("bad_office", f"This is not a valid .{ext} file.", 422)
        row = store.add_original(tmp, display_name=stored_name, stored_name=stored_name,
                                 mime=MIME.get(ext, "application/octet-stream"), page_count=None, analysis={}, warnings=[])
        return row, True
    finally:
        tmp.unlink(missing_ok=True)
