"""Manage a project-local PostgreSQL cluster (no system service, no Docker needed).

`python -m app.localpg start` is what run.sh calls. If DATABASE_URL points elsewhere, this is a no-op.
"""
from __future__ import annotations

import glob
import os
import pwd
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import get_settings


def _bin_dir() -> Path:
    for pattern in ("/usr/lib/postgresql/*/bin", "/opt/homebrew/opt/postgresql@*/bin", "/usr/local/opt/postgresql@*/bin",
                    "/usr/pgsql-*/bin"):
        found = sorted(glob.glob(pattern))
        if found:
            return Path(found[-1])
    which = shutil.which("pg_ctl")
    if which:
        return Path(which).parent
    raise SystemExit("PostgreSQL server binaries not found. Install PostgreSQL 14+ (or set DATABASE_URL, "
                     "or run `docker compose up -d db`).")


def _runner() -> list[str]:
    """Postgres refuses to run as root; drop to the `postgres` user in that case."""
    if os.geteuid() == 0:
        try:
            pwd.getpwnam("postgres")
        except KeyError:
            raise SystemExit("Refusing to run PostgreSQL as root and no `postgres` user exists.") from None
        return ["runuser", "-u", "postgres", "--"]
    return []


def _prepare_dir(pgdata: Path) -> None:
    pgdata.parent.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        pw = pwd.getpwnam("postgres")
        pgdata.mkdir(exist_ok=True)
        for d in [pgdata, *pgdata.parents]:
            if d == Path("/"):
                break
            try:
                if not os.access(d, os.X_OK, effective_ids=False) or (d.stat().st_mode & 0o001) == 0:
                    os.chmod(d, d.stat().st_mode | 0o001)
            except PermissionError:
                pass
        os.chown(pgdata, pw.pw_uid, pw.pw_gid)
        os.chmod(pgdata, 0o700)


def is_ready(port: int) -> bool:
    exe = shutil.which("pg_isready") or str(_bin_dir() / "pg_isready")
    return subprocess.run([exe, "-h", "127.0.0.1", "-p", str(port)], capture_output=True).returncode == 0


def start(pgdata: Path | None = None, port: int | None = None, password: str | None = None) -> str:
    s = get_settings()
    port = port or s.pg_port
    password = password or s.pg_password
    pgdata = pgdata or s.data_dir / "postgres"
    bins = _bin_dir()
    run = _runner()
    _prepare_dir(pgdata)
    if not (pgdata / "PG_VERSION").exists():
        pwfile = pgdata.parent / ".pgpw"
        pwfile.write_text(password)
        os.chmod(pwfile, 0o644 if run else 0o600)
        try:
            subprocess.run([*run, str(bins / "initdb"), "-D", str(pgdata), "-U", "ipdf", "--auth=scram-sha-256",
                            f"--pwfile={pwfile}", "-E", "UTF8", "--no-locale"], check=True, capture_output=True)
        finally:
            pwfile.unlink(missing_ok=True)
    if not is_ready(port):
        log = pgdata / "server.log"
        opts = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories=/tmp"
        subprocess.run([*run, str(bins / "pg_ctl"), "-D", str(pgdata), "-l", str(log), "-o", opts, "-w", "start"],
                       check=True, capture_output=True)
    for _ in range(50):
        if is_ready(port):
            break
        time.sleep(0.2)
    # Create the application database once.
    env = {**os.environ, "PGPASSWORD": password}
    psql = shutil.which("psql") or str(bins / "psql")
    exists = subprocess.run([psql, "-h", "127.0.0.1", "-p", str(port), "-U", "ipdf", "-d", "postgres", "-tAc",
                             "SELECT 1 FROM pg_database WHERE datname='ipdf'"], capture_output=True, text=True, env=env)
    if exists.stdout.strip() != "1":
        subprocess.run([psql, "-h", "127.0.0.1", "-p", str(port), "-U", "ipdf", "-d", "postgres", "-c",
                        "CREATE DATABASE ipdf"], check=True, capture_output=True, env=env)
    return f"postgresql://ipdf:{password}@127.0.0.1:{port}/ipdf"


def stop(pgdata: Path | None = None) -> None:
    s = get_settings()
    pgdata = pgdata or s.data_dir / "postgres"
    if (pgdata / "postmaster.pid").exists():
        subprocess.run([*_runner(), str(_bin_dir() / "pg_ctl"), "-D", str(pgdata), "-m", "fast", "stop"],
                       capture_output=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if get_settings().database_url != f"postgresql://ipdf:{get_settings().pg_password}@127.0.0.1:{get_settings().pg_port}/ipdf":
        print("DATABASE_URL is set: using your PostgreSQL server; nothing to manage.")
    elif cmd == "start":
        print(start())
    elif cmd == "stop":
        stop()
    else:
        raise SystemExit("usage: python -m app.localpg [start|stop]")
