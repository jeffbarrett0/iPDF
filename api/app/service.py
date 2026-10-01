"""Operation orchestration: validate inputs, run a transformation, commit a new immutable version."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import logging

from . import analysis, events, pdfops, store, tools
from .db import connection
from .errors import AppError
from .safenames import safe_stem

log = logging.getLogger("ipdf.service")
OPERATIONS = {"merge", "split", "reorder", "rotate", "compress", "watermark", "redact", "annotate", "ocr", "convert"}


def _load_sources(file_ids: list[str], op: str) -> list[dict]:
    if not file_ids:
        raise AppError("no_input", "Choose a file first.", 422)
    if len(file_ids) > 50:
        raise AppError("too_many_inputs", "At most 50 files per operation.", 422)
    rows = []
    with connection() as conn:
        for fid in file_ids:
            rows.append(store.get_file(conn, fid))
    for r in rows:
        is_pdf = r["mime"] == "application/pdf"
        if op == "convert" and is_pdf:
            raise AppError("already_pdf", f"{r['display_name']} is already a PDF.", 422)
        if op != "convert" and not is_pdf:
            raise AppError("not_pdf", f"{r['display_name']} is not a PDF yet.", 422, "Convert it to PDF first.")
        v = store.verify_file(r)
        if not v["ok"]:
            raise AppError("integrity", f"{r['display_name']} on disk no longer matches its recorded hash.", 409,
                           "The stored file was modified outside the app. Restore it from a backup.")
    return rows


def preflight(op: str, file_ids: list[str]) -> list[dict]:
    if op not in OPERATIONS:
        raise AppError("bad_operation", "Unknown operation.", 404)
    with connection() as conn:
        rows = [store.get_file(conn, f) for f in file_ids]
    return analysis.warnings_for(op, [r["analysis"] for r in rows])


def _tmp_pdf() -> Path:
    d = store.tmp_dir()
    f = tempfile.NamedTemporaryFile(dir=d, suffix=".pdf", delete=False)
    f.close()
    return Path(f.name)


def _commit(tmp: Path, op: str, params: dict, sources: list[dict], warnings: list, hint: str | None = None,
            extra_event: dict | None = None) -> dict:
    try:
        an = analysis.analyze_path(tmp)
        return store.add_output(tmp, operation=op, params=params, sources=sources, page_count=an["page_count"],
                                analysis=an, warnings=warnings, name_hint=hint, extra_event=extra_event)
    finally:
        tmp.unlink(missing_ok=True)


def run(op: str, file_ids: list[str], params: dict[str, Any]) -> list[dict]:
    if op not in OPERATIONS:
        raise AppError("bad_operation", "Unknown operation.", 404)
    try:
        return _run(op, file_ids, params or {})
    except AppError as exc:
        _log_failure(op, file_ids, params, exc.code, exc.message)
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("operation %s failed", op)
        _log_failure(op, file_ids, params, "internal", repr(exc)[:300])
        raise AppError("operation_failed", "The operation failed unexpectedly. Your original is unchanged.", 500,
                       "Check the API log for details, then retry.") from exc


def _log_failure(op: str, file_ids: list, params: dict, code: str, message: str) -> None:
    try:
        with connection() as conn:
            events.append(conn, "op.failed", outcome="error",
                          details={"operation": op, "files": [str(f) for f in file_ids], "code": code, "message": message})
    except Exception:  # noqa: BLE001
        pass


def _run(op: str, file_ids: list[str], p: dict) -> list[dict]:
    sources = _load_sources(file_ids, op)
    warnings = analysis.warnings_for(op, [s["analysis"] for s in sources])
    paths = [store.path_of(s) for s in sources]
    first, first_path = sources[0], paths[0]
    count = first["page_count"] or 0
    if op != "merge" and op != "convert" and len(sources) != 1:
        raise AppError("one_file", f"'{op}' works on one file at a time.", 422)

    if op == "merge":
        out = _tmp_pdf()
        pdfops.merge(paths, out)
        return [_commit(out, op, {"order": [str(s["id"]) for s in sources]}, sources, warnings, "merged")]

    if op == "split":
        groups = pdfops.split_plan(p.get("ranges"), p.get("mode", "ranges"), count, p.get("every"))
        results = []
        for g in groups:
            out = _tmp_pdf()
            pdfops.select_pages(first_path, g, out)
            label = f"p{g[0]}" if len(g) == 1 else f"p{g[0]}-{g[-1]}"
            results.append(_commit(out, op, {"pages": g}, sources, warnings, f"{safe_stem(first['display_name'])}-{label}"))
        return results

    if op == "reorder":
        order = p.get("order")
        if not isinstance(order, list) or not order:
            raise AppError("bad_order", "Give the new page order, e.g. [3,1,2].", 422)
        pages = pdfops.parse_pages([int(x) for x in order], count)
        if len(pages) != len(order):
            raise AppError("bad_order", "A page can appear only once.", 422)
        extra = []
        if len(pages) < count:
            extra = [{"code": "pages_omitted", "severity": "warning",
                      "message": f"{count - len(pages)} page(s) were left out of the new order."}]
        out = _tmp_pdf()
        pdfops.select_pages(first_path, pages, out)
        return [_commit(out, op, {"order": pages}, sources, warnings + extra)]

    if op == "rotate":
        pages = pdfops.parse_pages(p.get("pages"), count)
        degrees = int(p.get("degrees", 90))
        out = _tmp_pdf()
        pdfops.rotate(first_path, pages, degrees, out)
        return [_commit(out, op, {"pages": pages, "degrees": degrees}, sources, warnings)]

    if op == "compress":
        level = p.get("level", "medium")
        out = _tmp_pdf()
        stats = pdfops.compress(first_path, level, out)
        extra = []
        if stats["bytes_after"] >= stats["bytes_before"]:
            extra = [{"code": "no_gain", "severity": "info",
                      "message": "This file did not get smaller; it was probably already optimised."}]
        return [_commit(out, op, {"level": level, **stats}, sources, warnings + extra, extra_event=stats)]

    if op == "watermark":
        pages = pdfops.parse_pages(p.get("pages"), count)
        params = {"text": p.get("text", ""), "pages": pages, "opacity": float(p.get("opacity", 0.25)),
                  "size": float(p.get("size", 64)), "angle": float(p.get("angle", 45)), "color": p.get("color", "#888888")}
        out = _tmp_pdf()
        pdfops.watermark(first_path, params["text"], pages, params["opacity"], params["size"], params["angle"],
                         params["color"], out)
        return [_commit(out, op, params, sources, warnings)]

    if op == "redact":
        regions = list(p.get("regions") or [])
        terms = [t.strip() for t in (p.get("terms") or []) if t and t.strip()]
        found = []
        if terms:
            found = pdfops.find_text_regions(first_path, terms)
            if not found and not regions:
                raise AppError("no_match", "None of those terms were found in the text layer.", 422,
                               "Scanned pages have no text layer: run OCR first, or draw regions by hand.")
        out = _tmp_pdf()
        stats = pdfops.redact(first_path, regions + found, out, int(p.get("dpi", 200)))
        # Record that redactions were applied, but never log the redacted terms' content beyond what the user typed.
        return [_commit(out, op, {"terms": terms, "manual_regions": len(regions), **stats}, sources, warnings)]

    if op == "annotate":
        n = pdfops.annotate(first_path, list(p.get("annotations") or []), _out := _tmp_pdf())
        return [_commit(_out, op, {"count": n}, sources, warnings)]

    if op == "ocr":
        langs = str(p.get("languages") or "eng")
        out = _tmp_pdf()
        tools.ocr_pdf(first_path, out, langs, bool(p.get("force", False)))
        return [_commit(out, op, {"languages": langs, "force": bool(p.get("force", False))}, sources, warnings)]

    if op == "convert":
        results = []
        for s, path in zip(sources, paths):
            out = _tmp_pdf()
            tools.office_to_pdf(path, out)
            results.append(_commit(out, op, {"from": s["display_name"]}, [s], warnings,
                                   hint=s["display_name"]))
        return results
    raise AppError("bad_operation", "Unknown operation.", 404)
