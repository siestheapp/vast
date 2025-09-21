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


try:
    settings = Settings()  # fail fast if missing
except ValidationError as exc:
    raise RuntimeError(f"Invalid configuration: {exc}") from exc
