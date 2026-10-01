"""Inspect a PDF for features that transformations may alter, and turn them into warnings."""
from __future__ import annotations

from pathlib import Path

import pikepdf
from pikepdf import Name

STANDARD_14 = {
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique", "Symbol", "ZapfDingbats",
}


def _is_sig_field(obj) -> bool:
    try:
        return obj.get("/FT") == Name.Sig
    except Exception:  # noqa: BLE001
        return False


def analyze_pdf(pdf: pikepdf.Pdf) -> dict:
    root = pdf.Root
    has_forms = False
    has_sigs = False
    acro = root.get("/AcroForm")
    if acro is not None:
        fields = acro.get("/Fields", [])
        has_forms = len(fields) > 0
        for f in fields:
            if _is_sig_field(f):
                has_sigs = True
    if "/Perms" in root:
        has_sigs = True

    nonembedded: set[str] = set()
    seen: set[tuple[int, int]] = set()
    for page in pdf.pages:
        try:
            fonts = page.get("/Resources", {}).get("/Font", {})
        except Exception:  # noqa: BLE001
            continue
        for _, font in fonts.items():
            try:
                key = font.objgen
                if key != (0, 0):
                    if key in seen:
                        continue
                    seen.add(key)
                subtype = str(font.get("/Subtype", ""))
                if subtype == "/Type3":
                    continue
                base = str(font.get("/BaseFont", "unknown")).lstrip("/")
                base = base.split("+", 1)[-1]
                target = font
                if subtype == "/Type0":
                    desc = font.get("/DescendantFonts")
                    target = desc[0] if desc else font
                fd = target.get("/FontDescriptor")
                embedded = fd is not None and any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3"))
                if not embedded and base not in STANDARD_14:
                    nonembedded.add(base)
            except Exception:  # noqa: BLE001
                continue

    return {
        "encrypted": bool(pdf.is_encrypted),
        "has_forms": has_forms,
        "has_signatures": has_sigs,
        "nonembedded_fonts": sorted(nonembedded)[:20],
        "has_javascript": "/JavaScript" in (root.get("/Names") or {}) or "/OpenAction" in root,
        "has_attachments": "/EmbeddedFiles" in (root.get("/Names") or {}),
        "page_count": len(pdf.pages),
    }


def analyze_path(path: Path) -> dict:
    with pikepdf.open(path) as pdf:
        return analyze_pdf(pdf)


def _w(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


def warnings_for(operation: str, analyses: list[dict]) -> list[dict]:
    """Warnings a user should see *before* running `operation` on inputs with these analyses."""
    out: list[dict] = []

    def any_(key: str) -> bool:
        return any(a.get(key) for a in analyses)

    rasterizes = operation in {"redact"}
    if any_("has_signatures"):
        out.append(_w("signatures", "warning",
            "This PDF contains digital signatures. Any change produces a new file whose signatures "
            "are no longer valid; the untouched original keeps them."))
    if any_("encrypted"):
        out.append(_w("encryption", "warning",
            "This PDF is encrypted (opened with an empty password). The output is written without "
            "encryption or permission restrictions."))
    if any_("has_forms"):
        if operation in {"merge", "split", "reorder"}:
            out.append(_w("forms", "warning",
                "Interactive form fields may be dropped, renamed or stop being fillable when pages "
                "are copied into a new document."))
        elif rasterizes:
            out.append(_w("forms", "warning", "Form fields on redacted pages are flattened into the page image."))
        elif operation == "ocr":
            out.append(_w("forms", "info", "Form fields are kept, but OCR may rewrite page content."))
        else:
            out.append(_w("forms", "info", "Form fields are preserved but not re-validated."))
    fonts = sorted({f for a in analyses for f in a.get("nonembedded_fonts", [])})
    if fonts and operation != "convert":
        out.append(_w("fonts", "warning",
            "Fonts not embedded in the PDF (" + ", ".join(fonts[:5]) + ("…" if len(fonts) > 5 else "") +
            "): other viewers, and the redaction/OCR rasterizer, may substitute different fonts."))
    if operation == "convert":
        out.append(_w("fonts", "info",
            "LibreOffice substitutes fonts it does not have installed; layout may differ from Word. "
            "Check the preview."))
    if operation == "redact":
        out.append(_w("redact", "info",
            "Redacted pages are rasterized (text on them becomes an image and is no longer selectable). "
            "Text on pages you did not redact is untouched. Metadata and attachments are stripped."))
    if operation == "compress" and any_("has_signatures"):
        pass
    if any_("has_javascript") and operation in {"merge", "split", "reorder"}:
        out.append(_w("javascript", "info", "Document-level scripts or open actions are not carried over."))
    return out
