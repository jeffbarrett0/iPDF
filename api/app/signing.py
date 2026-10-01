"""Simple, local electronic-signature workflow.

NOT a qualified/advanced/regulated e-signature and performs NO identity verification: it records who
clicked a private link, their stated consent, and seals the resulting PDF with a SHA-256 hash that is
written to the tamper-evident event log.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import re
import secrets
import tempfile
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, Pdf, Stream
from PIL import Image
from psycopg.types.json import Jsonb

from . import analysis, events, geometry as geo, mailer, pdfops, store
from .config import get_settings
from .db import connection
from .errors import AppError

CONSENT_VERSION = "2025-01"
CONSENT_TEXT = (
    "I agree to sign this document electronically. I understand that my typed or drawn mark, the time of "
    "signing and my network address will be recorded, that this is a simple electronic signature without "
    "identity verification, and that I can stop now by closing this page or declining."
)
CONSENT_SHA256 = hashlib.sha256(CONSENT_TEXT.encode()).hexdigest()
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
MAX_SIGNERS = 20
MAX_FIELDS = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(token: str) -> str:
    return hmac.new(get_settings().secret_key.encode(), token.encode(), hashlib.sha256).hexdigest()


def _ser(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if k in ("token_hash", "consent_user_agent"):
            continue
        out[k] = str(v) if isinstance(v, uuid.UUID) else v
    return out


# ------------------------------------------------------------------ draft management
def create_request(file_id: str, title: str, message: str) -> dict:
    title = (title or "").strip()[:200]
    if not title:
        raise AppError("bad_title", "Give the signature request a title.", 422)
    with connection() as conn:
        f = store.get_file(conn, file_id)
        if f["mime"] != "application/pdf":
            raise AppError("not_pdf", "Only PDFs can be sent for signature.", 422)
        row = conn.execute(
            "INSERT INTO sign_requests (id, file_id, title, message, source_sha256) VALUES (%s,%s,%s,%s,%s) RETURNING *",
            (uuid.uuid4(), f["id"], title, (message or "")[:2000], f["sha256"])).fetchone()
        events.append(conn, "sign.request_created", file_id=f["id"], sha256=f["sha256"],
                      details={"request_id": str(row["id"]), "title": title})
    return get_request(row["id"])


def _request_row(conn, request_id: Any) -> dict:
    try:
        rid = uuid.UUID(str(request_id))
    except ValueError:
        raise AppError("bad_id", "Invalid request id.", 400) from None
    row = conn.execute("SELECT * FROM sign_requests WHERE id=%s", (rid,)).fetchone()
    if not row:
        raise AppError("not_found", "Signature request not found.", 404)
    return row


def get_request(request_id: Any) -> dict:
    with connection() as conn:
        r = _request_row(conn, request_id)
        signers = conn.execute("SELECT * FROM signers WHERE request_id=%s ORDER BY name, id", (r["id"],)).fetchall()
        fields = conn.execute("SELECT * FROM sign_fields WHERE request_id=%s ORDER BY page, y, x", (r["id"],)).fetchall()
        f = conn.execute("SELECT display_name, page_count FROM files WHERE id=%s", (r["file_id"],)).fetchone()
    out = _ser(r)
    out["file_name"], out["page_count"] = f["display_name"], f["page_count"]
    out["signers"] = [_ser(s) for s in signers]
    out["fields"] = [{**_ser(x), "value": None if x["value"] is None else {"mode": x["value"].get("mode")}} for x in fields]
    return out


def list_requests() -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT r.*, f.display_name AS file_name,
                      (SELECT count(*) FROM signers s WHERE s.request_id=r.id) AS signer_count,
                      (SELECT count(*) FROM signers s WHERE s.request_id=r.id AND s.status='signed') AS signed_count
               FROM sign_requests r JOIN files f ON f.id=r.file_id ORDER BY r.created_at DESC LIMIT 200""").fetchall()
    return [_ser(r) for r in rows]


def set_layout(request_id: str, signers: list[dict], fields: list[dict]) -> dict:
    if not 1 <= len(signers) <= MAX_SIGNERS:
        raise AppError("bad_signers", f"Add between 1 and {MAX_SIGNERS} signers.", 422)
    if len(fields) > MAX_FIELDS:
        raise AppError("too_many_fields", f"At most {MAX_FIELDS} fields.", 422)
    clean_signers = []
    for s in signers:
        name, email = str(s.get("name", "")).strip()[:120], str(s.get("email", "")).strip()[:320]
        if not name:
            raise AppError("bad_signer", "Every signer needs a name.", 422)
        if not EMAIL_RE.match(email):
            raise AppError("bad_signer", f"'{email}' is not a valid email address.", 422)
        clean_signers.append((name, email))
    with connection() as conn:
        r = _request_row(conn, request_id)
        if r["status"] != "draft":
            raise AppError("locked", "Fields and signers are locked once invitations are sent.", 409,
                           "Void this request and start a new one to change them.")
        page_count = conn.execute("SELECT page_count FROM files WHERE id=%s", (r["file_id"],)).fetchone()["page_count"]
        conn.execute("DELETE FROM sign_fields WHERE request_id=%s", (r["id"],))
        conn.execute("DELETE FROM signers WHERE request_id=%s", (r["id"],))
        ids = []
        for name, email in clean_signers:
            sid = uuid.uuid4()
            ids.append(sid)
            conn.execute("INSERT INTO signers (id, request_id, name, email) VALUES (%s,%s,%s,%s)", (sid, r["id"], name, email))
        signer_with_field = set()
        for fl in fields:
            idx = int(fl.get("signer", -1))
            if not 0 <= idx < len(ids):
                raise AppError("bad_field", "Each field must be assigned to one of the signers.", 422)
            page = int(fl.get("page", 0))
            if not 1 <= page <= page_count:
                raise AppError("bad_field", f"Field page {page} is outside the document.", 422)
            x, y, w, h = geo.validate_frac_rect(fl)
            kind = fl.get("kind", "signature")
            if kind not in ("signature", "date", "text"):
                raise AppError("bad_field", "Field type must be signature, date or text.", 422)
            signer_with_field.add(idx)
            conn.execute(
                """INSERT INTO sign_fields (id, request_id, signer_id, page, x, y, w, h, kind, label, required)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (uuid.uuid4(), r["id"], ids[idx], page, x, y, w, h, kind, str(fl.get("label", ""))[:80],
                 bool(fl.get("required", True))))
        events.append(conn, "sign.layout_saved", file_id=r["file_id"], sha256=r["source_sha256"],
                      details={"request_id": str(r["id"]), "signers": len(ids), "fields": len(fields)})
    return get_request(request_id)


# ------------------------------------------------------------------ invitations
def _link(token: str) -> str:
    return f"{get_settings().public_base_url}/s/{token}"


def _invite_one(conn, r: dict, signer: dict) -> dict:
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=get_settings().sign_link_ttl_days)
    conn.execute("UPDATE signers SET token_hash=%s, token_expires_at=%s, invited_at=%s, "
                 "status=CASE WHEN status IN ('pending','invited','viewed') THEN 'invited' ELSE status END WHERE id=%s",
                 (hash_token(token), expires, _now(), signer["id"]))
    link = _link(token)
    outcome, detail = mailer.send_invitation(signer["name"], signer["email"], r["title"], link, r["message"])
    events.append(conn, "sign.invite", file_id=r["file_id"], sha256=r["source_sha256"], outcome=outcome,
                  details={"request_id": str(r["id"]), "signer_id": str(signer["id"]), "email": signer["email"],
                           "delivery": detail, "link_expires": expires.isoformat()})
    return {"signer_id": str(signer["id"]), "name": signer["name"], "email": signer["email"],
            "delivery": outcome, "detail": detail, "link": link, "expires_at": expires}


def send_request(request_id: str) -> list[dict]:
    with connection() as conn:
        r = _request_row(conn, request_id)
        if r["status"] != "draft":
            raise AppError("already_sent", "Invitations were already sent for this request.", 409,
                           "Use 'new link' on an individual signer to resend.")
        signers = conn.execute("SELECT * FROM signers WHERE request_id=%s", (r["id"],)).fetchall()
        if not signers:
            raise AppError("no_signers", "Add at least one signer first.", 422)
        counts = {row["signer_id"]: row["n"] for row in conn.execute(
            "SELECT signer_id, count(*) n FROM sign_fields WHERE request_id=%s GROUP BY signer_id", (r["id"],))}
        for s in signers:
            if not counts.get(s["id"]):
                raise AppError("no_fields", f"{s['name']} has no fields to sign.", 422, "Place at least one field for each signer.")
        src = store.get_file(conn, r["file_id"])
        if src["sha256"] != r["source_sha256"] or not store.verify_file(src)["ok"]:
            raise AppError("integrity", "The document changed on disk since the request was created.", 409)
        conn.execute("UPDATE sign_requests SET status='sent', sent_at=now() WHERE id=%s", (r["id"],))
        return [_invite_one(conn, r, s) for s in signers]


def reissue_link(request_id: str, signer_id: str) -> dict:
    with connection() as conn:
        r = _request_row(conn, request_id)
        if r["status"] != "sent":
            raise AppError("not_open", "This request is not awaiting signatures.", 409)
        s = conn.execute("SELECT * FROM signers WHERE id=%s AND request_id=%s", (signer_id, r["id"])).fetchone()
        if not s:
            raise AppError("not_found", "Signer not found.", 404)
        if s["status"] in ("signed", "declined"):
            raise AppError("done", f"{s['name']} already {s['status']}.", 409)
        return _invite_one(conn, r, s)


def void_request(request_id: str) -> dict:
    with connection() as conn:
        r = _request_row(conn, request_id)
        if r["status"] in ("sealed", "voided"):
            raise AppError("final", f"A {r['status']} request cannot be voided.", 409)
        conn.execute("UPDATE sign_requests SET status='voided', voided_at=now() WHERE id=%s", (r["id"],))
        conn.execute("UPDATE signers SET token_hash=NULL WHERE request_id=%s", (r["id"],))
        events.append(conn, "sign.voided", file_id=r["file_id"], sha256=r["source_sha256"],
                      details={"request_id": str(r["id"])})
    return get_request(request_id)


# ------------------------------------------------------------------ signer side
def _signer_by_token(conn, token: str) -> tuple[dict, dict]:
    if not token or len(token) > 100:
        raise AppError("bad_link", "This signing link is not valid.", 404)
    s = conn.execute("SELECT * FROM signers WHERE token_hash=%s", (hash_token(token),)).fetchone()
    if not s:
        raise AppError("bad_link", "This signing link is not valid or was replaced.", 404,
                       "Ask the sender for a new link.")
    r = _request_row(conn, s["request_id"])
    if r["status"] == "voided":
        raise AppError("voided", "The sender cancelled this request.", 410)
    if s["token_expires_at"] and s["token_expires_at"] < _now() and s["status"] != "signed":
        raise AppError("expired", "This signing link has expired.", 410, "Ask the sender for a new link.")
    return s, r


def public_view(token: str) -> dict:
    with connection() as conn:
        s, r = _signer_by_token(conn, token)
        if s["status"] == "invited":
            conn.execute("UPDATE signers SET status='viewed', viewed_at=now() WHERE id=%s", (s["id"],))
            events.append(conn, "sign.viewed", file_id=r["file_id"], sha256=r["source_sha256"],
                          details={"request_id": str(r["id"]), "signer_id": str(s["id"])})
            s["status"] = "viewed"
        fields = conn.execute("SELECT * FROM sign_fields WHERE request_id=%s ORDER BY page, y, x", (r["id"],)).fetchall()
        f = conn.execute("SELECT display_name, page_count FROM files WHERE id=%s", (r["file_id"],)).fetchone()
    return {
        "title": r["title"], "message": r["message"], "request_status": r["status"],
        "document": f["display_name"], "page_count": f["page_count"],
        "signer": {"name": s["name"], "status": s["status"]},
        "consent_text": CONSENT_TEXT, "consent_version": CONSENT_VERSION,
        "fields": [{"id": str(x["id"]), "page": x["page"], "x": x["x"], "y": x["y"], "w": x["w"], "h": x["h"],
                    "kind": x["kind"], "label": x["label"], "required": x["required"],
                    "mine": x["signer_id"] == s["id"], "filled": x["value"] is not None} for x in fields],
        "sealed": r["status"] == "sealed",
    }


def record_consent(token: str, agree: bool, ip: str | None, user_agent: str | None) -> dict:
    if not agree:
        raise AppError("no_consent", "You must agree to sign electronically to continue.", 422)
    with connection() as conn:
        s, r = _signer_by_token(conn, token)
        if r["status"] != "sent":
            raise AppError("not_open", "This request is no longer accepting signatures.", 409)
        if s["status"] in ("signed", "declined"):
            raise AppError("done", f"You already {s['status']} this document.", 409)
        conn.execute("UPDATE signers SET status='consented', consent_at=now(), consent_text_sha256=%s, consent_ip=%s, "
                     "consent_user_agent=%s WHERE id=%s", (CONSENT_SHA256, ip, (user_agent or "")[:300], s["id"]))
        events.append(conn, "sign.consent", file_id=r["file_id"], sha256=r["source_sha256"],
                      details={"request_id": str(r["id"]), "signer_id": str(s["id"]), "name": s["name"],
                               "consent_version": CONSENT_VERSION, "consent_text_sha256": CONSENT_SHA256, "ip": ip})
    return {"ok": True}


def _clean_png(data_url: str) -> str:
    m = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=]+)", data_url or "")
    if not m or len(m.group(1)) > 400_000:
        raise AppError("bad_signature", "The drawn signature is invalid or too large.", 422)
    try:
        img = Image.open(io.BytesIO(base64.b64decode(m.group(1))))
        img.load()
    except Exception:  # noqa: BLE001
        raise AppError("bad_signature", "The drawn signature could not be read.", 422) from None
    if img.width > 2000 or img.height > 1000:
        raise AppError("bad_signature", "The drawn signature image is too large.", 422)
    img = img.convert("RGBA")
    if img.getchannel("A").getextrema()[1] == 0:
        raise AppError("blank_signature", "Please draw or type your signature.", 422)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def submit_signature(token: str, values: dict[str, dict]) -> dict:
    seal_needed = None
    with connection() as conn:
        s, r = _signer_by_token(conn, token)
        if r["status"] != "sent":
            raise AppError("not_open", "This request is no longer accepting signatures.", 409)
        if s["status"] == "signed":
            raise AppError("done", "You already signed this document.", 409)
        if s["status"] != "consented":
            raise AppError("consent_required", "Please agree to sign electronically first.", 409)
        fields = conn.execute("SELECT * FROM sign_fields WHERE signer_id=%s", (s["id"],)).fetchall()
        now = _now()
        for f in fields:
            fid = str(f["id"])
            v = values.get(fid) or {}
            if f["kind"] == "date":
                val = {"mode": "date", "text": now.date().isoformat()}
            elif f["kind"] == "text":
                text = str(v.get("text", "")).strip()[:200]
                if not text and f["required"]:
                    raise AppError("missing_value", f"Please fill in '{f['label'] or 'text field'}'.", 422)
                val = {"mode": "text", "text": text} if text else None
            else:
                mode = v.get("mode")
                if mode == "typed":
                    text = str(v.get("text", "")).strip()[:80]
                    try:
                        text.encode("cp1252")
                    except UnicodeEncodeError:
                        raise AppError("bad_signature", "Typed signatures support Latin letters only; draw it instead.", 422) from None
                    if not text:
                        raise AppError("missing_value", "Please type or draw your signature.", 422)
                    val = {"mode": "typed", "text": text}
                elif mode == "drawn":
                    val = {"mode": "drawn", "png_b64": _clean_png(v.get("data_url", ""))}
                elif f["required"]:
                    raise AppError("missing_value", "Please type or draw your signature.", 422)
                else:
                    val = None
            if val is not None:
                conn.execute("UPDATE sign_fields SET value=%s, filled_at=%s WHERE id=%s", (Jsonb(val), now, f["id"]))
        conn.execute("UPDATE signers SET status='signed', signed_at=%s WHERE id=%s", (now, s["id"]))
        events.append(conn, "sign.signed", file_id=r["file_id"], sha256=r["source_sha256"],
                      details={"request_id": str(r["id"]), "signer_id": str(s["id"]), "name": s["name"]})
        remaining = conn.execute("SELECT count(*) n FROM signers WHERE request_id=%s AND status<>'signed'", (r["id"],)).fetchone()["n"]
        if remaining == 0:
            conn.execute("UPDATE sign_requests SET status='completed', completed_at=now() WHERE id=%s", (r["id"],))
            events.append(conn, "sign.completed", file_id=r["file_id"], sha256=r["source_sha256"],
                          details={"request_id": str(r["id"])})
            seal_needed = r["id"]
    sealed = None
    if seal_needed:
        try:
            sealed = seal(seal_needed)
        except AppError:
            sealed = None  # sender can retry from the UI; the signer's signature is already recorded
    return {"ok": True, "sealed": sealed is not None}


def decline(token: str, reason: str) -> dict:
    with connection() as conn:
        s, r = _signer_by_token(conn, token)
        if r["status"] != "sent" or s["status"] in ("signed", "declined"):
            raise AppError("not_open", "This request is no longer open.", 409)
        conn.execute("UPDATE signers SET status='declined', decline_reason=%s WHERE id=%s", ((reason or "")[:300], s["id"]))
        events.append(conn, "sign.declined", file_id=r["file_id"], sha256=r["source_sha256"],
                      details={"request_id": str(r["id"]), "signer_id": str(s["id"]), "name": s["name"], "reason": (reason or "")[:300]})
    return {"ok": True}


def public_file(token: str) -> tuple[dict, dict]:
    """The source (or, once sealed, the sealed) PDF row for a valid signer token."""
    with connection() as conn:
        s, r = _signer_by_token(conn, token)
        fid = r["sealed_file_id"] or r["file_id"]
        return store.get_file(conn, fid), r


# ------------------------------------------------------------------ sealing
def _image_xobject(pdf: Pdf, img: Image.Image):
    rgba = img.convert("RGBA")
    xo = Stream(pdf, b"")
    xo.write(zlib.compress(rgba.convert("RGB").tobytes()), filter=Name.FlateDecode)
    xo.Type, xo.Subtype = Name.XObject, Name.Image
    xo.Width, xo.Height, xo.ColorSpace, xo.BitsPerComponent = rgba.width, rgba.height, Name.DeviceRGB, 8
    sm = Stream(pdf, b"")
    sm.write(zlib.compress(rgba.getchannel("A").tobytes()), filter=Name.FlateDecode)
    sm.Type, sm.Subtype = Name.XObject, Name.Image
    sm.Width, sm.Height, sm.ColorSpace, sm.BitsPerComponent = rgba.width, rgba.height, Name.DeviceGray, 8
    xo.SMask = pdf.make_indirect(sm)
    return pdf.make_indirect(xo)


def _certificate_pages(pdf: Pdf, lines: list[tuple[str, bool]]) -> None:
    per_page = 52
    chunks = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    reg = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica, Encoding=Name.WinAnsiEncoding))
    bold = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name("/Helvetica-Bold"), Encoding=Name.WinAnsiEncoding))
    for chunk in chunks:
        pdf.add_blank_page(page_size=(612, 792))
        page = pdf.pages[-1]
        page.Resources = Dictionary(Font=Dictionary(F1=reg, F2=bold))
        ops = [b"BT"]
        y = 750
        for text, is_bold in chunk:
            ops.append(f"/{'F2' if is_bold else 'F1'} {12 if is_bold else 9} Tf 1 0 0 1 48 {y} Tm".encode() + pdfops._pdf_str(text) + b" Tj")
            y -= 14
        ops.append(b"ET")
        page.Contents = Stream(pdf, b"\n".join(ops))


def _wrap_line(text: str, width: int = 100) -> list[str]:
    out, cur = [], ""
    for word in text.split(" "):
        if len(cur) + len(word) + 1 > width and cur:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return out + [cur]


def seal(request_id: Any) -> dict:
    with connection() as conn:
        r = _request_row(conn, request_id)
        if r["status"] == "sealed":
            return {"file_id": str(r["sealed_file_id"]), "sha256": r["sealed_sha256"]}
        if r["status"] != "completed":
            raise AppError("not_complete", "Every signer must sign before the document can be sealed.", 409)
        src = store.get_file(conn, r["file_id"])
        signers = conn.execute("SELECT * FROM signers WHERE request_id=%s ORDER BY name", (r["id"],)).fetchall()
        fields = conn.execute("SELECT * FROM sign_fields WHERE request_id=%s", (r["id"],)).fetchall()
    if not store.verify_file(src)["ok"] or src["sha256"] != r["source_sha256"]:
        raise AppError("integrity", "The source document no longer matches the hash recorded when signing began.", 409)
    src_path = store.path_of(src)
    by_signer = {s["id"]: s for s in signers}
    out = Path(tempfile.NamedTemporaryFile(dir=store.tmp_dir(), suffix=".pdf", delete=False).name)
    try:
        with pikepdf.open(src_path) as pdf:
            ital = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name("/Times-Italic"), Encoding=Name.WinAnsiEncoding))
            reg = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica, Encoding=Name.WinAnsiEncoding))
            for i, f in enumerate(fields):
                v = f["value"]
                if not v:
                    continue
                page = pdf.pages[f["page"] - 1]
                l, b, w, h = geo.frac_rect_to_visible(page, f["x"], f["y"], f["w"], f["h"])
                if v["mode"] == "drawn":
                    img = Image.open(io.BytesIO(base64.b64decode(v["png_b64"])))
                    scale = min(w / img.width, h / img.height)
                    dw, dh = img.width * scale, img.height * scale
                    xo = _image_xobject(pdf, img)
                    name = f"/IPDFSig{i}"
                    body = f"q {dw:.2f} 0 0 {dh:.2f} {l + (w - dw) / 2:.2f} {b + (h - dh) / 2:.2f} cm {name} Do Q".encode()
                    pdfops.stamp_page(pdf, page, body, xobjects={name: xo})
                else:
                    text = v["text"]
                    typed = v["mode"] == "typed"
                    size = max(6.0, min(h * 0.6, w / max(1, len(text)) / (0.42 if typed else 0.5)))
                    fname = f"/IPDFTx{i}"
                    body = (b"BT 0.05 0.1 0.35 rg " if typed else b"BT 0 0 0 rg ") + \
                        f"{fname} {size:.2f} Tf {l + 2:.2f} {b + (h - size) / 2 + size * 0.2:.2f} Td ".encode() + pdfops._pdf_str(text) + b" Tj ET"
                    pdfops.stamp_page(pdf, page, body, fonts={fname: ital if typed else reg})
            lines: list[tuple[str, bool]] = [("Certificate of Completion", True), ("", False)]
            for text in (f"Request: {r['title']}", f"Request ID: {r['id']}", f"Document: {src['display_name']}",
                         f"SHA-256 of document before signing: {src['sha256']}", f"Completed (UTC): {r['completed_at']}"):
                lines += [(ln, False) for ln in _wrap_line(text)]
            lines += [("", False), ("Signers", True)]
            for s in signers:
                lines += [(f"{s['name']} <{s['email']}>", True),
                          (f"  Consented (UTC): {s['consent_at']}   from: {s['consent_ip'] or 'unknown'}", False),
                          (f"  Signed (UTC): {s['signed_at']}   consent text sha256: {(s['consent_text_sha256'] or '')[:16]}...", False)]
            lines += [("", False)]
            for ln in _wrap_line("This is a simple electronic signature record produced locally by iPDF. It is not a qualified, "
                                 "advanced or otherwise regulated signature and signers' identities were not verified. The final "
                                 "file's SHA-256 is recorded in the append-only event log (event 'sign.sealed'); it cannot be "
                                 "printed on this page because the page is part of the hashed file."):
                lines.append((ln, False))
            _certificate_pages(pdf, lines)
            pdf.docinfo["/IPDFSourceSHA256"] = src["sha256"]
            pdf.docinfo["/IPDFRequestId"] = str(r["id"])
            pdfops._save(pdf, out)
        an = analysis.analyze_path(out)
        sources = [src]
        final = store.add_output(out, operation="sign.seal", params={"request_id": str(r["id"])}, sources=sources,
                                 page_count=an["page_count"], analysis=an,
                                 warnings=[{"code": "sealed", "severity": "info",
                                            "message": "Sealed signature copy. Editing it creates a new file with a different hash."}],
                                 name_hint=f"signed-{src['display_name']}")
    finally:
        out.unlink(missing_ok=True)
    with connection() as conn:
        conn.execute("UPDATE sign_requests SET status='sealed', sealed_file_id=%s, sealed_sha256=%s WHERE id=%s",
                     (final["id"], final["sha256"], r["id"]))
        events.append(conn, "sign.sealed", file_id=final["id"], sha256=final["sha256"], details={
            "request_id": str(r["id"]), "source_file_id": str(src["id"]), "source_sha256": src["sha256"],
            "signers": [{"name": s["name"], "email": s["email"], "consent_at": s["consent_at"], "signed_at": s["signed_at"]}
                        for s in signers]})
    return {"file_id": str(final["id"]), "sha256": final["sha256"]}
