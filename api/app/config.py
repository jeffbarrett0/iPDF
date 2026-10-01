from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    data_dir: Path = Path("data")
    backup_dir: Path = Path("backups")
    file_ttl_days: int = 30
    max_upload_mb: int = 200

    database_url: str = ""
    pg_port: int = 54329
    pg_password: str = "change-me"

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    web_port: int = 3000
    public_base_url: str = "http://localhost:3000"
    allow_remote_admin: bool = False

    secret_key: str = "change-me"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    sign_link_ttl_days: int = 14

    office_timeout_seconds: int = 120
    ocr_timeout_seconds: int = 600
    ocr_languages: str = "eng"

    @model_validator(mode="after")
    def _resolve(self) -> "Settings":
        # Relative paths are relative to the repository root, not the CWD.
        if not self.data_dir.is_absolute():
            self.data_dir = (REPO_ROOT / self.data_dir).resolve()
        if not self.backup_dir.is_absolute():
            self.backup_dir = (REPO_ROOT / self.backup_dir).resolve()
        if not self.database_url:
            self.database_url = (
                f"postgresql://ipdf:{self.pg_password}@127.0.0.1:{self.pg_port}/ipdf"
            )
        self.public_base_url = self.public_base_url.rstrip("/")
        return self

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
