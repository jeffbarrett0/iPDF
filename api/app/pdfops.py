"""Pure PDF transformations. Each function reads input paths and writes a new file; none mutates inputs."""
from __future__ import annotations

import io
import math
import re
import zlib
from contextlib import ExitStack
from pathlib import Path
from typing import Iterator

import pikepdf
import pypdfium2 as pdfium
from pikepdf import Array, Dictionary, Name, Pdf, Stream
from PIL import Image, ImageDraw

from . import geometry as geo
from .errors import AppError

MAX_PAGES_OUT = 2000
PRODUCER = "iPDF (pikepdf)"


# ---------------------------------------------------------------- helpers
def open_pdf(path: Path) -> Pdf:
    try:
        return pikepdf.open(path)
    except pikepdf.PasswordError:
        raise AppError("password_protected", "This PDF needs a password to open.", 422,
                       "Remove the password in another tool, then upload the unlocked copy.") from None
    except (pikepdf.PdfError, OSError) as exc:
        raise AppError("invalid_pdf", "This file is not a readable PDF.", 422, str(exc)[:200]) from None


def parse_pages(spec: str | list[int] | None, count: int, *, allow_empty_all: bool = True) -> list[int]:
    """'1-3,5,8-' or [1,2] -> ordered unique 1-based page numbers. Empty/'all' means every page."""
    if spec is None or (isinstance(spec, str) and spec.strip().lower() in ("", "all")):
        if allow_empty_all:
            return list(range(1, count + 1))
        raise AppError("bad_pages", "Specify which pages.", 422)
    if isinstance(spec, list):
        nums = [int(n) for n in spec]
    else:
        nums = []
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            m = re.fullmatch(r"(\d+)?\s*-\s*(\d+)?", part)
            if m and (m.group(1) or m.group(2)):
                a = int(m.group(1) or 1)
                b = int(m.group(2) or count)
                if a > b:
                    raise AppError("bad_pages", f"Range '{part}' runs backwards.", 422)
                nums.extend(range(a, b + 1))
            elif part.isdigit():
                nums.append(int(part))
            else:
                raise AppError("bad_pages", f"Cannot understand page selection '{part}'.", 422,
                               "Use forms like 1-3,5,8-")
    for n in nums:
        if not 1 <= n <= count:
            raise AppError("bad_pages", f"Page {n} is outside this document (1–{count}).", 422)
    seen: set[int] = set()
    out = []
    for n in nums:
        if n not in seen:
            seen.add(n)
            out.append(n)
    if not out:
        raise AppError("bad_pages", "No pages selected.", 422)
    return out


def _save(pdf: Pdf, out: Path, *, compress: bool = True) -> None:
    pdf.docinfo["/Producer"] = PRODUCER
    pdf.save(out, compress_streams=compress, object_stream_mode=pikepdf.ObjectStreamMode.generate,
             recompress_flate=False)


def page_count(path: Path) -> int:
    with open_pdf(path) as p:
        return len(p.pages)


# ---------------------------------------------------------------- merge / split / reorder / rotate
def merge(paths: list[Path], out: Path) -> None:
    if len(paths) < 2:
        raise AppError("need_two", "Select at least two files to merge.", 422)
    with ExitStack() as stack:
        dst = Pdf.new()
        stack.callback(dst.close)
        for p in paths:
            src = stack.enter_context(open_pdf(p))
            dst.pages.extend(src.pages)
        if len(dst.pages) > MAX_PAGES_OUT:
            raise AppError("too_many_pages", f"Merged file would exceed {MAX_PAGES_OUT} pages.", 422)
        _save(dst, out)


def select_pages(path: Path, pages: list[int], out: Path) -> None:
    """Write a new PDF with the given 1-based pages, in the given order (used by split and reorder)."""
    with open_pdf(path) as src, Pdf.new() as dst:
        for n in pages:
            dst.pages.append(src.pages[n - 1])
        _save(dst, out)


def split_plan(spec: str | None, mode: str, count: int, every: int | None) -> list[list[int]]:
    if mode == "each":
        groups = [[n] for n in range(1, count + 1)]
    elif mode == "every":
        if not every or every < 1:
            raise AppError("bad_split", "Give a chunk size of at least 1 page.", 422)
        groups = [list(range(s, min(s + every, count + 1))) for s in range(1, count + 1, every)]
    elif mode == "ranges":
        if not spec or not spec.strip():
            raise AppError("bad_split", "Give page ranges such as 1-3,4-6.", 422)
        groups = [parse_pages(part, count) for part in spec.split(",") if part.strip()]
    else:
        raise AppError("bad_split", "Unknown split mode.", 422)
    if len(groups) < 1 or len(groups) > 500:
        raise AppError("bad_split", "A split may produce between 1 and 500 files.", 422)
    return groups


def rotate(path: Path, pages: list[int], degrees: int, out: Path) -> None:
    if degrees not in (90, 180, 270, -90, -180, -270):
        raise AppError("bad_angle", "Rotation must be 90, 180 or 270 degrees.", 422)
    with open_pdf(path) as pdf:
        for n in pages:
            pdf.pages[n - 1].rotate(degrees, relative=True)
        _save(pdf, out)


# ---------------------------------------------------------------- compress
LEVELS = {  # level -> (max image long edge px, jpeg quality)
    "low": (None, None),
    "medium": (2000, 75),
    "high": (1200, 55),
}


def _recompress_images(pdf: Pdf, max_edge: int, quality: int) -> int:
    changed = 0
    seen: set[tuple[int, int]] = set()
    for obj in list(pdf.objects):
        try:
            if not isinstance(obj, Stream) or obj.get("/Subtype") != Name.Image:
                continue
            if obj.objgen in seen or "/SMask" in obj or "/Mask" in obj or obj.get("/ImageMask"):
                continue
            seen.add(obj.objgen)
            cs = obj.get("/ColorSpace")
            if cs not in (Name.DeviceRGB, Name.DeviceGray) and not (isinstance(cs, Array) and cs[0] == Name.ICCBased):
                continue
            before = len(obj.read_raw_bytes())
            img = pikepdf.PdfImage(obj).as_pil_image()
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            scale = min(1.0, max_edge / max(img.size))
            if scale < 1.0:
                img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) >= before * 0.9:
                continue
            obj.write(data, filter=Name.DCTDecode)
            obj.Width, obj.Height = img.width, img.height
            obj.ColorSpace = Name.DeviceGray if img.mode == "L" else Name.DeviceRGB
            obj.BitsPerComponent = 8
            for k in ("/DecodeParms", "/Decode"):
                if k in obj:
                    del obj[k]
            changed += 1
        except Exception:  # noqa: BLE001 - never fail a whole job over one odd image
            continue
    return changed


def compress(path: Path, level: str, out: Path) -> dict:
    if level not in LEVELS:
        raise AppError("bad_level", "Compression level must be low, medium or high.", 422)
    max_edge, quality = LEVELS[level]
    with open_pdf(path) as pdf:
        n = _recompress_images(pdf, max_edge, quality) if max_edge else 0
        pdf.docinfo["/Producer"] = PRODUCER
        pdf.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate,
                 recompress_flate=True)
    return {"images_recompressed": n, "bytes_before": path.stat().st_size, "bytes_after": out.stat().st_size}


# ---------------------------------------------------------------- stamping helpers
def _pdf_str(text: str) -> bytes:
    try:
        raw = text.encode("cp1252")
    except UnicodeEncodeError:
        raise AppError("unsupported_text", "Text may only use Latin characters (Windows-1252).", 422,
                       "Remove emoji or non-Latin letters.") from None
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _hex_color(value: str, default=(0.5, 0.5, 0.5)) -> tuple[float, float, float]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", value or "")
    if not m:
        return default
    h = m.group(1)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _font(pdf: Pdf, base: str) -> pikepdf.Object:
    return pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name("/" + base),
                                        Encoding=Name.WinAnsiEncoding))


def _approx_width(text: str, size: float, bold: bool = False) -> float:
    return len(text) * size * (0.58 if bold else 0.52)


def stamp_page(pdf: Pdf, page: pikepdf.Page, body: bytes, *, fonts: dict[str, pikepdf.Object] | None = None,
               gstates: dict[str, Dictionary] | None = None, xobjects: dict[str, pikepdf.Object] | None = None) -> None:
    """Append drawing commands in the *visible* frame on top of existing content, isolated by q/Q."""
    res = page.obj.get("/Resources")
    if res is None:
        page.obj.Resources = Dictionary()
        res = page.obj.Resources
    for key, items in (("/Font", fonts), ("/ExtGState", gstates), ("/XObject", xobjects)):
        if not items:
            continue
        if key not in res:
            res[key] = Dictionary()
        for name, obj in items.items():
            res[key][name] = obj
    m = geo.matrix_str(geo.visible_matrix(page))
    page.contents_add(Stream(pdf, b"q"), prepend=True)
    page.contents_add(Stream(pdf, b"Q\nq " + m.encode() + b" cm\n" + body + b"\nQ"), prepend=False)


# ---------------------------------------------------------------- watermark
def watermark(path: Path, text: str, pages: list[int] | None, opacity: float, size: float, angle: float,
              color: str, out: Path) -> None:
    text = (text or "").strip()
    if not text or len(text) > 80:
        raise AppError("bad_watermark", "Watermark text must be 1–80 characters.", 422)
    if not 0.05 <= opacity <= 1:
        raise AppError("bad_watermark", "Opacity must be between 0.05 and 1.", 422)
    if not 8 <= size <= 300:
        raise AppError("bad_watermark", "Font size must be between 8 and 300.", 422)
    if not -90 <= angle <= 90:
        raise AppError("bad_watermark", "Angle must be between -90 and 90 degrees.", 422)
    r, g, b = _hex_color(color)
    pdfstr = _pdf_str(text)
    with open_pdf(path) as pdf:
        font = _font(pdf, "Helvetica-Bold")
        gs = pdf.make_indirect(Dictionary(Type=Name.ExtGState, ca=opacity, CA=opacity))
        targets = pages or list(range(1, len(pdf.pages) + 1))
        for n in targets:
            page = pdf.pages[n - 1]
            vw, vh = geo.visible_size(page)
            rad = math.radians(angle)
            c, s = math.cos(rad), math.sin(rad)
            tw = _approx_width(text, size, True)
            # centre the text on the page: translate to centre then back by half the text box
            cx, cy = vw / 2, vh / 2
            tx = cx - (tw / 2) * c + (size * 0.35) * s
            ty = cy - (tw / 2) * s - (size * 0.35) * c
            body = (b"/IPDFGS gs " + f"{r:.3f} {g:.3f} {b:.3f} rg".encode() + b" BT /IPDFWM " +
                    f"{size:g} Tf {c:.5f} {s:.5f} {-s:.5f} {c:.5f} {tx:.2f} {ty:.2f} Tm ".encode() +
                    pdfstr + b" Tj ET")
            stamp_page(pdf, page, body, fonts={"/IPDFWM": font}, gstates={"/IPDFGS": gs})
        _save(pdf, out)


# ---------------------------------------------------------------- annotate
def _ap(pdf: Pdf, rect: list[float], body: bytes, res: Dictionary | None = None) -> pikepdf.Object:
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    d = Dictionary(Type=Name.XObject, Subtype=Name.Form, BBox=Array([0, 0, w, h]))
    if res is not None:
        d.Resources = res
    s = Stream(pdf, body)
    for k, v in d.items():
        s[k] = v
    return pdf.make_indirect(s)


def annotate(path: Path, items: list[dict], out: Path) -> int:
    if not items:
        raise AppError("no_annotations", "Add at least one annotation.", 422)
    if len(items) > 500:
        raise AppError("too_many", "At most 500 annotations per operation.", 422)
    with open_pdf(path) as pdf:
        font = _font(pdf, "Helvetica")
        gs_mul = pdf.make_indirect(Dictionary(Type=Name.ExtGState, BM=Name.Multiply))
        for it in items:
            kind = it.get("type")
            page_no = int(it.get("page", 0))
            if not 1 <= page_no <= len(pdf.pages):
                raise AppError("bad_page", f"Annotation page {page_no} is outside the document.", 422)
            page = pdf.pages[page_no - 1]
            x, y, w, h = geo.validate_frac_rect(it)
            rect = geo.frac_rect_to_user(page, x, y, w, h)
            rw, rh = rect[2] - rect[0], rect[3] - rect[1]
            text = str(it.get("text") or "")[:2000]
            color = _hex_color(it.get("color", ""), (1, 0.9, 0.2) if kind == "highlight" else (0, 0, 0))
            cs = f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f}".encode()
            base = Dictionary(Type=Name.Annot, Rect=Array(rect), F=4, C=Array(list(color)),
                              T=pikepdf.String("iPDF"), M=pikepdf.String("D:20000101000000Z"))
            if kind == "highlight":
                base.Subtype = Name.Highlight
                base.QuadPoints = Array([rect[0], rect[3], rect[2], rect[3], rect[0], rect[1], rect[2], rect[1]])
                base.Contents = pikepdf.String(text)
                res = Dictionary(ExtGState=Dictionary(G=gs_mul))
                base.AP = Dictionary(N=_ap(pdf, rect, b"/G gs " + cs + f" rg 0 0 {rw:.2f} {rh:.2f} re f".encode(), res))
            elif kind == "rect":
                base.Subtype = Name.Square
                base.Contents = pikepdf.String(text)
                base.BS = Dictionary(W=1.5)
                base.AP = Dictionary(N=_ap(pdf, rect, cs + f" RG 1.5 w 0.75 0.75 {rw-1.5:.2f} {rh-1.5:.2f} re S".encode()))
            elif kind == "note":
                if not text:
                    raise AppError("empty_note", "A note needs text.", 422)
                base.Subtype = Name.Text
                base.Name = Name.Note
                base.Contents = pikepdf.String(text)
                base.Rect = Array([rect[0], rect[3] - 20, rect[0] + 20, rect[3]])
                base.AP = Dictionary(N=_ap(pdf, [0, 0, 20, 20],
                                           b"1 0.9 0.2 rg 0 0 20 20 re f 0.4 0.3 0 RG 1 w 0.5 0.5 19 19 re S"))
            elif kind == "text":
                if not text:
                    raise AppError("empty_text", "Text annotation needs text.", 422)
                size = float(it.get("size", 12))
                if not 6 <= size <= 72:
                    raise AppError("bad_size", "Text size must be between 6 and 72.", 422)
                base.Subtype = Name.FreeText
                base.Contents = pikepdf.String(text)
                base.DA = pikepdf.String(f"/Helv {size:g} Tf {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg")
                lines = _wrap(text, rw, size)
                ops = [cs + b" rg BT /Helv " + f"{size:g} Tf {size * 1.2:g} TL 2 {rh - size:.2f} Td".encode()]
                for ln in lines:
                    ops.append(_pdf_str(ln) + b" Tj T*")
                ops.append(b"ET")
                res = Dictionary(Font=Dictionary(Helv=font))
                base.DR = res
                base.AP = Dictionary(N=_ap(pdf, rect, b"\n".join(ops), res))
            else:
                raise AppError("bad_annotation", f"Unknown annotation type '{kind}'.", 422,
                               "Use highlight, note, text or rect.")
            annots = page.obj.get("/Annots")
            if annots is None:
                page.obj.Annots = Array()
                annots = page.obj.Annots
            annots.append(pdf.make_indirect(base))
        _save(pdf, out)
    return len(items)


def _wrap(text: str, width: float, size: float) -> list[str]:
    max_chars = max(1, int((width - 4) / (size * 0.5)))
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        cur = ""
        for word in para.split(" "):
            if len(cur) + len(word) + (1 if cur else 0) <= max_chars:
                cur = f"{cur} {word}".strip()
            else:
                if cur:
                    lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


# ---------------------------------------------------------------- redact
def _to_visible(rot: int, vw: float, vh: float, ux: float, uy: float) -> tuple[float, float]:
    """Unrotated page-frame point -> visible-frame point (both y-up). vw/vh are the visible page size."""
    if rot == 90:
        return uy, vh - ux
    if rot == 180:
        return vw - ux, vh - uy
    if rot == 270:
        return vw - uy, ux
    return ux, uy


def find_text_regions(path: Path, terms: list[str]) -> list[dict]:
    """Locate case-insensitive occurrences of `terms`; returns fractional regions on the visible page."""
    regions: list[dict] = []
    doc = pdfium.PdfDocument(str(path))
    try:
        for i in range(len(doc)):
            page = doc[i]
            pw, ph = page.get_size()  # visible size (rotation applied)
            rot = page.get_rotation() % 360
            ox, oy = page.get_cropbox()[:2]
            tp = page.get_textpage()
            for term in terms:
                searcher = tp.search(term, match_case=False)
                while True:
                    hit = searcher.get_next()
                    if not hit:
                        break
                    idx, cnt = hit
                    for j in range(tp.count_rects(idx, cnt)):
                        l, b, r, t = tp.get_rect(j)
                        l, r, b, t = l - ox, r - ox, b - oy, t - oy
                        # pdfium reports text boxes in the unrotated page frame; convert to the visible frame.
                        (l, b), (r, t) = _to_visible(rot, pw, ph, l, b), _to_visible(rot, pw, ph, r, t)
                        l, r, b, t = min(l, r), max(l, r), min(b, t), max(b, t)
                        pad = 1.5
                        l, b, r, t = max(0, l - pad), max(0, b - pad), min(pw, r + pad), min(ph, t + pad)
                        regions.append({"page": i + 1, "x": l / pw, "y": 1 - t / ph,
                                        "w": max(0.0005, (r - l) / pw), "h": max(0.0005, (t - b) / ph), "term": term})
            tp.close()
            page.close()
    finally:
        doc.close()
    return regions


def redact(path: Path, regions: list[dict], out: Path, dpi: int = 200) -> dict:
    if not regions:
        raise AppError("no_regions", "Nothing to redact: draw a region or enter text to find.", 422)
    if not 72 <= dpi <= 400:
        raise AppError("bad_dpi", "DPI must be between 72 and 400.", 422)
    by_page: dict[int, list[tuple[float, float, float, float]]] = {}
    for r in regions:
        by_page.setdefault(int(r["page"]), []).append(geo.validate_frac_rect(r))
    with open_pdf(path) as src, Pdf.new() as dst:
        total = len(src.pages)
        for n in by_page:
            if not 1 <= n <= total:
                raise AppError("bad_page", f"Region page {n} is outside the document.", 422)
        doc = pdfium.PdfDocument(str(path))
        try:
            for n in range(1, total + 1):
                if n not in by_page:
                    dst.pages.append(src.pages[n - 1])
                    continue
                page = doc[n - 1]
                pw, ph = page.get_size()
                img = page.render(scale=dpi / 72).to_pil().convert("RGB")
                draw = ImageDraw.Draw(img)
                for (x, y, w, h) in by_page[n]:
                    draw.rectangle([x * img.width, y * img.height, (x + w) * img.width, (y + h) * img.height], fill=(0, 0, 0))
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=88)
                dst.add_blank_page(page_size=(pw, ph))
                newp = dst.pages[-1]
                xo = Stream(dst, buf.getvalue())
                xo.Type, xo.Subtype = Name.XObject, Name.Image
                xo.Width, xo.Height = img.width, img.height
                xo.ColorSpace, xo.BitsPerComponent, xo.Filter = Name.DeviceRGB, 8, Name.DCTDecode
                newp.Resources = Dictionary(XObject=Dictionary(Im0=dst.make_indirect(xo)))
                newp.Contents = Stream(dst, f"q {pw:.3f} 0 0 {ph:.3f} 0 0 cm /Im0 Do Q".encode())
                page.close()
        finally:
            doc.close()
        # New document: no Info/XMP/attachments/outlines are carried over.
        dst.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return {"redacted_pages": sorted(by_page), "regions": sum(len(v) for v in by_page.values())}


# ---------------------------------------------------------------- text helper (certificate pages, tests)
def extract_text(path: Path, page_index: int) -> str:
    doc = pdfium.PdfDocument(str(path))
    try:
        tp = doc[page_index].get_textpage()
        try:
            return tp.get_text_range()
        finally:
            tp.close()
    finally:
        doc.close()
