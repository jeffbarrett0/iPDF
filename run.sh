#!/usr/bin/env bash
# iPDF — one command to set up and run everything locally.
#   ./run.sh              set up (first run) and start the app at http://localhost:3000
#   ./run.sh dev          same, but with the Next.js dev server (hot reload)
#   ./run.sh test         run the automated tests
#   ./run.sh backup [dir] write a restorable backup archive
#   ./run.sh restore FILE restore a backup into an EMPTY install
#   ./run.sh stop-db      stop the project-managed PostgreSQL
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
ROOT="$PWD"
CMD="${1:-start}"

say()  { printf '\033[1;34m[ipdf]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[ipdf] warning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[ipdf] error:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. .env (secrets live here and are never committed) -----------------------------
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  say "created .env from .env.example"
fi
rand() { python3 -c 'import secrets; print(secrets.token_hex(24))'; }
for key in PG_PASSWORD SECRET_KEY; do
  if grep -qE "^${key}=change-me$" .env; then
    sed -i.bak "s|^${key}=change-me$|${key}=$(rand)|" .env && rm -f .env.bak
    say "generated a random ${key} in .env"
  fi
done
set -a; source .env; set +a
WEB_PORT="${WEB_PORT:-3000}"; API_PORT="${API_PORT:-8000}"; API_HOST="${API_HOST:-127.0.0.1}"

# --- 2. Python 3.12 environment ---------------------------------------------------------
setup_python() {
  if [[ ! -x .venv/bin/python ]]; then
    if command -v uv >/dev/null; then
      say "creating Python 3.12 virtualenv (uv)"; uv venv --python 3.12 .venv
    elif command -v python3.12 >/dev/null; then
      say "creating Python 3.12 virtualenv"; python3.12 -m venv .venv
    else
      die "Python 3.12 is required. Install uv (https://docs.astral.sh/uv/) or python3.12, then re-run."
    fi
  fi
  local want; want="$(cat api/requirements-dev.txt api/requirements.txt | sha256sum | cut -d' ' -f1)"
  if [[ "$(cat .venv/.req-hash 2>/dev/null || true)" != "$want" ]]; then
    say "installing Python dependencies"
    if command -v uv >/dev/null; then uv pip install --python .venv/bin/python -r api/requirements-dev.txt
    else .venv/bin/python -m pip install -q -r api/requirements-dev.txt; fi
    echo "$want" > .venv/.req-hash
  fi
}

# --- 3. Node dependencies -----------------------------------------------------------------
setup_web() {
  command -v node >/dev/null && command -v npm >/dev/null || die "Node.js 20+ and npm are required."
  [[ "$(node -p 'process.versions.node.split(".")[0]')" -ge 20 ]] || die "Node.js 20 or newer is required."
  if [[ ! -d web/node_modules || web/package.json -nt web/node_modules/.package-stamp ]]; then
    say "installing web dependencies"; (cd web && npm install --no-audit --no-fund && touch node_modules/.package-stamp)
  fi
}

# --- 4. PostgreSQL --------------------------------------------------------------------------
start_db() {
  mkdir -p "${DATA_DIR:-./data}"
  if [[ -n "${DATABASE_URL:-}" ]]; then say "using DATABASE_URL from .env"; return; fi
  if ( cd api && ../.venv/bin/python -m app.localpg start >/dev/null 2>"$ROOT/.run-db.err" ); then
    say "PostgreSQL ready on 127.0.0.1:${PG_PORT:-54329} (data in ${DATA_DIR:-./data}/postgres)"
  elif command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    warn "no local PostgreSQL server binaries; starting PostgreSQL in Docker instead"
    docker compose up -d db
  else
    cat "$ROOT/.run-db.err" >&2 || true
    die "PostgreSQL is not available. Install PostgreSQL 14+ (server binaries), or Docker, or set DATABASE_URL in .env."
  fi
  rm -f "$ROOT/.run-db.err"
  ( cd api && ../.venv/bin/python - <<'PY'
import sys, time
from app.db import database_available
for _ in range(60):
    if database_available(): sys.exit(0)
    time.sleep(1)
sys.exit("database did not become ready")
PY
  )
}

check_tools() {
  command -v soffice >/dev/null || command -v libreoffice >/dev/null || warn "LibreOffice not found: Office→PDF conversion disabled (apt install libreoffice-writer libreoffice-calc libreoffice-impress)"
  command -v tesseract >/dev/null || warn "Tesseract not found: OCR disabled (apt install tesseract-ocr ghostscript)"
}

case "$CMD" in
  start|dev)
    setup_python; setup_web; start_db; check_tools
    if [[ "$CMD" == "start" ]]; then
      say "building the web app (first run takes a minute)"
      (cd web && API_PORT="$API_PORT" ./node_modules/.bin/next build >/dev/null)
    fi
    pids=()
    cleanup() { say "shutting down"; for p in "${pids[@]:-}"; do pkill -P "$p" 2>/dev/null || true; kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; }
    trap cleanup EXIT INT TERM
    ( cd api && exec ../.venv/bin/python -m uvicorn app.main:app --host "$API_HOST" --port "$API_PORT" --log-level warning ) & pids+=($!)
    ( cd web && API_PORT="$API_PORT" exec ./node_modules/.bin/next "$([[ $CMD == dev ]] && echo dev || echo start)" -H 127.0.0.1 -p "$WEB_PORT" ) & pids+=($!)
    say "iPDF is starting — open  http://localhost:${WEB_PORT}  (Ctrl+C to stop)"
    say "your files and database are in ${DATA_DIR:-./data}"
    wait -n
    ;;
  test)
    setup_python
    cd api && exec ../.venv/bin/python -m pytest "${@:2}"
    ;;
  backup)
    setup_python; start_db
    cd api && ../.venv/bin/python -m app.backup create "${@:2}"
    ;;
  restore)
    [[ -n "${2:-}" ]] || die "usage: ./run.sh restore ARCHIVE.tar.gz"
    setup_python; start_db
    cd api && ../.venv/bin/python -m app.backup restore "$(realpath "$2")"
    ;;
  stop-db)
    setup_python; cd api && ../.venv/bin/python -m app.localpg stop
    ;;
  *) die "unknown command '$CMD' (see the header of run.sh)";;
esac
