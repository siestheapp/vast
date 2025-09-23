from pydantic import AnyUrl, ValidationError, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="allow")

    database_url_ro: AnyUrl = Field(alias="DATABASE_URL_RO")
    database_url_rw: AnyUrl = Field(alias="DATABASE_URL_RW")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    openai_embedding_model: str = Field(default="text-embedding-3-large", alias="OPENAI_EMBEDDING_MODEL")
    default_statement_timeout_ms: int = Field(default=8000, alias="STATEMENT_TIMEOUT_MS")
    idle_in_tx_timeout_ms: int = Field(default=8000, alias="IDLE_TX_TIMEOUT_MS")
    max_write_rows: int = Field(default=50_000, alias="MAX_WRITE_ROWS")
    env: str = Field(default="dev", alias="VAST_ENV")
    read_role: str = Field(default="vast_ro", alias="VAST_READ_ROLE")
    write_url_override: AnyUrl | None = Field(default=None, alias="VAST_WRITE_URL")


try:
    settings = Settings()  # fail fast if missing
except ValidationError as exc:
    raise RuntimeError(f"Invalid configuration: {exc}") from exc


def read_url() -> str:
    url = getattr(settings, "database_url_ro", None)
    if not url:
        raise RuntimeError("DATABASE_URL_RO is not configured")
    return str(url)


def write_url() -> str:
    override = getattr(settings, "write_url_override", None)
    if override:
        return str(override)
    url = getattr(settings, "database_url_rw", None)
    if url:
        return str(url)
    raise RuntimeError("No write URL configured; set VAST_WRITE_URL or DATABASE_URL_RW")
