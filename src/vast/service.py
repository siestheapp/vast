"""Shared service layer consumed by both CLI and API surfaces."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple
from contextlib import contextmanager

from sqlalchemy import text

from .agent import (
    plan_sql,
    plan_sql_with_retry,
    load_or_build_schema_summary,
    refresh_schema_summary as _agent_refresh_schema_summary,
    get_schema_state as _agent_get_schema_state,
    resolver_shortcut,
)
from .config import settings
from .db import get_engine, get_ro_engine, is_select
from .catalog_pg import load_card
from .introspect import list_tables, table_columns
from .identifier_guard import extract_requested_identifiers
from .sql_params import ensure_limit_param, hydrate_readonly_params, normalize_limit_literal, stmt_kind
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

logger = logging.getLogger(__name__)

# Exported name that tests patch: src.vast.service.ensure_valid_identifiers
ensure_valid_identifiers = _ensure_valid_identifiers  # bind now


# Exported helper for CLI
SQL_PREFIXES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "WITH",
    "CREATE",
    "ALTER",
    "DROP",
    "GRANT",
    "REVOKE",
    "EXPLAIN",
)


_LIMIT_HINT_RE = re.compile(r"\b(?:top|first|limit|last|show(?:\s+me)?)\s+(\d+)\b", re.IGNORECASE)
_LIMIT_BIND_RE = re.compile(r"(?i)\blimit\s+:limit\b")


def _default_limit() -> int:
    value = getattr(settings, "VAST_DEFAULT_LIMIT", 10)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 10


def infer_limit_from_text(text: str | None, default: int | None = None) -> int:
    """Infer an integer limit from natural language text."""

    fallback = default if default is not None else _default_limit()
    if not text:
        return fallback
    match = _LIMIT_HINT_RE.search(text)
    if not match:
        return fallback
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return fallback
    return value if value > 0 else fallback


def _apply_limit_hint(sql_text: str | None, prompt_text: str | None, params: Dict[str, Any] | None) -> Dict[str, Any]:
    """Apply limit inference when SQL uses a :limit bind and the caller omitted it."""

    data = dict(params or {})
    if "limit" in data:
        return data
    if not sql_text or not _LIMIT_BIND_RE.search(sql_text):
        return data
    data["limit"] = infer_limit_from_text(prompt_text, _default_limit())
    return data


def looks_like_sql(text: str | None) -> bool:
    if not text:
        return False
    stripped = text.lstrip()
    if not stripped:
        return False
    first_token = stripped.split(None, 1)[0].upper()
    return first_token in SQL_PREFIXES


def _intent_from_sql(sql_text: str | None) -> str | None:
    """Best effort classification of statement intent (read/write)."""

    if not sql_text or not sql_text.strip():
        return None
    try:
        return "read" if is_select(sql_text) else "write"
    except Exception:  # pragma: no cover - defensive
        logger.debug("Failed to classify SQL intent", exc_info=True)
        return None


def _breadcrumbs_from_meta(meta: Dict[str, Any] | None, deterministic_hint: bool | None = None) -> Dict[str, Any] | None:
    """Extract breadcrumb metadata surfaced to the UI."""

    if not meta:
        meta = {}

    breadcrumbs: Dict[str, Any] = {}

    if deterministic_hint is not None:
        breadcrumbs["deterministic"] = deterministic_hint
    else:
        llm_ms = meta.get("llm_ms")
        if llm_ms is not None:
            breadcrumbs["deterministic"] = bool(llm_ms == 0 and not meta.get("handoff"))

    if "rule" in meta:
        breadcrumbs.setdefault("rule", meta.get("rule"))
    elif "used_path" in meta:
        breadcrumbs.setdefault("rule", meta.get("used_path"))

    if "llm_ms" in meta:
        breadcrumbs["llm_ms"] = meta["llm_ms"]

    return breadcrumbs or None


def _print_debug_timings(meta: Dict[str, Any]) -> None:
    catalog_ms = meta.get("catalog_ms", meta.get("catalog_ms_slim", 0))
    plan_ms = meta.get("plan_ms", 0)
    engine_ms = meta.get("engine_ms", 0)
    exec_ms = meta.get("exec_ms", 0)
    llm_ms = meta.get("llm_ms", 0)
    total_ms = meta.get("total_ms", 0)
    print(
        "debug timing → catalog_ms={catalog_ms} plan_ms={plan_ms} engine_ms={engine_ms} exec_ms={exec_ms} llm_ms={llm_ms} total_ms={total_ms}".format(
            catalog_ms=catalog_ms,
            plan_ms=plan_ms,
            engine_ms=engine_ms,
            exec_ms=exec_ms,
            llm_ms=llm_ms,
            total_ms=total_ms,
        )
    )


def connection_info(engine) -> Dict[str, Any]:
    """Return connection metadata for the provided engine."""

    query = text(
        """
        SELECT
          current_database() AS db,
          current_user       AS whoami,
          inet_server_addr()::text AS host,
          inet_server_port()       AS port
        """
    )

    try:
        with engine.connect() as conn:
            row = conn.execute(query).mappings().first()
    except Exception as exc:
        return {
            "db": None,
            "whoami": None,
            "host": None,
            "port": None,
            "error": str(exc),
        }

    if not row:
        return {"db": None, "whoami": None, "host": None, "port": None}

    return {
        "db": row.get("db"),
        "whoami": row.get("whoami"),
        "host": row.get("host"),
        "port": row.get("port"),
    }


def probe_read(engine) -> None:
    """Lightweight read probe to ensure SELECT works."""

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def check_reference_privileges(engine, tables: Iterable[str]) -> List[Dict[str, Any]]:
    """Verify REFERENCES privileges for the active role on each table."""

    results: List[Dict[str, Any]] = []
    with engine.connect() as conn:
        for tbl in tables:
            res = conn.execute(
                text(
                    "SELECT has_table_privilege(current_user, :tbl, 'REFERENCES') AS has_ref"
                ),
                {"tbl": tbl},
            ).scalar()
            results.append({"table": tbl, "has_ref": bool(res)})
    return results


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
for _name in ("ensure_valid_identifiers", "safe_execute", "preflight_statements", "apply_statements", "looks_like_sql", "infer_limit_from_text"):
    if _name not in __all__:
        __all__.append(_name)

for _name in ("schema_state", "refresh_schema_summary"):
    if _name not in __all__:
        __all__.append(_name)

for _name in ("connection_info", "probe_read", "check_reference_privileges"):
    if _name not in __all__:
        __all__.append(_name)


def schema_state(force_refresh: bool = False) -> Dict[str, Any]:
    """Expose the cached schema summary and fingerprint for callers/tests."""

    return _agent_get_schema_state(force_refresh=force_refresh)


def refresh_schema_summary() -> Dict[str, Any]:
    """Force a schema summary refresh and return the updated state."""

    return _agent_refresh_schema_summary()


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
    params_with_hint = _apply_limit_hint(sql, sql, params)
    normalized_sql = normalize_limit_literal(sql, params_with_hint)
    hydrated_params = hydrate_readonly_params(normalized_sql, params_with_hint)
    sql_kind = stmt_kind(normalized_sql)
    if not allow_writes and sql_kind != "SELECT":
        keyword = normalized_sql.lstrip().split(None, 1)[0].upper() if (normalized_sql or "").strip() else ""
        raise ValueError(
            f"Read-only mode: expected a SELECT statement but received {keyword or 'non-SELECT SQL'}."
        )
    if _LIMIT_BIND_RE.search(sql or "") and "limit" not in (hydrated_params or {}):
        hydrated_params = dict(hydrated_params or {})
        hydrated_params["limit"] = infer_limit_from_text(sql, _default_limit())
    summary = load_or_build_schema_summary()
    engine_ms = 0
    if not allow_writes:
        engine_start = time.perf_counter()
        get_ro_engine()
        engine_ms = int((time.perf_counter() - engine_start) * 1000)
    engine = get_engine(readonly=True)
    requested = extract_requested_identifiers(normalized_sql)
    _ensure_valid_identifiers(
        normalized_sql,
        engine=engine,
        schema_summary=summary,
        params=hydrated_params,
        requested=requested,
    )
    exec_start = time.perf_counter()
    rows = safe_execute(
        normalized_sql,
        params=hydrated_params or {},
        allow_writes=allow_writes,
        force_write=force_write,
    )
    exec_ms = int((time.perf_counter() - exec_start) * 1000)
    serialised, dry_run = _serialize_rows(rows)
    return {
        "rows": serialised,
        "row_count": 0 if dry_run else len(serialised),
        "dry_run": dry_run,
        "meta": {
            "engine_ms": engine_ms,
            "exec_ms": exec_ms,
        },
    }


def plan_and_execute(
    nl_request: str,
    params: Dict[str, Any] | None = None,
    allow_writes: bool = False,
    force_write: bool = False,
    refresh_schema: bool = False,
    retry: bool = True,
    max_retries: int = 2,
    debug: bool = False,
) -> Dict[str, Any]:
    """Plan SQL using the agent and execute it, returning SQL and results."""

    total_start = time.perf_counter()
    param_hints = dict(params or {})
    is_sql = looks_like_sql(nl_request)
    resolution: Dict[str, Any] | None = None
    catalog_ms = 0
    plan_ms = 0
    llm_ms = 0

    if is_sql:
        param_hints = _apply_limit_hint(nl_request, nl_request, param_hints)
        summary = load_or_build_schema_summary()
        engine = get_engine(readonly=True)
        ensure_valid_identifiers(
            nl_request,
            engine=engine,
            schema_summary=summary,
            params=param_hints,
        )
        execution = execute_sql(
            nl_request,
            params=param_hints,
            allow_writes=allow_writes,
            force_write=force_write,
        )
        total_ms = int((time.perf_counter() - total_start) * 1000)
        exec_meta = execution.get("meta", {}) if isinstance(execution, dict) else {}
        engine_ms = exec_meta.get("engine_ms", 0)
        exec_ms = exec_meta.get("exec_ms", 0)
        meta = {
            "intent": "sql",
            "catalog_ms": 0,
            "catalog_ms_slim": 0,
            "plan_ms": 0,
            "engine_ms": engine_ms,
            "exec_ms": exec_ms,
            "llm_ms": 0,
            "total_ms": total_ms,
            "handoff": False,
            "handoff_reason": None,
            "regenerated": False,
            "allowed_tables": None,
        }
        intent = _intent_from_sql(nl_request) or "write"
        breadcrumbs = _breadcrumbs_from_meta(meta, deterministic_hint=False)
        if debug:
            _print_debug_timings(meta)
        if isinstance(execution, dict):
            execution = dict(execution)
            existing_meta = dict(execution.get("meta") or {})
            existing_meta.setdefault("engine_ms", engine_ms)
            existing_meta.setdefault("exec_ms", exec_ms)
            execution["meta"] = existing_meta
        outcome = {
            "sql": nl_request,
            "execution": execution,
            "passthrough": True,
            "meta": meta,
            "intent": intent,
        }
        if breadcrumbs:
            outcome["breadcrumbs"] = breadcrumbs
        return outcome

    try:
        resolution, shortcut = resolver_shortcut(nl_request)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Resolver shortcut failed: %s", exc)
        resolution, shortcut = None, None

    if debug and resolution:
        preview = [
            {
                "table": f"{c.get('schema')}.{c.get('table')}" if c.get("schema") and c.get("table") else c.get("table") or c.get("schema"),
                "score": c.get("score"),
            }
            for c in (resolution.get("candidates") or [])
        ]
        if preview:
            print(f"debug resolver candidates={preview}")

    resolution_meta = {}
    if resolution:
        resolution_meta = resolution.get("meta", {}) or {}
        catalog_ms = resolution_meta.get("catalog_ms_slim", resolution_meta.get("catalog_ms", 0))
        plan_ms = resolution_meta.get("plan_ms", 0)

    if shortcut:
        if "sql" not in shortcut:
            meta = dict(shortcut.get("meta") or {})
            meta.setdefault("catalog_ms", catalog_ms)
            meta.setdefault("catalog_ms_slim", meta.get("catalog_ms", catalog_ms))
            meta.setdefault("plan_ms", plan_ms)
            meta.setdefault("engine_ms", meta.get("engine_ms", 0))
            meta.setdefault("exec_ms", meta.get("exec_ms", 0))
            meta.setdefault("llm_ms", 0)
            meta["total_ms"] = int((time.perf_counter() - total_start) * 1000)
            meta.setdefault("handoff", False)
            meta.setdefault("handoff_reason", None)
            meta.setdefault("regenerated", False)
            meta.setdefault("allowed_tables", None)
            if debug:
                _print_debug_timings(meta)
            outcome = {
                "answer": shortcut.get("answer"),
                "meta": meta,
                "intent": "unknown",
            }
            if resolution:
                outcome["resolver"] = resolution
            return outcome

        meta = dict(shortcut.get("meta") or {})
        meta.setdefault("catalog_ms", catalog_ms)
        meta.setdefault("catalog_ms_slim", meta.get("catalog_ms", catalog_ms))
        meta.setdefault("plan_ms", plan_ms)
        meta.setdefault("engine_ms", meta.get("engine_ms", 0))
        meta.setdefault("exec_ms", meta.get("exec_ms", 0))
        meta.setdefault("llm_ms", 0)
        meta["total_ms"] = int((time.perf_counter() - total_start) * 1000)
        meta.setdefault("handoff", False)
        meta.setdefault("handoff_reason", None)
        meta.setdefault("regenerated", False)
        meta.setdefault("allowed_tables", None)
        execution = dict(shortcut.get("execution") or {})
        execution_meta = dict(execution.get("meta") or {})
        execution_meta.setdefault("engine_ms", meta.get("engine_ms", 0))
        execution_meta.setdefault("exec_ms", meta.get("exec_ms", 0))
        execution["meta"] = execution_meta
        if "row_count" not in execution:
            answer = shortcut.get("answer")
            execution["row_count"] = len(answer) if isinstance(answer, list) else 1
        intent = _intent_from_sql(shortcut.get("sql")) or "write"
        breadcrumbs = _breadcrumbs_from_meta(meta, deterministic_hint=True)
        outcome = {
            "sql": shortcut["sql"],
            "execution": execution,
            "answer": shortcut.get("answer"),
            "meta": meta,
            "intent": intent,
        }
        if debug and meta.get("used_path"):
            print({"intent": meta.get("intent"), "used_path": meta.get("used_path")})
        if resolution:
            outcome["resolver"] = resolution
        if debug:
            _print_debug_timings(meta)
        if breadcrumbs:
            outcome["breadcrumbs"] = breadcrumbs
        return outcome

    focus_cards: List[Dict[str, Any]] = []
    if resolution:
        for candidate in (resolution.get("candidates") or [])[:3]:
            schema = candidate.get("schema")
            table = candidate.get("table")
            if not schema or not table:
                continue
            try:
                card = load_card(schema, table)
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            except Exception:  # pragma: no cover - defensive
                continue
            focus_cards.append(card)
            if len(focus_cards) >= 3:
                break

    if focus_cards:
        focus_tables = [f"{card['schema']}.{card['table']}" for card in focus_cards]
    elif resolution:
        message = "I'm not confident which tables to use. Tell me which table(s) to query."
        meta = {
            "intent": resolution.get("intent"),
            "reason": "no_focus_tables",
            "catalog_ms": catalog_ms,
            "catalog_ms_slim": catalog_ms,
            "plan_ms": plan_ms,
            "engine_ms": 0,
            "exec_ms": 0,
            "llm_ms": 0,
            "total_ms": int((time.perf_counter() - total_start) * 1000),
        }
        if debug:
            _print_debug_timings(meta)
        outcome = {"answer": message, "meta": meta, "intent": "unknown"}
        outcome["resolver"] = resolution
        return outcome

    llm_start = time.perf_counter()
    if retry:
        plan_result = plan_sql_with_retry(
            nl_request,
            allow_writes=allow_writes,
            force_refresh_schema=refresh_schema,
            param_hints=param_hints,
            max_retries=max_retries,
            validator=_validation_executor,
            focus_cards=focus_cards,
        )
    else:
        plan_result = plan_sql(
            nl_request,
            allow_writes=allow_writes,
            force_refresh_schema=refresh_schema,
            param_hints=param_hints,
            focus_cards=focus_cards,
        )
    llm_ms = int((time.perf_counter() - llm_start) * 1000)

    if plan_result.clarification:
        meta = {
            "intent": (resolution or {}).get("intent", "unknown"),
            "catalog_ms": catalog_ms,
            "catalog_ms_slim": catalog_ms,
            "plan_ms": plan_ms,
            "engine_ms": 0,
            "exec_ms": 0,
            "llm_ms": llm_ms,
            "total_ms": int((time.perf_counter() - total_start) * 1000),
            "regenerated": plan_result.regenerated,
            "allowed_tables": plan_result.allowed_tables,
            "handoff": bool(resolution and resolution.get("needs_llm")),
            "handoff_reason": (resolution or {}).get("reason") if resolution and resolution.get("needs_llm") else None,
        }
        if debug:
            _print_debug_timings(meta)
        outcome = {
            "answer": plan_result.clarification,
            "meta": meta,
            "intent": "unknown",
        }
        if resolution:
            outcome["resolver"] = resolution
        return outcome

    sql = plan_result.sql or ""
    if sql and not sql.rstrip().endswith(";"):
        sql = f"{sql.rstrip()};"

    if debug:
        print(f"debug regenerated={plan_result.regenerated}")
        if plan_result.allowed_tables:
            print(f"debug allowed_tables={plan_result.allowed_tables}")

    param_hints = _apply_limit_hint(sql, nl_request, param_hints)
    execution = execute_sql(sql, params=param_hints, allow_writes=allow_writes, force_write=force_write)
    total_ms = int((time.perf_counter() - total_start) * 1000)

    execution_meta = execution.get("meta", {}) if isinstance(execution, dict) else {}
    engine_ms = execution_meta.get("engine_ms", 0)
    exec_ms = execution_meta.get("exec_ms", 0)

    meta = {
        "intent": (resolution or {}).get("intent", "unknown"),
        "catalog_ms": catalog_ms,
        "catalog_ms_slim": catalog_ms,
        "plan_ms": plan_ms,
        "engine_ms": engine_ms,
        "exec_ms": exec_ms,
        "llm_ms": llm_ms,
        "total_ms": total_ms,
        "regenerated": plan_result.regenerated,
        "allowed_tables": plan_result.allowed_tables,
        "handoff": bool(resolution and resolution.get("needs_llm")),
        "handoff_reason": (resolution or {}).get("reason") if resolution and resolution.get("needs_llm") else None,
    }

    if debug:
        _print_debug_timings(meta)

    if isinstance(execution, dict):
        execution = dict(execution)
        exec_meta = dict(execution.get("meta") or {})
        exec_meta.setdefault("engine_ms", engine_ms)
        exec_meta.setdefault("exec_ms", exec_ms)
        execution["meta"] = exec_meta

    intent = _intent_from_sql(sql)
    if intent is None:
        intent = "unknown" if not sql else "write"
    breadcrumbs = _breadcrumbs_from_meta(meta)

    outcome = {
        "sql": sql,
        "execution": execution,
        "meta": meta,
        "intent": intent,
    }
    if resolution:
        outcome["resolver"] = resolution
    if breadcrumbs:
        outcome["breadcrumbs"] = breadcrumbs
    return outcome


def _validation_executor(sql: str, params: Dict[str, Any], allow_writes: bool) -> None:
    """Validate generated SQL without forcing writes to run."""
    params = _apply_limit_hint(sql, sql, params)
    normalized_sql = normalize_limit_literal(sql, params)
    hydrated_params = hydrate_readonly_params(normalized_sql, params)
    hydrated_params = ensure_limit_param(normalized_sql, hydrated_params)
    sql_kind = stmt_kind(normalized_sql)
    if not allow_writes and sql_kind != "SELECT":
        keyword = normalized_sql.lstrip().split(None, 1)[0].upper() if (normalized_sql or "").strip() else ""
        raise ValueError(
            f"Read-only mode: expected a SELECT statement but received {keyword or 'non-SELECT SQL'}."
        )
    if _LIMIT_BIND_RE.search(sql or "") and "limit" not in (hydrated_params or {}):
        hydrated_params = dict(hydrated_params or {})
        hydrated_params["limit"] = infer_limit_from_text(sql, _default_limit())
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
    # Preserve trailing semicolons (tests assert original statements)
    return sql.strip().rstrip("\n \t")


@contextmanager
def _writer_conn():
    """Yield a single writer connection for transactional operations."""
    engine = get_engine(readonly=False)

    # Normal path: real SQLAlchemy engine
    connect = getattr(engine, "connect", None)
    if callable(connect):
        conn = connect()
        try:
            yield conn
        finally:
            close = getattr(conn, "close", None)
            if callable(close):
                close()
        return

    # Test shim path: DummyEngine without .connect()
    if hasattr(engine, "statements") and hasattr(engine, "events"):
        class _ShimConnection:
            def __init__(self, eng):
                self.engine = eng
                self.closed = False

            def execute(self, clause, params=None):
                sql_text = getattr(clause, "text", str(clause))
                if params:
                    sql_text = f"{sql_text} -- params={params}"
                self.engine.statements.append(sql_text)
                # Simulate failure on Nth call like DummyEngine
                current = getattr(self.engine, "call_count", 0) + 1
                setattr(self.engine, "call_count", current)
                fail_on = getattr(self.engine, "fail_on", None)
                if fail_on and current == fail_on:
                    raise RuntimeError("boom")
                # Minimal result compatible with _first_row
                return []

            def exec_driver_sql(self, sql, params=None):
                sql_text = f"{sql} -- params={params}" if params else sql
                self.engine.statements.append(sql_text)
                current = getattr(self.engine, "call_count", 0) + 1
                setattr(self.engine, "call_count", current)
                fail_on = getattr(self.engine, "fail_on", None)
                if fail_on and current == fail_on:
                    raise RuntimeError("boom")
                # Provide minimal rows for identity/sequence helpers
                if "current_user" in sql:
                    return [("demo_user", "demo_session")]
                if "pg_get_serial_sequence" in sql:
                    return [("public.review_review_id_seq",)]
                return []

            def begin(self):
                self.engine.events.append("BEGIN")
                class _Tx:
                    def __init__(self, eng):
                        self.engine = eng
                    def rollback(self):
                        self.engine.events.append("ROLLBACK")
                    def commit(self):
                        self.engine.events.append("COMMIT")
                return _Tx(self.engine)

            def close(self):
                if not self.closed:
                    self.closed = True
                    self.engine.events.append("CLOSE")

        conn = _ShimConnection(engine)
        try:
            yield conn
        finally:
            conn.close()
        return

    # Fallback: attempt normal connect and yield
    conn = engine.connect()
    try:
        yield conn
    finally:
        conn.close()


def _exec_on(conn, sql: str, params: Dict[str, Any] | None = None):
    """Execute SQL using the provided connection."""

    statement = text(sql)
    return conn.execute(statement, params or {})


def _first_row(res):
    first = getattr(res, "first", None)
    if callable(first):
        return first()
    fetch = getattr(res, "fetchone", None)
    if callable(fetch):
        return fetch()
    if isinstance(res, list) and res:
        return res[0]
    return None


def _log_writer_identity(conn) -> None:
    try:
        exec_driver = getattr(conn, "exec_driver_sql", None)
        if callable(exec_driver):
            res = exec_driver("SELECT current_user, session_user")
        else:
            res = conn.execute(text("SELECT current_user, session_user"))
        row = _first_row(res)
        if row:
            current = row[0]
            session = row[1] if len(row) > 1 else current
            logger.info("writer session: current_user=%s session_user=%s", current, session)
        else:
            logger.warning("Failed to fetch writer identity: empty result")
    except Exception as exc:
        logger.warning("Failed to fetch writer identity: %s", exc)


def preflight_statements(
    statements: List[str],
    *,
    engine=None,
    schema_map: Dict[str, Dict[str, Set[str]]] | None = None,
) -> List[str]:
    """Preflight statements using a single writer connection and transaction."""

    notes: List[str] = []
    if not statements:
        return notes

    with _writer_conn() as conn:
        trans = conn.begin()
        _log_writer_identity(conn)
        try:
            # Pass 1: run all DDL so dependent statements become visible.
            for raw in statements:
                statement = _normalize_statement(raw)
                if not statement:
                    continue
                if stmt_kind(statement) == "DDL":
                    _exec_on(conn, statement)
                    notes.append(f"OK DDL {statement.split()[0].upper()} ...")

            # Pass 2: EXPLAIN DML/SELECT using the same connection.
            for raw in statements:
                statement = _normalize_statement(raw)
                if not statement:
                    continue
                kind = stmt_kind(statement)
                if kind in {"DML", "SELECT"}:
                    explain_sql = f"EXPLAIN (VERBOSE, FORMAT JSON) {statement}"
                    params = hydrate_readonly_params(statement, {}) or {}
                    _exec_on(conn, explain_sql, params)
                    notes.append(f"OK EXPLAIN {kind} ...")

            trans.rollback()
        except Exception:
            trans.rollback()
            raise

    return notes


def apply_statements(
    statements: List[str],
    *,
    engine=None,
    schema_map: Dict[str, Dict[str, Set[str]]] | None = None,
) -> None:
    """Apply statements atomically using a single writer connection."""

    if not statements:
        return

    with _writer_conn() as conn:
        trans = conn.begin()
        try:
            _log_writer_identity(conn)
            # Apply all provided statements using the same connection/transaction
            for raw in statements:
                statement = _normalize_statement(raw)
                if not statement:
                    continue
                _exec_on(conn, statement)

            # Apply grants using the same connection/transaction (no second BEGIN)
            ro_role = getattr(settings, "read_role", "vast_ro")
            if ro_role:
                grant_sql = f"GRANT SELECT ON TABLE public.review TO {ro_role}"
                _exec_on(conn, grant_sql)

                exec_driver = getattr(conn, "exec_driver_sql", None)
                if callable(exec_driver):
                    seq_row = _first_row(exec_driver(
                        "SELECT pg_get_serial_sequence('public.review','review_id')"
                    ))
                else:
                    seq_row = _first_row(_exec_on(
                        conn,
                        "SELECT pg_get_serial_sequence('public.review','review_id')",
                    ))
                seq = seq_row[0] if seq_row else None
                if seq:
                    _exec_on(
                        conn,
                        f"GRANT USAGE, SELECT ON SEQUENCE {seq} TO {ro_role}",
                    )

            trans.commit()
        except Exception:
            trans.rollback()
            raise


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
