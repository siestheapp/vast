from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Callable

from openai import OpenAI
from rich.console import Console

from .config import settings
from .introspect import list_tables, table_columns, schema_fingerprint
from .db import safe_execute, get_engine
from .knowledge import get_knowledge_store
from .identifier_guard import (
    ensure_valid_identifiers,
    IdentifierValidationError,
    extract_requested_identifiers,
)
from .sql_params import hydrate_readonly_params, normalize_limit_literal
from .settings import STRICT_IDENTIFIER_MODE

console = Console()
CACHE_PATH = Path(".vast/schema_cache.json")

BASE_RULES = """You are Vast1, an AI database operator.

Rules:
- Generate ONLY safe SQL. Allowed: SELECT (default). If writes are allowed, INSERT/UPDATE with WHERE are okay. NEVER DELETE.
- Never use DDL (CREATE/ALTER/DROP/TRUNCATE/GRANT/REVOKE).
- Use only tables/columns that exist in the provided schema.
- DO NOT inline literal values; use named parameters like :name, :id, :limit whenever possible.
- Return ONLY the SQL — no prose, no backticks, no code fences, no comments.
- Output exactly one SQL statement (no multiple statements).
"""

def schema_summary(max_tables: int = 18, max_cols_per_table: int = 12) -> str:
    # keep context leaner to avoid model choking
    tables = list_tables()[:max_tables]
    lines: List[str] = []
    for t in tables:
        cols = table_columns(t["table_schema"], t["table_name"])[:max_cols_per_table]
        colnames = ", ".join(c["column_name"] for c in cols)
        lines.append(f"{t['table_schema']}.{t['table_name']}({colnames})")
    return "\n".join(lines)

def load_or_build_schema_summary(force_refresh: bool = False) -> str:
    fp = schema_fingerprint()
    if not force_refresh and CACHE_PATH.exists():
        try:
            data = json.loads(CACHE_PATH.read_text())
            if data.get("fingerprint") == fp and "summary" in data:
                return data["summary"]
        except Exception:
            pass
    summary = schema_summary()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    cache_payload: Dict[str, Any] = {}
    if CACHE_PATH.exists():
        try:
            cache_payload = json.loads(CACHE_PATH.read_text())
        except Exception:
            cache_payload = {}

    cache_payload["fingerprint"] = fp
    cache_payload["summary"] = summary
    cache_payload["updated_at"] = datetime.utcnow().isoformat()

    CACHE_PATH.write_text(json.dumps(cache_payload, indent=2))
    return summary

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
    normalized_sql = normalize_limit_literal(sql, params)
    hydrated_params = hydrate_readonly_params(normalized_sql, params)
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
) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY missing. Set it in .env")

    system_prompt = BASE_RULES
    if not allow_writes:
        system_prompt += "\n- For this task, generate ONLY SELECT queries."
    if extra_system_hint:
        system_prompt += "\n" + extra_system_hint.strip()

    client = OpenAI(api_key=settings.openai_api_key)
    schema_ctx = load_or_build_schema_summary(force_refresh_schema)

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

    sql = normalize_limit_literal(sql, param_hints)
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

    for attempt in range(max_retries):
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
            sql = plan_sql(
                current_request,
                allow_writes,
                force_refresh_schema if attempt == 0 else False,
                param_hints,
                extra_system_hint=identifier_hint,
            )

            normalized_sql = normalize_limit_literal(sql, param_hints)
            hydrated_params = hydrate_readonly_params(normalized_sql, param_hints or {})

            if validator is not None:
                result_sql = validator(normalized_sql, hydrated_params, allow_writes)
                if isinstance(result_sql, str):
                    normalized_sql = normalize_limit_literal(result_sql, hydrated_params)
                    hydrated_params = hydrate_readonly_params(normalized_sql, hydrated_params)
            else:
                normalized_sql = _validate_with_guard(normalized_sql, hydrated_params, allow_writes)

            return normalized_sql

        except IdentifierValidationError as ide:
            last_error = str(ide)
            if STRICT_IDENTIFIER_MODE and ide.details.get("strict_violation"):
                raise
            hint = ide.hint
            limit_hint = "Use a literal LIMIT 1 when returning a single row."
            combined_hint = hint or ""
            if limit_hint not in combined_hint:
                combined_hint = f"{combined_hint}\n{limit_hint}" if combined_hint else limit_hint
            if identifier_hint:
                if combined_hint:
                    if combined_hint not in identifier_hint:
                        identifier_hint = f"{identifier_hint}\n{combined_hint}"
            else:
                identifier_hint = combined_hint or limit_hint
            continue
        except Exception as exc:
            last_error = str(exc)
            if attempt == max_retries - 1:
                console.print(f"[red]All {max_retries} attempts failed. Final error: {exc}[/]")
                raise RuntimeError(
                    f"Failed to generate valid SQL after {max_retries} attempts. Last error: {exc}"
                ) from exc

    raise RuntimeError(f"Unexpected error in retry logic after {max_retries} attempts")
