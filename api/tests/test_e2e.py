"""End-to-end happy path through the real HTTP API, PostgreSQL, LibreOffice and Tesseract (when installed)."""
import base64
import hashlib
import io
import re
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from conftest import make_pdf
import pypdfium2 as pdfium


@pytest.fixture(scope="module")
def client(environment):
    from app.main import app

    with TestClient(app) as c:
        yield c


def up(client, name, data, ctype="application/pdf"):
    r = client.post("/api/files", files={"file": (name, data, ctype)})
    assert r.status_code == 201, r.text
    return r.json()


def op(client, name, ids, **params):
    r = client.post(f"/api/ops/{name}", json={"file_ids": ids, "params": params})
    assert r.status_code == 200, r.text
    return r.json()["outputs"]


def dark_pixels(pdf_bytes, page, rect, scale=1.5):
    doc = pdfium.PdfDocument(pdf_bytes)
    img = doc[page].render(scale=scale).to_pil().convert("L")
    x, y, w, h = rect
    box = img.crop((int(x * img.width), int(y * img.height), int((x + w) * img.width), int((y + h) * img.height)))
    return sum(1 for v in box.tobytes() if v < 128)


def drawn_png():
    img = Image.new("RGBA", (200, 80), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([10, 10, 190, 70], fill=(0, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def sign_document(client, file_id, page=1):
    req = client.post("/api/sign/requests", json={"file_id": file_id, "title": "Test agreement"}).json()
    rid = req["id"]
    field = {"page": page, "x": 0.1, "y": 0.5, "w": 0.4, "h": 0.12}
    layout = {"signers": [{"name": "Ada Signer", "email": "ada@example.com"}, {"name": "Bob Signer", "email": "bob@example.com"}],
              "fields": [{**field, "signer": 0, "kind": "signature"},
                         {**field, "signer": 0, "y": 0.7, "h": 0.04, "kind": "date"},
                         {**field, "signer": 1, "y": 0.3, "kind": "signature"}]}
    assert client.put(f"/api/sign/requests/{rid}", json=layout).status_code == 200
    sent = client.post(f"/api/sign/requests/{rid}/send").json()
    assert {i["delivery"] for i in sent["invitations"]} == {"not_configured"}  # no SMTP in tests -> link to share
    tokens = [i["link"].rsplit("/", 1)[1] for i in sent["invitations"]]
    # Fields are frozen once invitations go out.
    assert client.put(f"/api/sign/requests/{rid}", json=layout).status_code == 409
    for n, token in enumerate(tokens):
        info = client.get(f"/api/public/sign/{token}").json()
        mine = [f for f in info["fields"] if f["mine"]]
        # Signing before consent is refused.
        assert client.post(f"/api/public/sign/{token}/sign", json={"values": {}}).status_code == 409
        assert client.post(f"/api/public/sign/{token}/consent", json={"agree": False}).status_code == 422
        assert client.post(f"/api/public/sign/{token}/consent", json={"agree": True}).status_code == 200
        values = {}
        for f in mine:
            if f["kind"] == "signature":
                values[f["id"]] = {"mode": "drawn", "data_url": drawn_png()} if n == 0 else {"mode": "typed", "text": "Bob Signer"}
        assert client.post(f"/api/public/sign/{token}/sign", json={"values": {}}).status_code == 422  # missing signature
        assert client.post(f"/api/public/sign/{token}/sign", json={"values": values}).status_code == 200
        assert client.post(f"/api/public/sign/{token}/sign", json={"values": values}).status_code == 409  # only once
    return client.get(f"/api/sign/requests/{rid}").json()


def test_happy_path(client):
    health = client.get("/api/health").json()
    assert health["database"] is True

    # --- upload (originals are stored immutable)
    a = up(client, "Contract A.pdf", make_pdf(["alpha one", "alpha two"]))["file"]
    b = up(client, "b.pdf", make_pdf(["bravo one"]))["file"]
    assert a["version"] == 0 and a["page_count"] == 2
    assert client.get("/api/files").json()[0]["kind"] == "original"

    # --- preview + preflight warnings
    png = client.get(f"/api/files/{a['id']}/pages/1/preview?width=320")
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get(f"/api/files/{a['id']}/pages/9/preview").status_code == 404
    assert client.post("/api/ops/merge/preflight", json={"file_ids": [a["id"], b["id"]]}).status_code == 200

    # --- merge -> versioned output, original untouched
    merged = op(client, "merge", [a["id"], b["id"]])[0]
    assert merged["page_count"] == 3 and merged["version"] == 1
    assert merged["display_name"].startswith("v001-merge-") and merged["source_ids"] == [a["id"], b["id"]]
    dl = client.get(f"/api/files/{merged['id']}/download")
    assert hashlib.sha256(dl.content).hexdigest() == merged["sha256"]
    assert "attachment" in dl.headers["content-disposition"]
    orig_dl = client.get(f"/api/files/{a['id']}/download")
    assert hashlib.sha256(orig_dl.content).hexdigest() == a["sha256"]

    # --- the rest of the transformation set, chained on outputs
    rot = op(client, "rotate", [merged["id"]], pages="1", degrees=90)[0]
    reo = op(client, "reorder", [rot["id"]], order=[3, 1, 2])[0]
    wm = op(client, "watermark", [reo["id"]], text="CONFIDENTIAL", opacity=0.3)[0]
    comp = op(client, "compress", [wm["id"]], level="medium")[0]
    ann = op(client, "annotate", [comp["id"]], annotations=[{"type": "highlight", "page": 1, "x": .1, "y": .1, "w": .3, "h": .05}])[0]
    parts = op(client, "split", [ann["id"]], mode="ranges", ranges="1,2-3")
    assert [p["page_count"] for p in parts] == [1, 2]
    lineage = client.get(f"/api/files/{a['id']}").json()["lineage"]
    assert [f["version"] for f in lineage] == list(range(len(lineage))) and len(lineage) >= 9  # original + every output, in order
    assert lineage[0]["kind"] == "original" and all(f["kind"] == "output" for f in lineage[1:])

    red = op(client, "redact", [merged["id"]], terms=["alpha one"])[0]
    red_bytes = client.get(f"/api/files/{red['id']}/download").content
    assert b"alpha one" not in red_bytes
    assert any(w["code"] == "redact" for w in red["warnings"])

    # --- validation + recoverable errors
    r = client.post("/api/ops/rotate", json={"file_ids": [a["id"]], "params": {"pages": "99", "degrees": 90}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_pages"
    r = client.post("/api/ops/merge", json={"file_ids": [a["id"]], "params": {}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "need_two"
    assert client.post("/api/files", files={"file": ("x.pdf", b"nope", "application/pdf")}).status_code == 422
    assert client.get("/api/files/not-a-uuid").status_code == 400
    assert client.get("/api/files/00000000-0000-0000-0000-000000000000").status_code == 404

    # --- signature mode: place fields, invite, consent, sign, seal
    req = sign_document(client, merged["id"])
    assert req["status"] == "sealed" and req["sealed_sha256"]
    final = client.get(f"/api/files/{req['sealed_file_id']}/download").content
    assert hashlib.sha256(final).hexdigest() == req["sealed_sha256"]
    assert client.get(f"/api/files/{req['sealed_file_id']}/verify").json()["ok"]
    doc = pdfium.PdfDocument(final)
    assert len(doc) == 4  # 3 pages + certificate of completion
    cert = doc[3].get_textpage().get_text_range()
    assert "Certificate of Completion" in cert and merged["sha256"] in cert and "Ada Signer" in cert
    assert dark_pixels(final, 0, (0.1, 0.5, 0.4, 0.12)) > 500   # Ada's drawn signature is on page 1
    assert dark_pixels(merged_bytes := client.get(f"/api/files/{merged['id']}/download").content, 0, (0.1, 0.5, 0.4, 0.12)) == 0
    assert hashlib.sha256(merged_bytes).hexdigest() == merged["sha256"]  # source untouched by signing

    # Signing also lands in the right place on a rotated page.
    rotated_only = op(client, "rotate", [merged["id"]], pages="all", degrees=90)[0]
    req2 = sign_document(client, rotated_only["id"])
    final2 = client.get(f"/api/files/{req2['sealed_file_id']}/download").content
    assert dark_pixels(final2, 0, (0.1, 0.5, 0.4, 0.12)) > 500

    # --- the append-only log tells the whole story, with delivery outcomes and hashes
    evs = client.get("/api/events?limit=1000").json()
    types = {e["type"] for e in evs}
    assert {"file.uploaded", "file.created", "file.downloaded", "sign.invite", "sign.consent", "sign.signed", "sign.sealed"} <= types
    invite = next(e for e in evs if e["type"] == "sign.invite" and e["sha256"] == merged["sha256"])
    assert invite["outcome"] == "not_configured" and invite["sha256"] == merged["sha256"]
    sealed = next(e for e in evs if e["type"] == "sign.sealed" and e["sha256"] == req["sealed_sha256"])
    assert sealed["details"]["source_sha256"] == merged["sha256"]
    ver = client.get("/api/events/verify").json()
    assert ver["ok"] and ver["checked"] == len(evs)

    # --- export + backup
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/export").content))
    names = z.namelist()
    assert "manifest.json" in names and "events.csv" in names and any(n.startswith("originals/") for n in names)
    bk = client.post("/api/backups").json()
    assert client.get("/api/backups").json()[0]["name"] == bk["name"]
    assert client.get(f"/api/backups/{bk['name']}").status_code == 200
    assert client.get("/api/backups/../../etc/passwd").status_code in (404, 400)

    # --- delete + expiry keep the record and the history
    assert client.delete(f"/api/files/{b['id']}").status_code == 200
    assert client.get(f"/api/files/{b['id']}/download").status_code == 410
    assert client.get(f"/api/files/{b['id']}").json()["file"]["deleted_reason"] == "deleted"
    keep = up(client, "keep.pdf", make_pdf(["k"]))["file"]
    assert client.post(f"/api/files/{keep['id']}/expiry", json={"days": 3}).status_code == 200
    from app import expiry
    from app.db import connection

    with connection() as conn:
        conn.execute("UPDATE files SET expires_at = now() - interval '1 day' WHERE id = %s", (keep["id"],))
    assert expiry.sweep() >= 1
    assert client.get(f"/api/files/{keep['id']}/download").status_code == 410
    assert client.get("/api/events/verify").json()["ok"]


def test_remote_hosts_cannot_use_admin_api_but_signer_links_work(client):
    assert client.get("/api/files", headers={"host": "evil.example.com"}).status_code == 403
    r = client.get("/api/public/sign/bogus-token", headers={"host": "evil.example.com"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "bad_link"


def test_missing_libreoffice_is_a_recoverable_error(client, monkeypatch):
    from app import tools

    monkeypatch.setattr(tools, "soffice_binary", lambda: None)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<x/>")
    r = client.post("/api/files", files={"file": ("memo.docx", buf.getvalue(), "application/octet-stream")})
    assert r.status_code == 201
    body = r.json()
    assert body["file"]["kind"] == "original" and body["outputs"] == []
    assert body["convert_error"]["code"] == "tool_unavailable" and "Install LibreOffice" in body["convert_error"]["hint"]


def test_smtp_failure_is_recorded_not_fatal(client, monkeypatch):
    from app import mailer
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "smtp_host", "127.0.0.1")
    monkeypatch.setattr(get_settings(), "smtp_port", 1)  # nothing listens here
    monkeypatch.setattr(get_settings(), "smtp_from", "me@example.com")
    f = up(client, "mail.pdf", make_pdf(["x"]))["file"]
    rid = client.post("/api/sign/requests", json={"file_id": f["id"], "title": "T"}).json()["id"]
    client.put(f"/api/sign/requests/{rid}", json={"signers": [{"name": "A", "email": "a@example.com"}],
                                                  "fields": [{"signer": 0, "page": 1, "x": .1, "y": .1, "w": .2, "h": .1}]})
    inv = client.post(f"/api/sign/requests/{rid}/send").json()["invitations"][0]
    assert inv["delivery"] == "failed" and inv["link"].startswith("http")  # link still usable manually
    ev = next(e for e in client.get("/api/events").json() if e["type"] == "sign.invite")
    assert ev["outcome"] == "failed"


@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice not installed")
def test_office_to_pdf(client):
    out = up(client, "notes.txt", b"Hello from LibreOffice\n", "text/plain")
    assert out["file"]["mime"] != "application/pdf" and out["convert_error"] is None
    pdf = out["outputs"][0]
    assert pdf["operation"] == "convert" and pdf["page_count"] >= 1
    body = client.get(f"/api/files/{pdf['id']}/download").content
    assert "Hello from LibreOffice" in pdfium.PdfDocument(body)[0].get_textpage().get_text_range()


@pytest.mark.skipif(not shutil.which("tesseract"), reason="Tesseract not installed")
def test_ocr_makes_scanned_page_searchable(client):
    img = Image.new("RGB", (1700, 600), "white")
    ImageDraw.Draw(img).text((100, 200), "Invoice 4711", fill="black", font=ImageFont.load_default(size=160))
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=150)
    scanned = up(client, "scan.pdf", buf.getvalue())["file"]
    out = op(client, "ocr", [scanned["id"]], languages="eng")[0]
    text = pdfium.PdfDocument(client.get(f"/api/files/{out['id']}/download").content)[0].get_textpage().get_text_range()
    assert "4711" in text
