from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

_BAD = re.compile(r"[^A-Za-z0-9._-]+")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
}


def safe_stem(name: str, fallback: str = "document", max_len: int = 80) -> str:
    """Reduce an arbitrary user-supplied name to a filesystem-safe stem."""
    name = name.replace("\\", "/").split("/")[-1]  # drop any path components
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem = _BAD.sub("_", stem).strip("._-")
    stem = re.sub(r"_+", "_", stem)[:max_len].strip("._-")
    if not stem or stem.lower() in _WINDOWS_RESERVED:
        stem = fallback
    return stem


def safe_filename(name: str, ext: str, fallback: str = "document") -> str:
    ext = ext.lower().lstrip(".")
    ext = re.sub(r"[^a-z0-9]", "", ext)[:8] or "bin"
    return f"{safe_stem(name, fallback)}.{ext}"


def extension_of(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1]
    return base.rsplit(".", 1)[1].lower() if "." in base else ""


def content_disposition(filename: str) -> str:
    ascii_name = safe_filename(filename, extension_of(filename) or "bin")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"
