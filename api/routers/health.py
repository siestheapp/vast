from fastapi import APIRouter
import os
import psycopg2
import urllib.parse as u

router = APIRouter()


@router.get("/health/full")
def health_full():
    dsn = os.environ.get("DATABASE_URL_RO", "")
    host = u.urlsplit(dsn).hostname or "unknown"
    try:
        conn = psycopg2.connect(dsn, connect_timeout=5, sslmode="require")
        with conn, conn.cursor() as cur:
            cur.execute("select current_user, current_database(), inet_server_addr()::text;")
            user, db, srv = cur.fetchone()
        return {
            "api_ok": True,
            "db_ok": True,
            "error": None,
            "db": {
                "user": user,
                "database": db,
                "host": srv or host,
                "schema": os.getenv("VAST_SCHEMA_INCLUDE", "public"),
            },
        }
    except Exception as e:
        return {
            "api_ok": True,
            "db_ok": False,
            "error": str(e),
            "db": {"host": host},
        }
