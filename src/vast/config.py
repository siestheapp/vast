from __future__ import annotations

import os
from typing import Any, Dict

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment/.env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow",
    )

    OPENAI_API_KEY: str | None = None
    DATABASE_URL_RO: str
    DATABASE_URL_RW: str | None = None
    VAST_DIAGNOSTICS: int | bool = 0
    VAST_SCHEMA_INCLUDE: str = "public"

    # Legacy fields kept for backward compatibility
    default_statement_timeout_ms: int = 8_000
    idle_in_tx_timeout_ms: int = 8_000
    max_write_rows: int = 50_000
    env: str = "dev"
    read_role: str = "vast_ro"
    write_url_override: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-large"

    @property
    def openai_api_key(self) -> str:
        return self.OPENAI_API_KEY or ""

    @openai_api_key.setter
    def openai_api_key(self, value: str | None) -> None:
        object.__setattr__(self, "OPENAI_API_KEY", value)

    @property
    def database_url_ro(self) -> str:
        return self.DATABASE_URL_RO

    @database_url_ro.setter
    def database_url_ro(self, value: str) -> None:
        object.__setattr__(self, "DATABASE_URL_RO", value)

    @property
    def database_url_rw(self) -> str | None:
        return self.DATABASE_URL_RW

    @database_url_rw.setter
    def database_url_rw(self, value: str | None) -> None:
        object.__setattr__(self, "DATABASE_URL_RW", value)


def _merge_legacy_env(env: Dict[str, Any]) -> Dict[str, Any]:
    """Populate DATABASE_URL_RO from DATABASE_URL when only legacy var is set."""

    if "DATABASE_URL_RO" not in env and "DATABASE_URL" in env:
        env = dict(env)  # copy to avoid mutating caller
        env["DATABASE_URL_RO"] = env["DATABASE_URL"]
    return env


_raw_env = _merge_legacy_env(dict(os.environ))
try:
    settings = Settings(
        **_raw_env,
        _env_file=".env",
        _env_file_encoding="utf-8",
    )
except Exception as exc:  # pragma: no cover - config errors surfaced early
    raise RuntimeError(f"Invalid configuration: {exc}") from exc


def get_ro_url() -> str:
    """Return the read-only connection URL."""

    return settings.DATABASE_URL_RO


def get_rw_url() -> str:
    """Return the read-write URL, falling back to the read-only URL."""

    return settings.DATABASE_URL_RW or settings.DATABASE_URL_RO


def read_url() -> str:
    """Backward-compatible helper returning the read-only URL."""

    return get_ro_url()


def write_url() -> str:
    """Backward-compatible helper returning the preferred write URL."""

    override = getattr(settings, "write_url_override", None)
    if override:
        return override
    if settings.DATABASE_URL_RW:
        return settings.DATABASE_URL_RW
    return settings.DATABASE_URL_RO
