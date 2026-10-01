#!/usr/bin/env bash
# Mac/Linux: run the whole app in Docker.  ./start.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
command -v docker >/dev/null || { echo "Docker is not installed: https://www.docker.com/products/docker-desktop/"; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker is not running. Start Docker Desktop and retry."; exit 1; }
if [[ ! -f .env ]]; then
  cp .env.example .env; chmod 600 .env
  for k in PG_PASSWORD SECRET_KEY; do
    sed -i.bak "s|^$k=change-me\$|$k=$(openssl rand -hex 24)|" .env && rm -f .env.bak
  done
fi
mkdir -p backups
docker compose up -d --build --wait
echo "Ready: http://localhost:${WEB_PORT:-3000}   (stop with: docker compose down)"
