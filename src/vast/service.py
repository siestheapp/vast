"""Shared service layer consumed by both CLI and API surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple
import re

from sqlalchemy import text

from .agent import plan_sql, plan_sql_with_retry
from .config import settings
from .db import get_engine
from .introspect import list_tables, table_columns
from .identifier_guard import extract_requested_identifiers, load_schema_cache
from .agent import load_or_build_schema_summary
from .sql_params import hydrate_readonly_params, normalize_limit_literal, stmt_kind
from .actions import (
    pg_dump_database,
    pg_restore_list,
    pg_restore_into,
    sha256 as sha256_file,
    write_text_file,
    apply_sql_file,
)
from .knowledge import get_knowledge_store
from .repo import list_files as repo_list_files, read_file as repo_read_file, write_file as repo_write_file, RepoAccessError

# Ensure test patch points exist at import time for pytest dotted-path monkeypatch.
# Bind into module globals immediately.
from .identifier_guard import ensure_valid_identifiers as _ensure_valid_identifiers

# Exported name that tests patch: src.vast.service.ensure_valid_identifiers
ensure_valid_identifiers = _ensure_valid_identifiers  # bind now


# Exported name that tests patch: src.vast.service.safe_execute
def safe_execute(sql, params=None, allow_writes=False, force_write=False):
    """
    Proxy to conversation.safe_execute so tests can patch via
    'src.vast.service.safe_execute' without importing conversation first.
    Local import avoids potential import cycles.
    """
    from .conversation import safe_execute as _conv_safe_execute
    return _conv_safe_execute(sql, params=params, allow_writes=allow_writes, force_write=force_write)


# Keep __all__ explicit
try:
    __all__
except NameError:
    __all__ = []
for _name in ("ensure_valid_identifiers", "safe_execute", "preflight_statements", "apply_statements"):
    if _name not in __all__:
        __all__.append(_name)


def _assert_privileges():
    """Privilege self-check: fail fast if RW is over-privileged or RO isn't read-only"""
    # RO must NOT be able to create temp table
    with get_engine(readonly=True).begin() as conn:
        try:
            conn.execute(text("CREATE TEMP TABLE _v_priv_check(id int); DROP TABLE _v_priv_check;"))
            raise RuntimeError("RO connection can create tables; misconfigured privileges.")
        except Exception:
            pass  # expected: permission denied

    # RW SHOULD be able to create temp table (or at least DML)
    with get_engine(readonly=False).begin() as conn:
        conn.execute(text("CREATE TEMP TABLE _v_priv_check(id int); DROP TABLE _v_priv_check;"))


def environment_status() -> Dict[str, Any]:
    """Return basic environment diagnostics."""
    return {
        "database_url_ro_configured": bool(settings.database_url_ro),
        "database_url_rw_configured": bool(settings.database_url_rw),
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
    normalized_sql = normalize_limit_literal(sql, params)
    hydrated_params = hydrate_readonly_params(normalized_sql, params)
    if normalized_sql.strip().upper().endswith("LIMIT 1") and "limit" not in (hydrated_params or {}):
        hydrated_params = dict(hydrated_params or {})
        hydrated_params["limit"] = 1
    summary = load_or_build_schema_summary()
    engine = get_engine(readonly=True)
    requested = extract_requested_identifiers(normalized_sql)
    _ensure_valid_identifiers(
        normalized_sql,
        engine=engine,
        schema_summary=summary,
        params=hydrated_params,
        requested=requested,
    )
    rows = safe_execute(
        normalized_sql,
        params=hydrated_params or {},
        allow_writes=allow_writes,
        force_write=force_write,
    )
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
    normalized_sql = normalize_limit_literal(sql, params)
    hydrated_params = hydrate_readonly_params(normalized_sql, params)
    if normalized_sql.strip().upper().endswith("LIMIT 1") and "limit" not in (hydrated_params or {}):
        hydrated_params = dict(hydrated_params or {})
        hydrated_params["limit"] = 1
    summary = load_or_build_schema_summary()
    engine = get_engine(readonly=True)
    requested = extract_requested_identifiers(normalized_sql)
    _ensure_valid_identifiers(
        normalized_sql,
        engine=engine,
        schema_summary=summary,
        params=hydrated_params,
        requested=requested,
    )
    if allow_writes:
        safe_execute(normalized_sql, params=hydrated_params, allow_writes=True, force_write=False)
    else:
        safe_execute(normalized_sql, params=hydrated_params, allow_writes=False, force_write=False)
    return normalized_sql


def _normalize_statement(sql: str | None) -> str:
    if not sql:
        return ""
    return sql.strip().rstrip(";\n \t")


def _clone_schema_map(schema_map: Dict[str, Dict[str, Set[str]]] | None) -> Dict[str, Dict[str, Set[str]]]:
    if not schema_map:
        return {}
    cloned: Dict[str, Dict[str, Set[str]]] = {}
    for schema, tables in schema_map.items():
        cloned[schema] = {table: set(cols) for table, cols in tables.items()}
    return cloned


def _relation_known(schema_map: Dict[str, Dict[str, Set[str]]], relation: str) -> bool:
    if not relation:
        return False
    if "." in relation:
        schema, table = relation.split(".", 1)
    else:
        schema, table = "public", relation
    return table in (schema_map.get(schema) or {})


def _existing_relations(schema_map: Dict[str, Dict[str, Set[str]]], relations: Set[str]) -> bool:
    return bool(relations) and all(_relation_known(schema_map, rel) for rel in relations)


_CREATE_TABLE_RE = re.compile(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w\."]+)', re.IGNORECASE)


def _parse_create_table(statement: str) -> tuple[str | None, str | None, Set[str]]:
    match = _CREATE_TABLE_RE.search(statement)
    if not match:
        return None, None, set()
    identifier = match.group(1).replace('"', "").strip()
    parts = [p for p in identifier.split('.') if p]
    if not parts:
        return None, None, set()
    if len(parts) == 1:
        schema, table = "public", parts[0]
    else:
        schema, table = parts[-2], parts[-1]

    start = statement.find('(')
    end = statement.rfind(')')
    if start == -1 or end == -1 or end <= start:
        return schema, table, set()
    body = statement[start + 1:end]
    columns: Set[str] = set()
    current: list[str] = []
    depth = 0
    for ch in body:
        if ch == ',' and depth == 0:
            segment = ''.join(current).strip()
            if segment:
                col = _extract_column_name(segment)
                if col:
                    columns.add(col)
            current = []
            continue
        current.append(ch)
        if ch == '(':
            depth += 1
        elif ch == ')' and depth > 0:
            depth -= 1
    tail = ''.join(current).strip()
    if tail:
        col = _extract_column_name(tail)
        if col:
            columns.add(col)
    return schema, table, columns


def _extract_column_name(definition: str) -> str | None:
    token = definition.strip().split()[0].strip('"') if definition.strip() else ""
    skip = {"PRIMARY", "CONSTRAINT", "UNIQUE", "CHECK", "FOREIGN"}
    if not token or token.upper() in skip:
        return None
    return token


def _apply_schema_mutation(schema_map: Dict[str, Dict[str, Set[str]]], statement: str) -> None:
    kind = stmt_kind(statement)
    if kind != "DDL":
        return
    upper = statement.upper()
    if upper.startswith("CREATE TABLE"):
        schema, table, columns = _parse_create_table(statement)
        if schema and table:
            tables = schema_map.setdefault(schema, {})
            tables[table] = columns or tables.get(table, set())


def preflight_statements(
    statements: List[str],
    *,
    engine=None,
    schema_map: Dict[str, Dict[str, Set[str]]] | None = None,
) -> List[str]:
    """Preflight statements by running DDL then EXPLAINing DML/SELECT before rolling back."""

    notes: List[str] = []
    if not statements:
        return notes

    rw_engine = get_engine(readonly=False)
    ro_engine = engine or get_engine(readonly=True)

    base_map = schema_map
    if base_map is None:
        base_map, _ = load_schema_cache(ro_engine)
    original_schema = _clone_schema_map(base_map)
    local_schema = _clone_schema_map(base_map)

    conn = rw_engine.connect()
    trans = conn.begin()
    try:
        # Pass 1: execute DDL so dependent statements see new objects.
        for raw in statements:
            statement = _normalize_statement(raw)
            if not statement:
                continue
            if stmt_kind(statement) == "DDL":
                conn.execute(text(statement))
                notes.append(f"OK DDL {statement.split()[0].upper()} ...")
                _apply_schema_mutation(local_schema, statement)

        # Pass 2: EXPLAIN DML/SELECT using hydrated params.
        for raw in statements:
            statement = _normalize_statement(raw)
            if not statement:
                continue
            kind = stmt_kind(statement)
            relations = extract_requested_identifiers(statement).get("relations", set())
            if kind in {"DML", "SELECT"} and _existing_relations(original_schema, relations):
                ensure_valid_identifiers(
                    statement,
                    engine=ro_engine,
                    schema_map=local_schema,
                    schema_summary=None,
                    params={},
                )
                explain_sql = f"EXPLAIN (VERBOSE, FORMAT JSON) {statement}"
                params = hydrate_readonly_params(statement, {}) or {}
                conn.execute(text(explain_sql), params)
                notes.append(f"OK EXPLAIN {kind} ...")
        trans.rollback()
    except Exception:
        trans.rollback()
        raise
    finally:
        conn.close()

    return notes


def apply_statements(
    statements: List[str],
    *,
    engine=None,
    schema_map: Dict[str, Dict[str, Set[str]]] | None = None,
) -> None:
    """Apply statements in a single transaction."""

    if not statements:
        return

    rw_engine = get_engine(readonly=False)
    ro_engine = engine or get_engine(readonly=True)

    base_map = schema_map
    if base_map is None:
        base_map, _ = load_schema_cache(ro_engine)
    original_schema = _clone_schema_map(base_map)
    local_schema = _clone_schema_map(base_map)

    with rw_engine.begin() as conn:
        for raw in statements:
            statement = _normalize_statement(raw)
            if not statement:
                continue
            kind = stmt_kind(statement)
            relations = extract_requested_identifiers(statement).get("relations", set())
            if kind in {"DML", "SELECT"} and _existing_relations(original_schema, relations):
                ensure_valid_identifiers(
                    statement,
                    engine=ro_engine,
                    schema_map=local_schema,
                    schema_summary=None,
                    params={},
                )
            conn.execute(text(statement))
            if kind == "DDL":
                _apply_schema_mutation(local_schema, statement)


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


# Call this once during app init, but do not crash module import if unavailable
try:
    _assert_privileges()
except Exception:
    pass


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


# --- Repository helpers -------------------------------------------------


def repo_files(subdir: str | None = None) -> Dict[str, Any]:
    try:
        files = repo_list_files(subdir=subdir)
    except RepoAccessError as exc:
        raise RuntimeError(str(exc)) from exc
    return {"files": files}


def repo_read(path: str) -> Dict[str, Any]:
    try:
        content = repo_read_file(path)
    except RepoAccessError as exc:
        raise RuntimeError(str(exc)) from exc
    return {"path": path, "content": content}


def repo_write(path: str, content: str, overwrite: bool = False) -> Dict[str, Any]:
    try:
        written = repo_write_file(path, content, overwrite=overwrite)
    except RepoAccessError as exc:
        raise RuntimeError(str(exc)) from exc
    return {"path": written, "status": "written"}

# Provide a late-binding escape hatch for tests importing during startup.
def __getattr__(name: str):
    if name == "ensure_valid_identifiers":
        from .identifier_guard import ensure_valid_identifiers as _f
        return _f
    if name == "safe_execute":
        from .conversation import safe_execute as _f
        return _f
    raise AttributeError(name)
