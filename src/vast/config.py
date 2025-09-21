from pydantic import BaseSettings, AnyUrl, ValidationError
import os
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseSettings):
    database_url_ro: AnyUrl
    database_url_rw: AnyUrl  # least-privilege: ONLY used for approved writes
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-large"
    default_statement_timeout_ms: int = 8000
    idle_in_tx_timeout_ms: int = 8000
    max_write_rows: int = 50_000  # gate for UPDATE/DELETE/MERGE etc.
    env: str = os.getenv("VAST_ENV", "dev")

    class Config:
        env_file = ".env"

settings = Settings()  # fail fast if missing
