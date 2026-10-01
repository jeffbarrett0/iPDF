# iPDF — a personal, local replacement for ILovePDF Premium

Merge, split, reorder, rotate, compress, watermark, redact, OCR, annotate, convert Office files to PDF, and
collect simple signatures — **all on your own machine**. Files never leave it, nothing is uploaded to a vendor,
and there are no accounts, billing, telemetry or analytics.

> **⚠ Low-stakes use only.** iPDF is a personal tool. Its signatures are *simple* electronic records: no identity
> verification, not qualified/advanced/regulated e-signatures. Don't rely on it for contracts with legal weight,
> regulated records, or anything where a dispute would be costly. The same warning is shown in the app.

Stack: Next.js 15 + TypeScript (UI) · Python 3.12 + FastAPI + pikepdf (API) · LibreOffice headless (Office → PDF) · PostgreSQL (metadata and audit log).

## Quick start

```bash
./run.sh
```

Then open **http://localhost:3000**. On first run the script creates `.env` (with freshly generated random secrets),
creates a Python 3.12 virtualenv, installs dependencies, starts a project-local PostgreSQL, builds the web app and
starts both servers. `Ctrl+C` stops the servers (the local PostgreSQL keeps running; stop it with `./run.sh stop-db`).

**Prerequisites** (the script tells you what is missing):

| Need | Why | Install (Debian/Ubuntu) |
|---|---|---|
| `uv` *or* `python3.12` | Python 3.12 runtime | https://docs.astral.sh/uv/ |
| Node.js ≥ 20 + npm | web UI | https://nodejs.org |
| PostgreSQL ≥ 14 **server binaries** (or Docker) | database | `apt install postgresql` (the system service need not run; iPDF starts its own private cluster) |
| LibreOffice | Word/Excel/PowerPoint → PDF | `apt install libreoffice-writer libreoffice-calc libreoffice-impress` |
| Tesseract + Ghostscript | OCR | `apt install tesseract-ocr ghostscript` |

LibreOffice and Tesseract are optional: without them those two features report a clear, recoverable error and everything else works.
If no PostgreSQL binaries are found but Docker is, `docker-compose.yml` starts PostgreSQL in a container instead.
To use your own server, set `DATABASE_URL` in `.env`.

Other commands: `./run.sh dev` (hot reload) · `./run.sh test` · `./run.sh backup [dir]` · `./run.sh restore ARCHIVE` · `./run.sh stop-db`.

## What it does

| Area | Details |
|---|---|
| Merge / split / reorder / rotate | Merge N files in the order chosen; split by ranges, every N pages or per page; reorder (omitted pages are dropped, with a warning); rotate any pages. |
| Compress | Low (structure only), Medium (images ≤ 2000 px, JPEG 75), High (≤ 1200 px, JPEG 55). Reports before/after size. |
| Watermark | Text watermark with opacity, size, angle, colour, page selection; correct on rotated pages. |
| Redact | Find text or draw boxes. Redacted pages are **rasterized**, so the text is really gone (verified in tests); metadata, attachments and outlines are stripped. Pages you didn't redact keep their text. |
| OCR | OCRmyPDF + Tesseract, any installed language, optional re-OCR. |
| Convert | docx/doc/xlsx/xls/pptx/ppt/odt/ods/odp/rtf/txt → PDF via LibreOffice headless (isolated profile, timeout). |
| Annotate | Highlight, text box, sticky note, rectangle (real PDF annotations with appearance streams). |
| Previews | Page thumbnails/previews rendered by PDFium, cached by content hash. |
| Risk warnings | Before every operation you get a preflight for encryption, form fields, digital signatures, non-embedded fonts, scripts. |
| Signature mode | Place fields, invite signers (email if SMTP is configured, otherwise a link to share), record consent, seal the final PDF with a SHA-256 hash. |
| Audit | Append-only, hash-chained event log with timestamps, document hashes and delivery outcomes; one-click integrity check. |
| Lifecycle | Per-file expiry, download, delete, export (zip), backup/restore. |

### Warnings about what may change
Every output is a *new file*, so these are flagged up front (and stored with the output):
**digital signatures** become invalid in the copy; **encryption** is removed from the copy; **form fields** may be
dropped or stop being fillable when pages are copied (merge/split/reorder) or are flattened (redact); **non-embedded
fonts** may be substituted by other viewers and by the redaction/OCR rasterizer; LibreOffice **substitutes missing fonts**
on conversion; document-level scripts are not carried across merges. Password-protected PDFs are refused with
instructions (unlock them elsewhere first).

### Signature mode
1. Open a PDF → **Tools → Sign**, add signers, drag to place signature / date / text fields per signer.
2. **Create links & invite signers.** Each signer gets a private link (`/s/<token>`; only an HMAC of the token is stored; links expire after `SIGN_LINK_TTL_DAYS`). If SMTP is configured an email is sent; otherwise (or if delivery fails) the link is shown for you to share. Every delivery outcome (`sent` / `failed` / `not_configured`) is written to the event log.
3. The signer reads the document, ticks the consent statement (its text hash, time, IP and user-agent are recorded), types or draws a signature, and signs. They can also decline.
4. When the last signer signs, iPDF stamps the signatures onto a copy, appends a *Certificate of Completion* page, stores it as a new immutable version, and writes `sign.sealed` with the **SHA-256 of the final PDF** to the event log. (The final hash can't be printed on the page that is part of the hashed file; it lives in the log and on the request page.)

Signers reach the app through `PUBLIC_BASE_URL`. By default that is `http://localhost:3000`, so only people on this machine can open links. To let others sign, you must expose the web port yourself (LAN address or a tunnel you run) and set `PUBLIC_BASE_URL` accordingly — iPDF itself has no hosted component. Even then the management UI stays local-only (see Permissions).

**Deliberately not included:** qualified or otherwise regulated electronic signatures, government/ID verification, cryptographic PAdES/PKI signing, full Acrobat-level editing, and enterprise document governance (retention policies, roles, DLP).

## Architecture

```
Browser ──▶ Next.js (127.0.0.1:3000)  ── /api/* proxy ──▶ FastAPI (127.0.0.1:8000)
                                                            │
        ┌───────────────┬──────────────────┬────────────────┼──────────────────┐
     pikepdf        PDFium (previews,   LibreOffice      OCRmyPDF/        PostgreSQL (127.0.0.1:54329)
  (all transforms)  text search,        headless         Tesseract        files · events · sign_*
                    redaction raster)   (Office→PDF)
                                   DATA_DIR/originals · outputs   (read-only files on disk)
```

* `api/app/pdfops.py` – pure transformations (input paths → new file). `service.py` – validation, preflight warnings, versioned commit. `store.py` – the file store. `events.py` – hash-chained log. `signing.py` – signature workflow. `backup.py`, `exporting.py`, `expiry.py`, `localpg.py` – operations.
* **Originals are immutable:** stored `chmod 0444` in a `0555` directory; a database trigger refuses to change a file's hash/size/lineage; each use re-verifies the SHA-256 and refuses on mismatch.
* **Every operation writes a new versioned file** `v003-compress-<name>.pdf` with its parameters, source file IDs and hashes, warnings, and sizes recorded (`manifest.json` in exports).
* **The event log is append-only** (triggers reject UPDATE/DELETE/TRUNCATE) and **hash-chained** (each entry hashes the previous one), so edits made even with direct database access are detected by *Event log → Verify*.

## Data location

Everything lives under `DATA_DIR` (default `./data`) and `BACKUP_DIR` (default `./backups`); both are git-ignored.

```
data/originals/<id>/<name>      your uploads, read-only
data/outputs/<id>/v001-….pdf    every operation result, read-only
data/postgres/                  the managed PostgreSQL cluster (metadata, signatures, event log)
data/cache/previews/            disposable page renders (safe to delete)
data/tmp/                       scratch (cleaned automatically)
backups/ipdf-backup-<UTC>.tar.gz
```

Expiry: files are purged `FILE_TTL_DAYS` (default 30; `0` = never) after creation, checked every 10 minutes. Per file you can **Keep forever** or reset the timer. Purging deletes the bytes but keeps the record and event history (a tombstone with the hash). Files in open signature requests are never auto-expired. **Delete** does the same on demand.

## Backup, restore, export

* **Backup:** *Storage & backup → Create backup now*, or `./run.sh backup [dir]`. The archive holds a JSONL dump of every table plus all stored files and a `MANIFEST.json` with SHA-256 of each entry (no `pg_dump` needed). Copy it somewhere else — a backup on the same disk is not a backup.
* **Restore** into an **empty** install (new machine, or after deleting `data/`): `./run.sh restore backups/ipdf-backup-….tar.gz`. It verifies every checksum, re-imports rows verbatim, restores the files read-only, and re-verifies the event chain.
* **Export:** *Storage & backup → Export everything* (or *Export this file's whole history* on a file page) gives a plain zip: `originals/`, `outputs/`, `manifest.json` (hashes, parameters, lineage), `events.csv`, `events.jsonl`. It is readable without iPDF.
* Back up `.env` separately and securely — it contains your random secrets (`SECRET_KEY` is needed to keep existing signer links valid).

## Permissions & security model

* **Single-user, no accounts.** The management UI/API are for this computer only: servers bind to `127.0.0.1`, and the API rejects any request whose `Host`/`X-Forwarded-Host` isn't localhost (`ALLOW_REMOTE_ADMIN=true` disables that — don't, unless you add your own authentication). Only `/api/public/…` (token-gated signer routes) answer other hosts.
* **Files:** stored `0444` (files) / `0555` (their folders); backups `0600`; `.env` `0600`. Anyone with filesystem access to `DATA_DIR` can read your documents — use disk encryption for sensitive material.
* **PostgreSQL:** private cluster listening on `127.0.0.1` only, `scram-sha-256` auth, random password in `.env`.
* **Secrets:** only in `.env` (git-ignored); `.env.example` has placeholders. Nothing secret is committed or logged. Signer tokens are stored only as HMACs.
* **Inputs:** extension allow-list + magic-byte checks, size limit (`MAX_UPLOAD_MB`), safe filenames (path components, control and non-ASCII characters stripped; files are stored under UUID folders), strict validation of page ranges, rectangles, colours and text; Office files are converted in a throw-away profile directory with a timeout.
* **What signing records:** consent timestamp, IP (as reported by the connection/proxy headers — spoofable) and user-agent are stored locally with the request. No analytics, no third-party calls; the only outbound traffic is the SMTP server you configure.

## Configuration (`.env`)

See `.env.example` — every setting is documented there: storage paths, `FILE_TTL_DAYS`, `MAX_UPLOAD_MB`, ports, `PUBLIC_BASE_URL`, `ALLOW_REMOTE_ADMIN`, `SECRET_KEY`, `PG_*`/`DATABASE_URL`, SMTP, `SIGN_LINK_TTL_DAYS`, `OFFICE_TIMEOUT_SECONDS`, `OCR_TIMEOUT_SECONDS`, `OCR_LANGUAGES`.

## Tests

```bash
./run.sh test                 # all tests (starts a throw-away PostgreSQL cluster automatically)
./run.sh test -k happy_path   # just the end-to-end flow
```

Focused tests cover every core transformation (merge order, split plans, reorder, rotate, compress, watermark incl. rotated pages, redaction that truly removes text incl. rotated/offset pages, annotations), immutability and append-only/tamper-detection guarantees, upload validation, filename safety, and one end-to-end happy path over the real HTTP API (upload → merge → rotate → reorder → watermark → compress → annotate → split → redact → signature request → consent → sign → seal → event log → export → backup → delete/expire), plus Office conversion and OCR when LibreOffice/Tesseract are installed. Set `TEST_DATABASE_URL` to test against an existing server instead.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "The database is not reachable" banner | Re-run `./run.sh`; check `data/postgres/server.log`; or set `DATABASE_URL`. |
| Office files stored but not converted | Install LibreOffice Writer/Calc/Impress; click *Convert to PDF* in the library afterwards. |
| OCR disabled | Install `tesseract-ocr` (+ `tesseract-ocr-<lang>`) and `ghostscript`. |
| "This PDF needs a password" | Remove the password in another tool and upload the unlocked copy. |
| Port in use | Change `WEB_PORT` / `API_PORT` / `PG_PORT` in `.env`. |
| Running as root (containers) | PostgreSQL refuses root; iPDF runs it as the `postgres` user (`apt install postgresql` creates it). |

## Known limits

Operations run synchronously (very large OCR/convert jobs keep the request open until done, up to the configured timeouts). Watermark and typed-signature text use Latin (Windows-1252) characters only. Compression only recompresses plain RGB/gray/ICC images without masks. Restore needs an empty database. Drawn signatures are PNG images, not cryptographic signatures.
