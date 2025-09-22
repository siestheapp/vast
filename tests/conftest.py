import os
import sys
import types
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(dotenv_path=".env", override=False)

os.environ.setdefault("DATABASE_URL_RO", "postgresql+psycopg://vast_ro:vast_ro_pwd@localhost:5433/pagila")
os.environ.setdefault("DATABASE_URL_RW", "postgresql+psycopg://vast_ro:vast_ro_pwd@localhost:5433/pagila")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

if "src.vast.service" not in sys.modules:
    fake_service = types.ModuleType("src.vast.service")

    def _dump_response(**overrides):
        base = {"ok": False, "artifacts": [], "stderr": "", "stdout": "", "command": "pg_dump"}
        base.update(overrides)
        return base

    fake_service.create_dump = lambda *args, **kwargs: _dump_response()
    fake_service.list_artifacts = lambda: []
    fake_service.repo_write = lambda *args, **kwargs: {"ok": True}
    fake_service.apply_sql = lambda *args, **kwargs: {"ok": True, "stderr": "", "stdout": ""}
    fake_service._assert_privileges = lambda: None
    sys.modules["src.vast.service"] = fake_service
