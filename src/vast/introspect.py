from __future__ import annotations

import logging
from hashlib import sha1
from typing import List

from sqlalchemy import String, bindparam, inspect, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import ProgrammingError

try:  # psycopg2 is optional at runtime (e.g., under async drivers)
    from psycopg2 import errors as psycopg2_errors
except Exception:  # pragma: no cover - psycopg2 not installed
    psycopg2_errors = None  # type: ignore[assignment]

from .config import settings
from .db import get_engine

logger = logging.getLogger(__name__)

INCLUDE_SCHEMAS: List[str] = [
    s.strip() for s in (settings.VAST_SCHEMA_INCLUDE or "public").split(",") if s.strip()
] or ["public"]


def has_schema_usage(engine, schema: str) -> bool:
    """Return True if the current role has USAGE on the schema."""

    query = text("select has_schema_privilege(current_user, :s, 'USAGE') as ok")
    try:
        with engine.connect() as conn:
            result = conn.execute(query, {"s": schema}).scalar()
            return bool(result)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed schema privilege check for %s: %s", schema, exc)
        return False


def _eligible_schemas(engine) -> List[str]:
    allowed: List[str] = []
    for schema in INCLUDE_SCHEMAS:
        if has_schema_usage(engine, schema):
            allowed.append(schema)
        else:
            logger.debug("Skipping schema %s due to missing USAGE privilege", schema)
    return allowed


def list_tables() -> list[dict]:
    engine = get_engine(readonly=True)
    schemas = _eligible_schemas(engine)
    if not schemas:
        return []

    stmt = text(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema = ANY(:schemas)
          AND table_type = 'BASE TABLE'
        ORDER BY table_schema, table_name
        """
    ).bindparams(bindparam("schemas", type_=ARRAY(String)))

    with engine.connect() as conn:
        rows = conn.execute(stmt, {"schemas": schemas}).mappings().all()

    return [
        {"table_schema": row["table_schema"], "table_name": row["table_name"]}
        for row in rows
    ]


def _is_privilege_error(exc: Exception) -> bool:
    if psycopg2_errors and isinstance(getattr(exc, "orig", None), psycopg2_errors.InsufficientPrivilege):
        return True
    message = str(exc).lower()
    return (
        "permission denied" in message
        or "must be owner" in message
        or "does not exist" in message
    )


def table_columns(schema: str, table: str) -> list[dict]:
    engine = get_engine(readonly=True)
    insp = inspect(engine)
    try:
        cols = insp.get_columns(table_name=table, schema=schema)
    except ProgrammingError as exc:
        if _is_privilege_error(exc):
            logger.debug("Skipping %s.%s due to reflection error: %s", schema, table, exc)
            return []
        raise
    except Exception as exc:  # pragma: no cover - defensive
        if _is_privilege_error(exc):
            logger.debug("Skipping %s.%s due to reflection error: %s", schema, table, exc)
            return []
        raise

    out: list[dict] = []
    for c in cols:
        out.append(
            {
                "column_name": c.get("name"),
                "data_type": str(c.get("type")),
                "is_nullable": "YES" if c.get("nullable", True) else "NO",
                "column_default": c.get("default"),
            }
        )
    return out


def schema_fingerprint() -> str:
    """Deterministic fingerprint of (schema, table, column, type)."""

    tables = list_tables()
    h = sha1()
    for entry in tables:
        schema = entry["table_schema"]
        table = entry["table_name"]
        cols = table_columns(schema, table)
        if not cols:
            h.update(f"{schema}|{table}|<no-columns>".encode("utf-8"))
            continue
        for col in cols:
            h.update(
                "|".join(
                    [
                        schema,
                        table,
                        str(col.get("column_name")),
                        str(col.get("data_type")),
                    ]
                ).encode("utf-8")
            )
    return h.hexdigest()
