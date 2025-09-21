"""Shared service layer consumed by both CLI and API surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .agent import plan_sql, plan_sql_with_retry
from .config import settings
from .db import safe_execute
from .introspect import list_tables, table_columns
from .actions import (
    pg_dump_database,
    pg_restore_list,
    pg_restore_into,
    sha256 as sha256_file,
    write_text_file,
    apply_sql_file,
)
from .knowledge import get_knowledge_store


def environment_status() -> Dict[str, Any]:
    """Return basic environment diagnostics."""
    return {
        "database_url_configured": bool(settings.database_url),
        "vast_env": settings.env,
        "openai_configured": bool(settings.openai_api_key),
        "openai_model": settings.openai_model,
    }


def tables() -> List[Dict[str, str]]:
    return list_tables()


def columns(schema: str, table: str) -> List[Dict[str, Any]]:
    return table_columns(schema, table)


def _serialize_rows(rows: List[Any]) -> Tuple[List[Any], bool]:
    """Convert SQLAlchemy rows into JSON-serialisable structures."""
    dry_run = False
    serialised: List[Any] = []
    for row in rows:
        if isinstance(row, dict):
            if row.get("_notice"):
                dry_run = True
            serialised.append(row)
            continue
        mapping = getattr(row, "_mapping", None)
        if mapping is not None:
            serialised.append(dict(mapping))
        else:
            serialised.append(row)
    return serialised, dry_run


def execute_sql(
    sql: str,
    params: Dict[str, Any] | None = None,
    allow_writes: bool = False,
    force_write: bool = False,
) -> Dict[str, Any]:
    """Run SQL with guardrails and return structured output."""
    rows = safe_execute(sql, params=params or {}, allow_writes=allow_writes, force_write=force_write)
    serialised, dry_run = _serialize_rows(rows)
    return {
        "rows": serialised,
        "row_count": 0 if dry_run else len(serialised),
        "dry_run": dry_run,
    }


def plan_and_execute(
    nl_request: str,
    params: Dict[str, Any] | None = None,
    allow_writes: bool = False,
    force_write: bool = False,
    refresh_schema: bool = False,
    retry: bool = True,
    max_retries: int = 2,
) -> Dict[str, Any]:
    """Plan SQL using the agent and execute it, returning SQL and results."""

    param_hints = params or {}

    if retry:
        sql = plan_sql_with_retry(
            nl_request,
            allow_writes=allow_writes,
            force_refresh_schema=refresh_schema,
            param_hints=param_hints,
            max_retries=max_retries,
            validator=_validation_executor,
        )
    else:
        sql = plan_sql(
            nl_request,
            allow_writes=allow_writes,
            force_refresh_schema=refresh_schema,
            param_hints=param_hints,
        )

    execution = execute_sql(sql, params=param_hints, allow_writes=allow_writes, force_write=force_write)
    return {
        "sql": sql,
        "execution": execution,
    }


def _validation_executor(sql: str, params: Dict[str, Any], allow_writes: bool) -> None:
    """Validate generated SQL without forcing writes to run."""
    if allow_writes:
        safe_execute(sql, params=params, allow_writes=True, force_write=False)
    else:
        safe_execute(sql, params=params, allow_writes=False, force_write=False)


# --- Operations helpers ---------------------------------------------------

def list_artifacts() -> List[str]:
    art_dir = Path(".vast/artifacts")
    if not art_dir.exists():
        return []
    return sorted(str(p) for p in art_dir.iterdir() if p.is_file())


def create_dump(outfile: str | None = None, container_name: str = "vast-pg", fmt: str = "custom") -> Dict[str, Any]:
    return pg_dump_database(outfile=outfile, container_name=container_name, fmt=fmt)


def restore_list(dumpfile: str) -> Dict[str, Any]:
    return pg_restore_list(dumpfile)


def restore_into(dumpfile: str, target_db_url: str, drop: bool = False) -> Dict[str, Any]:
    return pg_restore_into(dumpfile, target_db_url, drop=drop)


def sha256(path: str) -> Dict[str, Any]:
    return sha256_file(path)


def write_file(path: str, content: str) -> Dict[str, Any]:
    return write_text_file(path, content)


def apply_sql(path: str) -> Dict[str, Any]:
    return apply_sql_file(path)


def load_conversation(session_name: str) -> Dict[str, Any] | None:
    path = Path(".vast/conversations") / f"{session_name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


# --- Knowledge helpers --------------------------------------------------


def ensure_knowledge_snapshot(force: bool = False) -> Dict[str, Any]:
    store = get_knowledge_store()
    snapshot = store.capture_schema_snapshot(force=force)
    return {
        "id": snapshot.id,
        "fingerprint": snapshot.fingerprint,
        "created_at": snapshot.created_at,
        "summary": snapshot.summary,
    }


def list_knowledge_snapshots(limit: int = 20) -> List[Dict[str, Any]]:
    store = get_knowledge_store()
    snaps = store.list_snapshots(limit=limit)
    return [
        {
            "id": snap.id,
            "fingerprint": snap.fingerprint,
            "created_at": snap.created_at,
            "summary": snap.summary,
        }
        for snap in snaps
    ]


def knowledge_search(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    store = get_knowledge_store()
    results = store.search(query, top_k=top_k)
    return [
        {
            "id": entry.id,
            "type": entry.type,
            "title": entry.title,
            "content": entry.content,
            "metadata": entry.metadata,
            "updated_at": entry.updated_at,
        }
        for entry in results
    ]


def list_knowledge_entries(entry_type: str | None = None, limit: int = 50) -> List[Dict[str, Any]]:
    store = get_knowledge_store()
    entries = store.list_entries(entry_type=entry_type, limit=limit)
    return [
        {
            "id": entry.id,
            "type": entry.type,
            "title": entry.title,
            "content": entry.content,
            "metadata": entry.metadata,
            "updated_at": entry.updated_at,
        }
        for entry in entries
    ]
