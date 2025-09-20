from __future__ import annotations
import json
from pathlib import Path
from typing import List, Dict, Any

from openai import OpenAI
from rich.console import Console

from .config import settings
from .introspect import list_tables, table_columns, schema_fingerprint
from .db import safe_execute

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
    CACHE_PATH.write_text(json.dumps({"fingerprint": fp, "summary": summary}, indent=2))
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

def plan_sql(
    nl_request: str,
    allow_writes: bool = False,
    force_refresh_schema: bool = False,
    param_hints: Dict[str, Any] | None = None,
) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY missing. Set it in .env")

    system_prompt = BASE_RULES
    if not allow_writes:
        system_prompt += "\n- For this task, generate ONLY SELECT queries."

    client = OpenAI(api_key=settings.openai_api_key)
    schema_ctx = load_or_build_schema_summary(force_refresh_schema)

    user_blocks = [
        f"Database schema:\n{schema_ctx}",
        f"Task: {nl_request}",
    ]
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
            model="gpt-4o-mini",
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

    console.print(f"[green]Generated SQL:[/]\n{sql}")
    return sql


def plan_sql_with_retry(
    nl_request: str,
    allow_writes: bool = False,
    force_refresh_schema: bool = False,
    param_hints: Dict[str, Any] | None = None,
    max_retries: int = 2,
) -> str:
    """
    Plan SQL with automatic retry on execution errors.
    Tests each generated SQL with a dry run before returning.
    If the SQL fails, includes the error in context for the next attempt.
    """
    last_error = None
    original_request = nl_request
    
    for attempt in range(max_retries):
        try:
            # Add error context if this is a retry
            current_request = original_request
            if last_error and attempt > 0:
                current_request = (
                    f"{original_request}\n\n"
                    f"IMPORTANT: The previous SQL attempt failed with this error:\n"
                    f"{last_error}\n"
                    f"Please generate corrected SQL that fixes this issue."
                )
                console.print(f"[yellow]Retry {attempt + 1}/{max_retries} after error: {last_error}[/]")
            
            # Generate SQL
            sql = plan_sql(current_request, allow_writes, force_refresh_schema, param_hints)
            
            # Validate with dry run (test execution without actually running)
            # This catches SQL syntax errors, missing tables/columns, etc.
            try:
                # Always test as read-only first to validate SQL structure
                test_params = param_hints or {}
                if allow_writes:
                    # For writes, test with allow_writes=True but force_write=False (dry run)
                    result = safe_execute(sql, params=test_params, allow_writes=True, force_write=False)
                    # Check if it's a DRY RUN response (expected for writes)
                    if result and isinstance(result, list) and len(result) > 0:
                        if isinstance(result[0], dict) and "_notice" in result[0]:
                            # This is expected for write operations - it passed validation
                            console.print("[dim]SQL validated successfully (dry run for write operation)[/]")
                        else:
                            # For SELECT queries that return actual results
                            console.print("[dim]SQL validated successfully[/]")
                else:
                    # For read-only queries, execute normally
                    result = safe_execute(sql, params=test_params, allow_writes=False, force_write=False)
                    console.print("[dim]SQL validated successfully[/]")
                
                return sql
                
            except Exception as validation_error:
                # SQL execution failed - will retry with error context
                raise validation_error
            
        except Exception as e:
            last_error = str(e)
            if attempt == max_retries - 1:
                # Final attempt failed, raise the error
                console.print(f"[red]All {max_retries} attempts failed. Final error: {e}[/]")
                raise RuntimeError(f"Failed to generate valid SQL after {max_retries} attempts. Last error: {e}")
            # Continue to next retry
    
    # Should never reach here, but just in case
    raise RuntimeError(f"Unexpected error in retry logic after {max_retries} attempts")
