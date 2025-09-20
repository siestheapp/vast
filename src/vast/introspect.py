from __future__ import annotations
from hashlib import sha1
from sqlalchemy import inspect, text
from .db import get_engine

def list_tables() -> list[dict]:
    insp = inspect(get_engine())
    out: list[dict] = []
    for schema in insp.get_schema_names():
        if schema in ("pg_catalog", "information_schema", "pg_toast"):
            continue
        for t in insp.get_table_names(schema=schema):
            out.append({"table_schema": schema, "table_name": t})
    return sorted(out, key=lambda r: (r["table_schema"], r["table_name"]))

def table_columns(schema: str, table: str) -> list[dict]:
    insp = inspect(get_engine())
    cols = insp.get_columns(table_name=table, schema=schema)
    out = []
    for c in cols:
        out.append({
            "column_name": c.get("name"),
            "data_type": str(c.get("type")),
            "is_nullable": "YES" if c.get("nullable", True) else "NO",
            "column_default": c.get("default"),
        })
    return out

def schema_fingerprint() -> str:
    """
    Deterministic fingerprint of (schema, table, column, type).
    If schema changes, this hash will change → cache refresh.
    """
    sql = """
    SELECT t.table_schema, t.table_name, c.column_name, c.data_type
    FROM information_schema.tables t
    JOIN information_schema.columns c
      ON c.table_schema = t.table_schema AND c.table_name = t.table_name
    WHERE t.table_schema NOT IN ('pg_catalog','information_schema','pg_toast')
    ORDER BY t.table_schema, t.table_name, c.ordinal_position;
    """
    with get_engine().begin() as conn:
        rows = conn.execute(text(sql)).all()
    h = sha1()
    for r in rows:
        h.update("|".join(map(str, r)).encode("utf-8"))
    return h.hexdigest()
