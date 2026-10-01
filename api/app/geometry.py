"""Map coordinates between the *visible* page (what a user sees and clicks on) and PDF user space.

All public API coordinates are fractions (0..1) of the visible page with the origin at the top-left.
"""
from __future__ import annotations

import pikepdf


def _inherited(page: pikepdf.Page, key: str, default=None):
    node = page.obj
    while node is not None:
        if key in node:
            return node[key]
        node = node.get("/Parent")
    return default


def page_box(page: pikepdf.Page) -> tuple[float, float, float, float]:
    box = _inherited(page, "/CropBox") or _inherited(page, "/MediaBox") or [0, 0, 612, 792]
    x0, y0, x1, y1 = (float(v) for v in box)
    return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)


def page_rotation(page: pikepdf.Page) -> int:
    return int(_inherited(page, "/Rotate", 0)) % 360


def visible_size(page: pikepdf.Page) -> tuple[float, float]:
    _, _, bw, bh = page_box(page)
    return (bh, bw) if page_rotation(page) in (90, 270) else (bw, bh)


def visible_matrix(page: pikepdf.Page) -> tuple[float, float, float, float, float, float]:
    """Matrix taking visible-frame points (origin bottom-left, y up) to user space."""
    x0, y0, bw, bh = page_box(page)
    rot = page_rotation(page)
    if rot == 0:
        return (1, 0, 0, 1, x0, y0)
    if rot == 90:
        return (0, 1, -1, 0, x0 + bw, y0)
    if rot == 180:
        return (-1, 0, 0, -1, x0 + bw, y0 + bh)
    return (0, -1, 1, 0, x0, y0 + bh)


def apply(m, vx: float, vy: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * vx + c * vy + e, b * vx + d * vy + f


def frac_rect_to_visible(page: pikepdf.Page, x: float, y: float, w: float, h: float):
    """Fractional top-left rect -> (left, bottom, width, height) in the visible frame (y up)."""
    vw, vh = visible_size(page)
    return x * vw, (1 - y - h) * vh, w * vw, h * vh


def frac_rect_to_user(page: pikepdf.Page, x: float, y: float, w: float, h: float) -> list[float]:
    l, b, rw, rh = frac_rect_to_visible(page, x, y, w, h)
    m = visible_matrix(page)
    pts = [apply(m, l, b), apply(m, l + rw, b), apply(m, l, b + rh), apply(m, l + rw, b + rh)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def matrix_str(m) -> str:
    return " ".join(f"{v:.6f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v)) for v in m)


def validate_frac_rect(r: dict) -> tuple[float, float, float, float]:
    from .errors import AppError

    try:
        x, y, w, h = (float(r[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        raise AppError("bad_rect", "Each region needs numeric x, y, w, h.", 422) from None
    if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1) or x + w > 1.0001 or y + h > 1.0001:
        raise AppError("bad_rect", "Regions must lie inside the page (values between 0 and 1).", 422)
    return x, y, w, h
