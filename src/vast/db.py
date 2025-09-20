from __future__ import annotations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from .config import settings

_engine: Engine | None = None

def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = settings.database_url.strip()
        if url.startswith("DATABASE_URL="):
            url = url.split("=", 1)[1]
        if not url:
            raise RuntimeError("DATABASE_URL missing. Set it in .env")
        _engine = create_engine(url, pool_pre_ping=True)
    return _engine

def _is_write(sql: str) -> bool:
    s = sql.strip().lower()
    return s.startswith("insert") or s.startswith("update")

def _is_disallowed(sql: str) -> bool:
    s = sql.strip().lower()
    forbidden = (" drop ", " alter ", " truncate ", " create ", " grant ", " revoke ", " delete ")
    return any(tok in s or s.startswith(tok.strip()) for tok in forbidden)

def safe_execute(
    sql: str,
    params: dict | None = None,
    allow_writes: bool = False,
    force_write: bool = False,
):
    """
    - Blocks DDL/DELETE always.
    - If write and allow_writes=False -> block.
    - If write and force_write=False -> DRY RUN (return a preview).
    """
    if _is_disallowed(sql):
        raise ValueError("DDL/DELETE statements are blocked in Vast1.")

    if _is_write(sql):
        if not allow_writes:
            raise ValueError("Write queries are disabled. Use --write to permit writes.")
        if not force_write:
            return [{"_notice": "DRY RUN — not executed", "_sql": sql, "_params": params or {}}]

    with get_engine().begin() as conn:
        return list(conn.execute(text(sql), params or {}))
