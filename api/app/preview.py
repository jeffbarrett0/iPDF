from __future__ import annotations

import os
from pathlib import Path

import pypdfium2 as pdfium

from .config import get_settings
from .errors import AppError

ALLOWED_WIDTHS = (160, 320, 640, 900, 1200)


def render_page(pdf_path: Path, sha256: str, page_no: int, width: int) -> Path:
    """Render (and cache by content hash) a PNG preview of one page."""
    width = min(ALLOWED_WIDTHS, key=lambda w: abs(w - width))
    cache_dir = get_settings().data_dir / "cache" / "previews" / sha256
    target = cache_dir / f"{page_no}-{width}.png"
    if target.exists():
        return target
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:  # noqa: BLE001
        raise AppError("preview_failed", "This file could not be rendered for preview.", 422, str(exc)[:160]) from None
    try:
        if not 1 <= page_no <= len(doc):
            raise AppError("bad_page", "Page out of range.", 404)
        page = doc[page_no - 1]
        pw, _ = page.get_size()
        img = page.render(scale=width / pw).to_pil().convert("RGB")
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        img.save(tmp, "PNG", optimize=False)
        os.replace(tmp, target)
        page.close()
    finally:
        doc.close()
    return target
