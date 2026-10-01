from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import backup as backup_mod
from . import events, exporting, expiry, ingest, preview, service, signing, store, tools
from .config import get_settings
from .db import close_pool, connection, database_available, init_schema
from .errors import AppError
from .safenames import content_disposition

log = logging.getLogger("ipdf")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "testserver"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    for sub in ("originals", "outputs", "tmp", "cache"):
        (s.data_dir / sub).mkdir(parents=True, exist_ok=True)
    try:
        init_schema()
    except Exception as exc:  # noqa: BLE001
        log.error("Database not ready: %s", exc)  # the API still starts; /api/health reports it
    stop = expiry.start_background()
    yield
    stop.set()
    close_pool()


app = FastAPI(title="iPDF", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)


@app.exception_handler(AppError)
async def _app_error(_: Request, exc: AppError):
    return JSONResponse(exc.payload(), status_code=exc.status)


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception):
    from psycopg import OperationalError

    if isinstance(exc, OperationalError) or exc.__class__.__name__ == "PoolTimeout":
        err = AppError("database_unavailable", "The local database is not reachable.", 503,
                       "Is PostgreSQL running? Start it with ./run.sh (or check DATABASE_URL), then retry.")
    else:
        log.exception("unhandled error")
        err = AppError("internal", "Something went wrong on our side. Nothing was changed.", 500)
    return JSONResponse(err.payload(), status_code=err.status)


@app.middleware("http")
async def admin_guard(request: Request, call_next):
    """The management API is for this machine only; just the signer links may be reached remotely."""
    if not get_settings().allow_remote_admin and not request.url.path.startswith("/api/public/"):
        raw = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
        host = raw[: raw.index("]") + 1] if raw.startswith("[") else raw.split(":")[0]
        if host not in LOCAL_HOSTS:
            return JSONResponse(AppError("local_only", "The management interface is only available on this computer.", 403,
                                         "Open it via http://localhost, or set ALLOW_REMOTE_ADMIN=true if you accept the risk.").payload(), status_code=403)
    return await call_next(request)


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)


# ------------------------------------------------------------------ system
@app.get("/api/health")
def health():
    s = get_settings()
    from .mailer import smtp_configured

    return {"ok": True, "database": database_available(), "tools": tools.tool_status(), "smtp": smtp_configured(),
            "data_dir": str(s.data_dir), "backup_dir": str(s.backup_dir), "file_ttl_days": s.file_ttl_days,
            "max_upload_mb": s.max_upload_mb}


# ------------------------------------------------------------------ files
@app.get("/api/files")
def list_files(include_deleted: bool = False):
    with connection() as conn:
        q = "SELECT * FROM files" + ("" if include_deleted else " WHERE deleted_at IS NULL") + " ORDER BY created_at DESC LIMIT 1000"
        return [store.public(r) for r in conn.execute(q).fetchall()]


@app.post("/api/files", status_code=201)
def upload(file: UploadFile = File(...)):
    row, is_office = ingest.validate_and_store(file.file, file.filename or "upload")
    out: dict[str, Any] = {"file": store.public(row), "outputs": [], "convert_error": None}
    if is_office:
        try:
            out["outputs"] = [store.public(r) for r in service.run("convert", [str(row["id"])], {})]
        except AppError as exc:
            out["convert_error"] = exc.payload()["error"]
    return out


@app.get("/api/files/{file_id}")
def file_detail(file_id: str):
    with connection() as conn:
        row = store.get_file(conn, file_id, require_content=False)
        lineage = conn.execute("SELECT * FROM files WHERE root_id=%s ORDER BY version", (row["root_id"],)).fetchall()
    return {"file": store.public(row), "lineage": [store.public(r) for r in lineage]}


@app.get("/api/files/{file_id}/download")
def download(file_id: str):
    with connection() as conn:
        row = store.get_file(conn, file_id)
        path = store.path_of(row)
        events.append(conn, "file.downloaded", file_id=row["id"], sha256=row["sha256"], details={"name": row["display_name"]})
    return FileResponse(path, media_type=row["mime"], headers={"Content-Disposition": content_disposition(row["display_name"])})


@app.get("/api/files/{file_id}/view")
def view(file_id: str):
    with connection() as conn:
        row = store.get_file(conn, file_id)
    if row["mime"] != "application/pdf":
        raise AppError("not_pdf", "Only PDFs can be viewed.", 422)
    return FileResponse(store.path_of(row), media_type="application/pdf", headers={"Content-Disposition": "inline"})


@app.get("/api/files/{file_id}/verify")
def verify(file_id: str):
    with connection() as conn:
        row = store.get_file(conn, file_id)
    return store.verify_file(row)


@app.get("/api/files/{file_id}/pages/{page}/preview")
def page_preview(file_id: str, page: int, width: int = Query(320, ge=64, le=2000)):
    with connection() as conn:
        row = store.get_file(conn, file_id)
    if row["mime"] != "application/pdf":
        raise AppError("not_pdf", "Previews are only available for PDFs.", 422)
    png = preview.render_page(store.path_of(row), row["sha256"], page, width)
    return FileResponse(png, media_type="image/png", headers={"Cache-Control": "private, max-age=31536000, immutable"})


class ExpiryBody(BaseModel):
    days: int | None = Field(None, ge=1, le=3650, description="null = never expire")


@app.post("/api/files/{file_id}/expiry")
def set_expiry(file_id: str, body: ExpiryBody):
    with connection() as conn:
        row = store.get_file(conn, file_id)
        conn.execute("UPDATE files SET expires_at = CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(days => %s::int) END WHERE id=%s",
                     (body.days, body.days, row["id"]))
        events.append(conn, "file.expiry_changed", file_id=row["id"], sha256=row["sha256"], details={"days": body.days})
        return store.public(store.get_file(conn, file_id))


@app.delete("/api/files/{file_id}")
def delete_file(file_id: str):
    with connection() as conn:
        row = store.get_file(conn, file_id)
        busy = conn.execute("SELECT 1 FROM sign_requests WHERE status IN ('sent','completed') AND (file_id=%s OR sealed_file_id=%s) LIMIT 1",
                            (row["id"], row["id"])).fetchone()
    if busy:
        raise AppError("in_use", "This file is part of an open signature request.", 409, "Void the request first.")
    store.delete_file(file_id)
    return {"ok": True}


# ------------------------------------------------------------------ operations
class OpBody(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=50)
    params: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/ops/{op}")
def run_op(op: str, body: OpBody):
    outputs = service.run(op, body.file_ids, body.params)
    return {"outputs": [store.public(r) for r in outputs]}


@app.post("/api/ops/{op}/preflight")
def preflight(op: str, body: OpBody):
    return {"warnings": service.preflight(op, body.file_ids)}


# ------------------------------------------------------------------ events
@app.get("/api/events")
def list_events(file_id: str | None = None, limit: int = Query(100, ge=1, le=1000), before: int | None = None):
    with connection() as conn:
        clauses, args = [], []
        if file_id:
            clauses.append("file_id = %s")
            args.append(file_id)
        if before:
            clauses.append("id < %s")
            args.append(before)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(f"SELECT * FROM events {where} ORDER BY id DESC LIMIT %s", (*args, limit)).fetchall()  # noqa: S608
    return [{**r, "file_id": str(r["file_id"]) if r["file_id"] else None} for r in rows]


@app.get("/api/events/verify")
def verify_events():
    with connection() as conn:
        return events.verify_chain(conn)


# ------------------------------------------------------------------ export / backup
def _cleanup(path: Path):
    path.unlink(missing_ok=True)


@app.get("/api/export")
def export_all(bg: BackgroundTasks, root_id: str | None = None):
    if root_id:
        uuid.UUID(root_id)
    path = exporting.build_export(root_id)
    with connection() as conn:
        events.append(conn, "export.created", details={"scope": root_id or "all", "bytes": path.stat().st_size})
    bg.add_task(_cleanup, path)
    return FileResponse(path, media_type="application/zip", filename=path.name.split("-", 2)[0] + "-export.zip")


@app.get("/api/backups")
def backups():
    return backup_mod.list_backups()


@app.post("/api/backups", status_code=201)
def make_backup():
    path = backup_mod.create_backup()
    return {"name": path.name, "size": path.stat().st_size, "path": str(path)}


@app.get("/api/backups/{name}")
def download_backup(name: str):
    if not name.startswith("ipdf-backup-") or not name.endswith(".tar.gz") or "/" in name or ".." in name:
        raise AppError("bad_name", "Unknown backup.", 404)
    p = get_settings().backup_dir / name
    if not p.exists():
        raise AppError("not_found", "Backup not found.", 404)
    return FileResponse(p, media_type="application/gzip", filename=name)


# ------------------------------------------------------------------ signing (sender side)
class RequestBody(BaseModel):
    file_id: str
    title: str
    message: str = ""


class LayoutBody(BaseModel):
    signers: list[dict[str, Any]]
    fields: list[dict[str, Any]] = []


@app.get("/api/sign/requests")
def sign_list():
    return signing.list_requests()


@app.post("/api/sign/requests", status_code=201)
def sign_create(body: RequestBody):
    return signing.create_request(body.file_id, body.title, body.message)


@app.get("/api/sign/requests/{rid}")
def sign_get(rid: str):
    return signing.get_request(rid)


@app.put("/api/sign/requests/{rid}")
def sign_layout(rid: str, body: LayoutBody):
    return signing.set_layout(rid, body.signers, body.fields)


@app.post("/api/sign/requests/{rid}/send")
def sign_send(rid: str):
    return {"invitations": signing.send_request(rid), "request": signing.get_request(rid)}


@app.post("/api/sign/requests/{rid}/signers/{sid}/link")
def sign_reissue(rid: str, sid: str):
    return signing.reissue_link(rid, sid)


@app.post("/api/sign/requests/{rid}/void")
def sign_void(rid: str):
    return signing.void_request(rid)


@app.post("/api/sign/requests/{rid}/seal")
def sign_seal(rid: str):
    return signing.seal(rid)


# ------------------------------------------------------------------ signing (signer side, token-gated)
class ConsentBody(BaseModel):
    agree: bool


class SignBody(BaseModel):
    values: dict[str, dict[str, Any]] = {}


class DeclineBody(BaseModel):
    reason: str = ""


@app.get("/api/public/sign/{token}")
def pub_view(token: str):
    return signing.public_view(token)


@app.post("/api/public/sign/{token}/consent")
def pub_consent(token: str, body: ConsentBody, request: Request):
    return signing.record_consent(token, body.agree, _client_ip(request), request.headers.get("user-agent"))


@app.post("/api/public/sign/{token}/sign")
def pub_sign(token: str, body: SignBody):
    return signing.submit_signature(token, body.values)


@app.post("/api/public/sign/{token}/decline")
def pub_decline(token: str, body: DeclineBody):
    return signing.decline(token, body.reason)


@app.get("/api/public/sign/{token}/pages/{page}/preview")
def pub_preview(token: str, page: int, width: int = Query(900, ge=64, le=2000)):
    row, _ = signing.public_file(token)
    return FileResponse(preview.render_page(store.path_of(row), row["sha256"], page, width), media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/public/sign/{token}/document")
def pub_document(token: str):
    row, r = signing.public_file(token)
    if r["status"] != "sealed":
        raise AppError("not_sealed", "The signed copy is not ready yet.", 409)
    return FileResponse(store.path_of(row), media_type="application/pdf",
                        headers={"Content-Disposition": content_disposition(row["display_name"])})
