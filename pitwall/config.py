"""Environment configuration. These are defaults; most can be overridden in the Settings UI."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tz: str = Field(default="America/New_York", alias="TZ")
    base_url: str = Field(default="http://localhost:8080", alias="PITWALL_BASE_URL")
    admin_password: str = Field(default="", alias="PITWALL_ADMIN_PASSWORD")
    ics_token: str = Field(default="", alias="PITWALL_ICS_TOKEN")
    secret_key: str = Field(default="", alias="PITWALL_SECRET_KEY")
    apprise_urls_email: str = Field(default="", alias="APPRISE_URLS_EMAIL")
    apprise_urls_webhook: str = Field(default="", alias="APPRISE_URLS_WEBHOOK")
    backfill_start_year: int = Field(default=2000, alias="PITWALL_BACKFILL_START_YEAR")
    log_level: str = Field(default="INFO", alias="PITWALL_LOG_LEVEL")
    data_dir: Path = Field(default=Path("data"), alias="PITWALL_DATA_DIR")
    database_url: str = Field(default="", alias="PITWALL_DATABASE_URL")
    scheduler_enabled: bool = Field(default=True, alias="PITWALL_SCHEDULER_ENABLED")
    secure_cookies: bool = Field(default=False, alias="PITWALL_SECURE_COOKIES")

    jolpica_base_url: str = Field(
        default="https://api.jolpi.ca/ergast/f1", alias="PITWALL_JOLPICA_URL"
    )

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            return "America/New_York"
        return v

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'pitwall.db').resolve()}"

    def resolved_secret_key(self) -> str:
        """Session-signing key. Uses env if set, else a random key persisted in the data dir."""
        if self.secret_key:
            return self.secret_key
        path = self.data_dir / ".secret_key"
        try:
            if path.exists():
                return path.read_text().strip()
            self.data_dir.mkdir(parents=True, exist_ok=True)
            key = secrets.token_urlsafe(48)
            path.write_text(key)
            path.chmod(0o600)
            return key
        except OSError:
            # Read-only data dir: fall back to an ephemeral key (sessions reset on restart).
            return secrets.token_urlsafe(48)

    def secret_values(self) -> list[str]:
        vals = [self.admin_password, self.ics_token, self.secret_key]
        for blob in (self.apprise_urls_email, self.apprise_urls_webhook):
            vals.extend(split_urls(blob))
        return [v for v in vals if v and len(v) >= 4]


def split_urls(blob: str) -> list[str]:
    """Apprise URL lists may be separated by commas, spaces or newlines."""
    return [u.strip() for u in blob.replace("\n", ",").replace(" ", ",").split(",") if u.strip()]


@lru_cache
def get_env() -> EnvSettings:
    return EnvSettings()
