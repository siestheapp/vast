from fastapi import APIRouter
import os
import time
import psycopg2
from psycopg2.extras import DictCursor
import urllib.parse as u

router = APIRouter()


CONNECT_TIMEOUT = int(os.getenv("HEALTH_CONNECT_TIMEOUT", "15"))
RETRY_BACKOFF_MS = int(os.getenv("HEALTH_RETRY_BACKOFF_MS", "350"))
TTL = int(os.getenv("HEALTH_CACHE_TTL_SEC", "10"))

# In-process cache for last successful payload
_HEALTH_CACHE = {"at": 0.0, "payload": None}


def _check_db_once(dsn: str):
    conn = None
    try:
        # Optional TCP keepalives (beneficial for some poolers)
        keepalive_kwargs = {}
        try:
            if os.getenv("HEALTH_PG_KEEPALIVES") is not None:
                keepalive_kwargs["keepalives"] = int(os.getenv("HEALTH_PG_KEEPALIVES"))
            if os.getenv("HEALTH_PG_KEEPALIVES_IDLE") is not None:
                keepalive_kwargs["keepalives_idle"] = int(os.getenv("HEALTH_PG_KEEPALIVES_IDLE"))
            if os.getenv("HEALTH_PG_KEEPALIVES_INTERVAL") is not None:
                keepalive_kwargs["keepalives_interval"] = int(os.getenv("HEALTH_PG_KEEPALIVES_INTERVAL"))
            if os.getenv("HEALTH_PG_KEEPALIVES_COUNT") is not None:
                keepalive_kwargs["keepalives_count"] = int(os.getenv("HEALTH_PG_KEEPALIVES_COUNT"))
        except Exception:
            keepalive_kwargs = {}

        conn = psycopg2.connect(
            dsn,
            connect_timeout=CONNECT_TIMEOUT,
            application_name="vast-health",
            sslmode=os.getenv("PGSSLMODE", "require"),
            **keepalive_kwargs,
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


def health_full_uncached():
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


@router.get("/health/full")
def health_full():
    now = time.time()
    last = _HEALTH_CACHE.get("payload")
    last_at = float(_HEALTH_CACHE.get("at") or 0.0)
    if last and (now - last_at) < TTL:
        return last
    payload = health_full_uncached()
    if payload.get("db_ok"):
        _HEALTH_CACHE["payload"] = payload
        _HEALTH_CACHE["at"] = now
    return payload
