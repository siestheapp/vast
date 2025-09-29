from fastapi import APIRouter
import os
import time
import psycopg2
from psycopg2.extras import DictCursor
import urllib.parse as u

router = APIRouter()


CONNECT_TIMEOUT = int(os.getenv("HEALTH_CONNECT_TIMEOUT", "15"))
RETRY_BACKOFF_MS = int(os.getenv("HEALTH_RETRY_BACKOFF_MS", "350"))


def _check_db_once(dsn: str):
    conn = None
    try:
        conn = psycopg2.connect(
            dsn,
            connect_timeout=CONNECT_TIMEOUT,
            application_name="vast-health",
            sslmode=os.getenv("PGSSLMODE", "require"),
        )
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
        return True, None
    except Exception as e:
        return False, str(e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@router.get("/health/full")
def health_full():
    dsn = os.environ.get("DATABASE_URL_RO") or os.environ.get("DATABASE_URL", "")
    host = u.urlsplit(dsn).hostname or "unknown"
    project_name = os.getenv("VAST_PROJECT_NAME")

    ok, err = _check_db_once(dsn) if dsn else (False, "no DATABASE_URL configured")
    if not ok:
        time.sleep(RETRY_BACKOFF_MS / 1000.0)
        ok, err = _check_db_once(dsn)

    return {
        "api_ok": True,
        "db_ok": ok,
        "error": None if ok else (err[:300] if err else "unknown"),
        "db": {
            "host": host,
            "project_name": project_name,
        },
    }
