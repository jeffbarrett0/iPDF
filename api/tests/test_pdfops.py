import io

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from app import pdfops
from app.errors import AppError
from app.safenames import safe_filename, safe_stem


def text_of(path, i):
    return pdfops.extract_text(path, i)


def pixel(path, page, fx, fy, scale=1):
    doc = pdfium.PdfDocument(str(path))
    img = doc[page].render(scale=scale).to_pil().convert("RGB")
    return img.getpixel((int(fx * img.width), int(fy * img.height)))


def test_merge_preserves_order_and_inputs(pdf_factory, tmp_path):
    a = pdf_factory("a.pdf", ["alpha one", "alpha two"])
    b = pdf_factory("b.pdf", ["bravo one"])
    before = a.read_bytes()
    out = tmp_path / "m.pdf"
    pdfops.merge([a, b], out)
    assert pdfops.page_count(out) == 3
    assert [text_of(out, i).strip() for i in range(3)] == ["alpha one", "alpha two", "bravo one"]
    assert a.read_bytes() == before  # inputs untouched


def test_merge_needs_two(pdf_factory, tmp_path):
    with pytest.raises(AppError):
        pdfops.merge([pdf_factory("a.pdf", ["x"])], tmp_path / "o.pdf")


def test_split_plan_and_select(pdf_factory, tmp_path):
    src = pdf_factory("s.pdf", [f"page {i}" for i in range(1, 6)])
    assert pdfops.split_plan("1-2,4", "ranges", 5, None) == [[1, 2], [4]]
    assert pdfops.split_plan(None, "every", 5, 2) == [[1, 2], [3, 4], [5]]
    assert len(pdfops.split_plan(None, "each", 5, None)) == 5
    out = tmp_path / "o.pdf"
    pdfops.select_pages(src, [4, 2], out)
    assert [text_of(out, i).strip() for i in range(2)] == ["page 4", "page 2"]


@pytest.mark.parametrize("spec,expected", [("1-3,5", [1, 2, 3, 5]), ("3-", [3, 4, 5]), ("all", [1, 2, 3, 4, 5]), ("2,2,1", [2, 1])])
def test_parse_pages(spec, expected):
    assert pdfops.parse_pages(spec, 5) == expected


@pytest.mark.parametrize("spec", ["0", "6", "3-1", "a-b", "1;2"])
def test_parse_pages_rejects(spec):
    with pytest.raises(AppError):
        pdfops.parse_pages(spec, 5)


def test_rotate(pdf_factory, tmp_path):
    src = pdf_factory("r.pdf", ["a", "b"])
    out = tmp_path / "o.pdf"
    pdfops.rotate(src, [2], 90, out)
    with pikepdf.open(out) as pdf:
        assert int(pdf.pages[0].obj.get("/Rotate", 0)) == 0
        assert int(pdf.pages[1].obj.get("/Rotate", 0)) == 90
    with pytest.raises(AppError):
        pdfops.rotate(src, [1], 45, out)


def test_compress_shrinks_image_pdf(tmp_path):
    import os

    img = Image.frombytes("RGB", (1600, 1600), os.urandom(1600 * 1600 * 3))
    src = tmp_path / "big.pdf"
    img.save(src, "PDF", resolution=100)
    out = tmp_path / "small.pdf"
    stats = pdfops.compress(src, "high", out)
    assert stats["images_recompressed"] == 1
    assert out.stat().st_size < src.stat().st_size * 0.5
    assert pdfops.page_count(out) == 1


def test_watermark_draws_on_page_including_rotated(pdf_factory, tmp_path):
    for rot in (0, 90):
        src = pdf_factory(f"w{rot}.pdf", ["hello"], rotate=rot)
        out = tmp_path / f"wo{rot}.pdf"
        pdfops.watermark(src, "DRAFT", None, 1.0, 120, 0, "#ff0000", out)
        doc = pdfium.PdfDocument(str(out))
        img = doc[0].render(scale=1).to_pil().convert("RGB")
        px = [(d[i], d[i + 1], d[i + 2]) for d in [img.tobytes()] for i in range(0, len(d), 3)]
        reds = sum(1 for p in px if p[0] > 200 and p[1] < 80 and p[2] < 80)
        assert reds > 500, f"watermark not visible for rotation {rot}"
        # centred: the red pixels' centroid is near the page centre
        xs = [i % img.width for i, p in enumerate(px) if p[0] > 200 and p[1] < 80 and p[2] < 80]
        assert abs(sum(xs) / len(xs) - img.width / 2) < img.width * 0.15
    with pytest.raises(AppError):
        pdfops.watermark(src, "emoji 😀", None, 0.5, 40, 0, "#000000", out)


def test_redact_removes_text_for_real(pdf_factory, tmp_path):
    src = pdf_factory("x.pdf", ["TOPSECRET number", "public page"])
    regions = pdfops.find_text_regions(src, ["topsecret"])
    assert len(regions) == 1 and regions[0]["page"] == 1
    out = tmp_path / "red.pdf"
    stats = pdfops.redact(src, regions, out)
    assert stats["redacted_pages"] == [1]
    assert "TOPSECRET" not in text_of(out, 0)
    assert "TOPSECRET" not in out.read_bytes().decode("latin1")
    assert text_of(out, 1).strip() == "public page"  # untouched pages keep their text
    r = regions[0]
    r, g, b = pixel(out, 0, r["x"] + r["w"] / 2, r["y"] + r["h"] / 2)
    assert (r, g, b) == (0, 0, 0)


@pytest.mark.parametrize("rot", [90, 180, 270])
def test_redact_search_on_rotated_page_lands_on_text(pdf_factory, tmp_path, rot):
    src = pdf_factory("rot.pdf", ["FINDME"], rotate=rot)
    regions = pdfops.find_text_regions(src, ["FINDME"])
    out = tmp_path / "o.pdf"
    pdfops.redact(src, regions, out)
    assert "FINDME" not in text_of(out, 0)
    # the page still shows black where the word was (i.e. the box was in the right place)
    r = regions[0]
    assert pixel(out, 0, r["x"] + r["w"] / 2, r["y"] + r["h"] / 2) == (0, 0, 0)


def test_annotate_highlight_and_text(pdf_factory, tmp_path):
    src = pdf_factory("n.pdf", ["note me"])
    out = tmp_path / "o.pdf"
    n = pdfops.annotate(src, [
        {"type": "highlight", "page": 1, "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.05, "color": "#00ff00"},
        {"type": "text", "page": 1, "x": 0.5, "y": 0.5, "w": 0.3, "h": 0.1, "text": "hi"},
        {"type": "note", "page": 1, "x": 0.8, "y": 0.8, "w": 0.1, "h": 0.1, "text": "remember"},
    ], out)
    assert n == 3
    with pikepdf.open(out) as pdf:
        assert {str(a.Subtype) for a in pdf.pages[0].Annots} == {"/Highlight", "/FreeText", "/Text"}
    r, g, b = pixel(out, 0, 0.2, 0.125)
    assert g > 200 and r < 80
    with pytest.raises(AppError):
        pdfops.annotate(src, [{"type": "banana", "page": 1, "x": 0, "y": 0, "w": 0.1, "h": 0.1}], out)


def test_safe_filenames():
    assert safe_filename("../../etc/passwd", "pdf") == "passwd.pdf"
    assert safe_filename("Rapport final (v2) é.PDF", "pdf") == "Rapport_final_v2_e.pdf"
    assert safe_stem("CON") == "document"
    assert safe_stem("....") == "document"
    assert safe_stem("a" * 500).__len__() <= 80


def test_redact_search_with_offset_mediabox(pdf_factory, tmp_path):
    src = pdf_factory("off.pdf", ["FINDME"])
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[0].MediaBox = [100, 100, 712, 892]
        pdf.pages[0].Contents = pikepdf.Stream(pdf, b"1 0 0 1 100 100 cm " + pdf.pages[0].Contents.read_bytes())
        pdf.save(src)
    regions = pdfops.find_text_regions(src, ["FINDME"])
    out = tmp_path / "o.pdf"
    pdfops.redact(src, regions, out)
    r = regions[0]
    assert pixel(out, 0, r["x"] + r["w"] / 2, r["y"] + r["h"] / 2) == (0, 0, 0)
