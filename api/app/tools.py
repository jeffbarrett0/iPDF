"""External tools: LibreOffice (Office -> PDF) and OCRmyPDF/Tesseract (OCR)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .config import get_settings
from .errors import AppError, ToolUnavailable

_office_slots = threading.Semaphore(2)
OFFICE_EXTS = {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf", "txt"}


def soffice_binary() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def tool_status() -> dict:
    return {
        "libreoffice": soffice_binary() is not None,
        "tesseract": shutil.which("tesseract") is not None,
        "ghostscript": shutil.which("gs") is not None,
    }


def office_to_pdf(src: Path, out: Path) -> None:
    binary = soffice_binary()
    if not binary:
        raise ToolUnavailable("LibreOffice", "Install LibreOffice (e.g. `apt install libreoffice-writer libreoffice-calc "
                              "libreoffice-impress`) and retry. Your uploaded original is safe.")
    settings = get_settings()
    with _office_slots, tempfile.TemporaryDirectory(dir=settings.data_dir / "tmp") as work:
        work_p = Path(work)
        # Copy under a neutral name so odd filenames never reach the command line.
        local = work_p / f"input.{src.suffix.lstrip('.').lower()}"
        shutil.copy(src, local)
        profile = work_p / "profile"
        cmd = [binary, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
               "--convert-to", "pdf:writer_pdf_Export" if local.suffix in (".doc", ".docx", ".odt", ".rtf", ".txt") else "pdf",
               "--outdir", str(work_p / "out"), str(local)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=settings.office_timeout_seconds,
                                  env={**os.environ, "HOME": str(work_p)})
        except subprocess.TimeoutExpired:
            raise AppError("convert_timeout", "LibreOffice took too long to convert this file.", 504,
                           "Try a smaller file, or raise OFFICE_TIMEOUT_SECONDS in .env.") from None
        produced = work_p / "out" / "input.pdf"
        if proc.returncode != 0 or not produced.exists():
            detail = (proc.stderr or proc.stdout or "")[-300:].strip()
            hint = detail or None
            if "could not be loaded" in detail:
                hint = ("The file may be corrupt, or LibreOffice is missing its Writer/Calc/Impress components "
                        "(apt install libreoffice-writer libreoffice-calc libreoffice-impress). " + detail)
            raise AppError("convert_failed", "LibreOffice could not convert this file.", 422, hint)
        shutil.move(str(produced), out)


def installed_ocr_languages() -> set[str]:
    try:
        r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=15)
        return {ln.strip() for ln in r.stdout.splitlines()[1:] if ln.strip()}
    except Exception:  # noqa: BLE001
        return set()


def ocr_pdf(src: Path, out: Path, languages: str, force: bool) -> None:
    if not shutil.which("tesseract"):
        raise ToolUnavailable("Tesseract OCR", "Install it (e.g. `apt install tesseract-ocr ghostscript`) and retry.")
    if not re.fullmatch(r"[a-z_]{3,12}(\+[a-z_]{3,12}){0,3}", languages):
        raise AppError("bad_language", "Language codes look like 'eng' or 'eng+deu'.", 422)
    missing = set(languages.split("+")) - installed_ocr_languages()
    if missing:
        raise AppError("language_missing", f"OCR language data not installed: {', '.join(sorted(missing))}.", 422,
                       "Install the matching tesseract-ocr-<lang> package.")
    settings = get_settings()
    cmd = [sys.executable, "-m", "ocrmypdf", "--force-ocr" if force else "--skip-text", "-l", languages,
           "--output-type", "pdf", "--optimize", "0", "--jobs", "2", "--quiet", str(src), str(out)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=settings.ocr_timeout_seconds)
    except subprocess.TimeoutExpired:
        raise AppError("ocr_timeout", "OCR took too long.", 504, "Split the document into smaller parts, or raise OCR_TIMEOUT_SECONDS.") from None
    if proc.returncode != 0 or not out.exists():
        raise AppError("ocr_failed", "OCR failed.", 422, (proc.stderr or proc.stdout or "")[-300:].strip() or None)
