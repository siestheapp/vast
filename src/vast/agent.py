from __future__ import annotations
import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Callable, Tuple

from openai import OpenAI
from rich.console import Console

from .config import settings
from .introspect import list_tables, table_columns
from .db import safe_execute, get_engine
from .knowledge import get_knowledge_store
from .catalog_pg import get_cached_cards
from .resolver import resolve_entities, run_template_count, run_template_list
from .identifier_guard import (
    ensure_valid_identifiers,
    IdentifierValidationError,
    extract_requested_identifiers,
)
from .sql_params import hydrate_readonly_params, normalize_limit_literal, stmt_kind
from .settings import STRICT_IDENTIFIER_MODE

console = Console()
logger = logging.getLogger(__name__)
CACHE_PATH = Path(".vast/schema_cache.json")
_SCHEMA_STATE: Dict[str, Any] = {
    "schema_summary": None,
    "schema_fingerprint": None,
}

BASE_RULES = """You are Vast1, an AI database operator.

Rules:
- Generate ONLY safe SQL. Allowed: SELECT (default). If writes are allowed, INSERT/UPDATE with WHERE are okay. NEVER DELETE.
- Never use DDL (CREATE/ALTER/DROP/TRUNCATE/GRANT/REVOKE).
- Use only tables/columns that exist in the provided schema.
- DO NOT inline literal values; use named parameters like :name, :id, :limit whenever possible.
- Return ONLY the SQL — no prose, no backticks, no code fences, no comments.
- Output exactly one SQL statement (no multiple statements).
"""


def resolver_shortcut(
    nl_request: str,
    cards: Dict[str, Dict[str, Any]] | None = None,
) -> Tuple[Dict[str, Any], Dict[str, Any] | None]:
    """Run the deterministic resolver and return an optional shortcut result."""

    catalog_ms = 0
    if cards is not None:
        catalog = cards
    else:
        catalog_start = time.perf_counter()
        try:
            catalog = get_cached_cards()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to load schema cards for resolver: %s", exc)
            catalog = {}
        catalog_ms = int((time.perf_counter() - catalog_start) * 1000)

    plan_start = time.perf_counter()
    try:
        resolution = resolve_entities(nl_request or "", catalog or {})
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Resolver failed for request %r: %s", nl_request, exc)
        resolution = {"intent": "unknown", "candidates": []}
    plan_ms = int((time.perf_counter() - plan_start) * 1000)

    meta = dict(resolution.get("meta") or {})
    meta["catalog_ms"] = catalog_ms
    meta["plan_ms"] = plan_ms
    resolution["meta"] = meta

    candidates = resolution.get("candidates") or []

    def _has_clear_lead() -> bool:
        if not candidates:
            return False
        if len(candidates) == 1:
            return True
        try:
            return float(candidates[0]["score"]) - float(candidates[1]["score"]) >= 0.15
        except Exception:  # pragma: no cover - defensive
            return False

    intent = resolution.get("intent")

    if intent in {"count", "list"}:
        if not candidates:
            clarification = {
                "answer": "I don’t see a matching table for that. Tell me the table (e.g., public.brand) and I’ll run it.",
                "meta": {
                    "intent": intent,
                    "reason": "no_candidates",
                    "catalog_ms": catalog_ms,
                    "plan_ms": plan_ms,
                    "engine_ms": 0,
                    "exec_ms": 0,
                    "llm_ms": 0,
                },
            }
            return resolution, clarification

        if len(candidates) >= 2:
            try:
                margin = float(candidates[0]["score"]) - float(candidates[1]["score"])
            except Exception:  # pragma: no cover - defensive
                margin = 0.0
            if margin < 0.15:
                options = ", ".join(
                    f'{c["schema"]}.{c["table"]}' for c in candidates[:3]
                )
                clarification = {
                    "answer": f"Did you mean one of these tables: {options}? Reply with one and I’ll proceed.",
                    "meta": {
                        "intent": intent,
                        "reason": "ambiguous",
                        "catalog_ms": catalog_ms,
                        "plan_ms": plan_ms,
                        "engine_ms": 0,
                        "exec_ms": 0,
                        "llm_ms": 0,
                    },
                }
                return resolution, clarification

    if intent == "count" and _has_clear_lead():
        top = candidates[0]
        count_value, timing = run_template_count(top["schema"], top["table"])
        engine_ms = timing.get("engine_ms", 0)
        exec_ms = timing.get("exec_ms", 0)
        result = {
            "answer": f"{count_value}",
            "sql": f'SELECT COUNT(*) FROM "{top["schema"]}"."{top["table"]}";',
            "meta": {
                "intent": "count",
                "catalog_ms": catalog_ms,
                "plan_ms": plan_ms,
                "engine_ms": engine_ms,
                "exec_ms": exec_ms,
                "llm_ms": 0,
            },
            "execution": {
                "rows": [{"count": int(count_value)}],
                "success": True,
                "row_count": 1,
                "dry_run": False,
                "meta": {
                    "engine_ms": engine_ms,
                    "exec_ms": exec_ms,
                },
            },
        }
        return resolution, result

    if intent == "list" and _has_clear_lead():
        top = candidates[0]
        column_hints = resolution.get("column_hints") or []
        column_name = column_hints[0] if column_hints else None

        if not column_name:
            card = catalog.get(f'{top["schema"]}.{top["table"]}', {})
            for column in card.get("columns") or []:
                col_type = str(column.get("type") or "").lower()
                if col_type in {"text", "varchar", "character varying"} and column.get("name"):
                    column_name = column["name"]
                    break

        if column_name:
            rows, timing = run_template_list(top["schema"], top["table"], column_name, limit=50)
            engine_ms = timing.get("engine_ms", 0)
            exec_ms = timing.get("exec_ms", 0)
            result = {
                "answer": rows,
                "sql": f'SELECT "{column_name}" FROM "{top["schema"]}"."{top["table"]}" LIMIT 50;',
                "meta": {
                    "intent": "list",
                    "catalog_ms": catalog_ms,
                    "plan_ms": plan_ms,
                    "engine_ms": engine_ms,
                    "exec_ms": exec_ms,
                    "llm_ms": 0,
                },
                "execution": {
                    "rows": rows,
                    "success": True,
                    "row_count": len(rows),
                    "dry_run": False,
                    "meta": {
                        "engine_ms": engine_ms,
                        "exec_ms": exec_ms,
                    },
                },
            }
            return resolution, result

    return resolution, None

def schema_summary(max_tables: int = 18, max_cols_per_table: int = 12) -> str:
    # keep context leaner to avoid model choking
    tables = list_tables()[:max_tables]
    lines: List[str] = []
    for t in tables:
        cols = table_columns(t["table_schema"], t["table_name"])[:max_cols_per_table]
        colnames = ", ".join(c["column_name"] for c in cols)
        lines.append(f"{t['table_schema']}.{t['table_name']}({colnames})")
    return "\n".join(lines)

def _normalize_columns_for_fingerprint(cols: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for col in cols:
        normalized.append(
            {
                "name": col.get("column_name"),
                "type": col.get("data_type"),
                "nullable": col.get("is_nullable"),
                "default": col.get("column_default"),
            }
        )
    return sorted(normalized, key=lambda item: (item["name"] or ""))


def _compute_schema_fingerprint() -> str:
    snapshot: List[Dict[str, Any]] = []
    for tbl in list_tables():
        schema_name = tbl["table_schema"]
        table_name = tbl["table_name"]
        cols = table_columns(schema_name, table_name)
        snapshot.append(
            {
                "relation": f"{schema_name}.{table_name}",
                "columns": _normalize_columns_for_fingerprint(cols),
            }
        )
    snapshot.sort(key=lambda item: item["relation"])
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def refresh_schema_summary() -> Dict[str, Any]:
    summary = schema_summary()
    fingerprint = _compute_schema_fingerprint()

    new_state = {
        "schema_summary": summary,
        "schema_fingerprint": fingerprint,
    }

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache_payload: Dict[str, Any] = {}
    if CACHE_PATH.exists():
        try:
            cache_payload = json.loads(CACHE_PATH.read_text())
        except Exception:
            cache_payload = {}

    cache_payload.update(
        {
            "summary": summary,
            "fingerprint": fingerprint,
            "schema_fingerprint": fingerprint,
            "updated_at": datetime.utcnow().isoformat(),
        }
    )

    try:
        CACHE_PATH.write_text(json.dumps(cache_payload, indent=2))
    except Exception as exc:
        logger.warning("Failed to persist schema cache: %s", exc)

    _SCHEMA_STATE.update(new_state)
    return dict(_SCHEMA_STATE)


def _load_cached_schema_state() -> Dict[str, Any] | None:
    if CACHE_PATH.exists():
        try:
            data = json.loads(CACHE_PATH.read_text())
        except Exception:
            return None
        summary = data.get("summary")
        fingerprint = data.get("schema_fingerprint") or data.get("fingerprint")
        if summary and fingerprint:
            _SCHEMA_STATE.update({
                "schema_summary": summary,
                "schema_fingerprint": fingerprint,
            })
            return dict(_SCHEMA_STATE)
    return None


def _ensure_schema_state(force_refresh: bool = False) -> Dict[str, Any]:
    if force_refresh:
        return refresh_schema_summary()

    if _SCHEMA_STATE.get("schema_summary") and _SCHEMA_STATE.get("schema_fingerprint"):
        return dict(_SCHEMA_STATE)

    cached = _load_cached_schema_state()
    if cached:
        return cached

    return refresh_schema_summary()


def load_or_build_schema_summary(force_refresh: bool = False) -> str:
    state = _ensure_schema_state(force_refresh=force_refresh)
    return state["schema_summary"]


def get_schema_fingerprint(force_refresh: bool = False) -> str:
    state = _ensure_schema_state(force_refresh=force_refresh)
    return state["schema_fingerprint"]


def get_schema_state(force_refresh: bool = False) -> Dict[str, Any]:
    return dict(_ensure_schema_state(force_refresh=force_refresh))

def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.strip("`")
        parts = s.split("\n", 1)
        s = parts[1] if len(parts) > 1 else s
    return s.strip()

def _single_statement(sql: str) -> str:
    sql = sql.strip().rstrip(";")
    if ";" in sql:
        sql = sql.split(";", 1)[0]
    return sql.strip()


def _validate_with_guard(sql: str, params: Dict[str, Any], allow_writes: bool) -> str:
    # Normalize first so LIMIT :limit becomes LIMIT 1 when missing
    normalized_sql = normalize_limit_literal(sql, params)
    hydrated_params = hydrate_readonly_params(normalized_sql, params)
    # Ensure hydrated_params reflects the implicit limit when normalized
    if normalized_sql.upper().endswith("LIMIT 1") and "limit" not in hydrated_params:
        hydrated_params["limit"] = 1
    engine = get_engine(readonly=True)
    summary = load_or_build_schema_summary()
    requested = extract_requested_identifiers(normalized_sql)
    ensure_valid_identifiers(
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

def plan_sql(
    nl_request: str,
    allow_writes: bool = False,
    force_refresh_schema: bool = False,
    param_hints: Dict[str, Any] | None = None,
    extra_system_hint: str | None = None,
    schema_state: Dict[str, Any] | None = None,
) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY missing. Set it in .env")

    if force_refresh_schema or schema_state is None:
        schema_state = get_schema_state(force_refresh=force_refresh_schema)

    schema_context = schema_state["schema_summary"]
    schema_fp = schema_state["schema_fingerprint"]

    system_prompt = BASE_RULES
    if not allow_writes:
        system_prompt += (
            "\n- READ-ONLY MODE: Generate ONLY SELECT statements."
            " Never return INSERT, UPDATE, DELETE, MERGE, CALL, DO, or DDL."
        )
    if extra_system_hint:
        system_prompt += "\n" + extra_system_hint.strip()

    client = OpenAI(api_key=settings.openai_api_key)
    schema_ctx = f"schema_fingerprint={schema_fp}\n{schema_context}".strip()

    user_blocks = [
        f"Database schema:\n{schema_ctx}",
        f"Task: {nl_request}",
    ]

    # Knowledge retrieval
    knowledge_entries = []
    try:
        store = get_knowledge_store()
        store.capture_schema_snapshot()
        knowledge_entries = store.search(nl_request, top_k=3)
    except Exception as exc:
        console.print(f"[yellow]Knowledge retrieval failed: {exc}[/]")

    if knowledge_entries:
        knowledge_text = "\n\n".join(
            f"{entry.title}:\n{entry.content}" for entry in knowledge_entries
        )
        user_blocks.append("Authoritative context:\n" + knowledge_text)
    if param_hints:
        user_blocks.append(
            "Use these named parameters if relevant (do not inline values): "
            + ", ".join(f":{k}" for k in param_hints.keys())
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(user_blocks)},
    ]

    try:
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=messages,
            temperature=0,
            max_tokens=400,
        )
    except Exception as e:
        raise RuntimeError(f"LLM call failed: {e}") from e

    # Defensive handling of odd SDK returns
    choice = resp.choices[0] if resp.choices else None
    content = getattr(choice.message, "content", None) if choice else None
    if not content or (isinstance(content, str) and content.strip().lower() in ("", "none", "null")):
        raise RuntimeError("Model returned an empty SQL response. Try again or reduce prompt size (--refresh-schema).")

    if not isinstance(content, str):
        # sometimes SDKs can return non-string structured content; serialize defensively
        content = str(content)

    sql = _single_statement(_strip_fences(content))
    if not sql:
        raise RuntimeError("Empty SQL after normalization. Aborting.")

    if not allow_writes:
        kind = stmt_kind(sql)
        if kind != "SELECT":
            keyword = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
            raise RuntimeError(
                "Read-only mode requires a SELECT statement. "
                f"Received {keyword or 'non-SELECT SQL'}."
            )

    console.print(f"[green]Generated SQL:[/]\n{sql}")
    return sql


def plan_sql_with_retry(
    nl_request: str,
    allow_writes: bool = False,
    force_refresh_schema: bool = False,
    param_hints: Dict[str, Any] | None = None,
    max_retries: int = 2,
    validator: Callable[[str, Dict[str, Any], bool], Any] | None = None,
    extra_system_hint: str | None = None,
) -> str:
    """Plan SQL with automatic retry on identifier or execution errors."""

    original_request = nl_request
    last_error = None
    identifier_hint = extra_system_hint
    auto_refresh_used = False

    def _plan_once(request_text: str, schema_state: Dict[str, Any]) -> str:
        sql = plan_sql(
            request_text,
            allow_writes,
            force_refresh_schema=False,
            param_hints=param_hints,
            extra_system_hint=identifier_hint,
            schema_state=schema_state,
        )

        raw_params: Dict[str, Any] = dict(param_hints or {})
        normalized_sql = normalize_limit_literal(sql, raw_params)
        hydrated_params = hydrate_readonly_params(normalized_sql, raw_params)

        if validator is not None:
            result_sql = validator(normalized_sql, hydrated_params, allow_writes)
            if isinstance(result_sql, str):
                normalized_sql = normalize_limit_literal(result_sql, raw_params)
                hydrated_params = hydrate_readonly_params(normalized_sql, raw_params)
        else:
            normalized_sql = _validate_with_guard(normalized_sql, raw_params, allow_writes)

        return normalized_sql

    for attempt in range(max_retries):
        schema_state = get_schema_state(force_refresh_schema if attempt == 0 else False)

        current_request = original_request
        if last_error and attempt > 0:
            current_request = (
                f"{original_request}\n\n"
                f"IMPORTANT: The previous SQL attempt failed with this error:\n"
                f"{last_error}\n"
                f"Please generate corrected SQL that fixes this issue."
            )
            console.print(f"[yellow]Retry {attempt + 1}/{max_retries} after error: {last_error}[/]")

        try:
            return _plan_once(current_request, schema_state)
        except IdentifierValidationError as ide:
            if STRICT_IDENTIFIER_MODE and ide.details.get("strict_violation"):
                raise

            hint = ide.hint
            limit_hint = "Use a literal LIMIT 1 when returning a single row."
            combined_hint = hint or ""
            if limit_hint not in (combined_hint or ""):
                combined_hint = f"{combined_hint}\n{limit_hint}" if combined_hint else limit_hint
            if identifier_hint:
                if combined_hint and combined_hint not in identifier_hint:
                    identifier_hint = f"{identifier_hint}\n{combined_hint}" if identifier_hint else combined_hint
            else:
                identifier_hint = combined_hint or limit_hint

            if not auto_refresh_used:
                fingerprint_before = schema_state.get("schema_fingerprint")
                unknown_relations = ide.details.get("unknown_relations") or []
                unknown_columns = ide.details.get("unknown_columns") or {}
                try:
                    refreshed_state = refresh_schema_summary()
                except Exception as refresh_exc:
                    raise RuntimeError(
                        f"Failed to refresh schema summary: {refresh_exc}"
                    ) from refresh_exc

                fingerprint_after = refreshed_state.get("schema_fingerprint")
                logger.info(
                    "Schema refresh triggered after identifier validation failure: before=%s after=%s unknown_relations=%s unknown_columns=%s",
                    fingerprint_before,
                    fingerprint_after,
                    unknown_relations,
                    unknown_columns,
                )

                auto_refresh_used = True
                last_error = None

                try:
                    return _plan_once(original_request, refreshed_state)
                except IdentifierValidationError as refreshed_error:
                    refresh_hint = "Schema was refreshed; identifier remains unknown."
                    hint_text = refreshed_error.hint or ""
                    if refresh_hint not in hint_text:
                        hint_text = f"{hint_text}\n{refresh_hint}" if hint_text else refresh_hint
                    raise IdentifierValidationError(
                        refreshed_error.details,
                        str(refreshed_error),
                        hint_text,
                    ) from refreshed_error
                except Exception as exc_after_refresh:
                    last_error = str(exc_after_refresh)
                    continue

            refresh_hint = "Schema was refreshed; identifier remains unknown."
            hint_text = ide.hint or ""
            if refresh_hint not in hint_text:
                hint_text = f"{hint_text}\n{refresh_hint}" if hint_text else refresh_hint
            raise IdentifierValidationError(ide.details, str(ide), hint_text) from ide
        except Exception as exc:
            last_error = str(exc)
            if attempt == max_retries - 1:
                console.print(f"[red]All {max_retries} attempts failed. Final error: {exc}[/]")
                raise RuntimeError(
                    f"Failed to generate valid SQL after {max_retries} attempts. Last error: {exc}"
                ) from exc

    raise RuntimeError(f"Unexpected error in retry logic after {max_retries} attempts")
