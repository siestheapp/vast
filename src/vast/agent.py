from __future__ import annotations
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Set

from openai import OpenAI
from rich.console import Console
from sqlalchemy import text

from .config import settings
from .introspect import list_tables, table_columns
from .db import safe_execute, get_engine, get_ro_engine, analyse_sql, is_select, add_limit
from .knowledge import get_knowledge_store
from .catalog_pg import load_schema_index_slim, load_card
from .resolver import (
    PREFERRED_LIST_COLUMNS,
    resolve_entities,
    run_template_count,
    run_template_list,
)
from .identifier_guard import (
    ensure_valid_identifiers,
    IdentifierValidationError,
    extract_requested_identifiers,
)
from .sql_params import ensure_limit_param, hydrate_readonly_params, normalize_limit_literal, stmt_kind
from .settings import STRICT_IDENTIFIER_MODE

console = Console()
logger = logging.getLogger(__name__)
CACHE_PATH = Path(".vast/schema_cache.json")
_SCHEMA_STATE: Dict[str, Any] = {
    "schema_summary": None,
    "schema_fingerprint": None,
}

_TEXTUAL_TYPE_HINTS = {"char", "varchar", "text", "citext", "name", "uuid", "character varying"}
_LATEST_TEMPLATE_PATH = "product_url→style→brand"
_TEMPLATE_TIMEOUT_MS = 2000


@dataclass
class PlanResult:
    sql: Optional[str]
    regenerated: bool = False
    allowed_tables: Optional[List[str]] = None
    clarification: Optional[str] = None


def _column_is_textual(col_type: Any) -> bool:
    if col_type is None:
        return False
    if isinstance(col_type, str):
        lowered = col_type.lower()
        return any(token in lowered for token in _TEXTUAL_TYPE_HINTS)
    try:
        python_type = getattr(col_type, "python_type", None)
        if python_type is str:
            return True
    except Exception:  # pragma: no cover - defensive
        pass
    return False


def _select_list_column(columns: Sequence[Dict[str, Any]]) -> Optional[str]:
    name_map: Dict[str, Dict[str, Any]] = {}
    for col in columns:
        name = col.get("name")
        if not name:
            continue
        name_map[name.lower()] = col

    for preferred in PREFERRED_LIST_COLUMNS:
        col = name_map.get(preferred)
        if col and _column_is_textual(col.get("type")):
            return str(col.get("name"))

    for col in columns:
        if not col.get("name"):
            continue
        if _column_is_textual(col.get("type")):
            return str(col.get("name"))

    return None


def _find_column_name(card: Dict[str, Any], target: str) -> Optional[str]:
    target_lower = target.lower()
    for col in card.get("columns") or []:
        name = col.get("name")
        if name and name.lower() == target_lower:
            return name
    return None


def _run_latest_urls_per_brand(k: int, columns: Dict[str, str]) -> Tuple[List[Dict[str, Any]], Dict[str, int], str]:
    engine_start = time.perf_counter()
    engine = get_ro_engine()
    engine_ms = int((time.perf_counter() - engine_start) * 1000)

    sql = """
WITH ranked AS (
  SELECT
    pu."url"      AS url,
    b."name"      AS brand,
    pu."seen_at"  AS seen_at,
    ROW_NUMBER() OVER (
      PARTITION BY s."brand_id"
      ORDER BY pu."seen_at" DESC
    ) AS rn
  FROM "public"."product_url" pu
  JOIN "public"."style" s  ON pu."style_id" = s."id"
  JOIN "public"."brand" b  ON s."brand_id" = b."id"
  WHERE COALESCE(pu."is_current", TRUE) = TRUE
)
SELECT brand, url, seen_at
FROM ranked
WHERE rn <= :k
ORDER BY brand, seen_at DESC
LIMIT :cap
"""

    params = {"k": k, "cap": max(200, k * 20)}

    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = '{_TEMPLATE_TIMEOUT_MS}ms'"))
        exec_start = time.perf_counter()
        rows = conn.execute(text(sql), params).mappings().all()
        exec_ms = int((time.perf_counter() - exec_start) * 1000)

    return [dict(row) for row in rows], {"engine_ms": engine_ms, "exec_ms": exec_ms}, sql

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
    index_tables: Sequence[Dict[str, Any]] | None = None,
) -> Tuple[Dict[str, Any], Dict[str, Any] | None]:
    """Run the deterministic resolver and return an optional shortcut result."""

    catalog_ms = 0
    if index_tables is not None:
        tables = list(index_tables)
    else:
        catalog_start = time.perf_counter()
        try:
            slim_index = load_schema_index_slim()
            tables = slim_index.get("tables") or []
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to load slim schema index: %s", exc)
            tables = []
        catalog_ms = int((time.perf_counter() - catalog_start) * 1000)

    plan_start = time.perf_counter()
    try:
        resolution = resolve_entities(nl_request or "", tables)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Resolver failed for request %r: %s", nl_request, exc)
        resolution = {"intent": "unknown", "candidates": []}
    plan_ms = int((time.perf_counter() - plan_start) * 1000)

    meta = dict(resolution.get("meta") or {})
    meta["catalog_ms_slim"] = catalog_ms
    meta["catalog_ms"] = catalog_ms
    meta["plan_ms"] = plan_ms
    resolution["meta"] = meta

    candidates = resolution.get("candidates") or []

    if resolution.get("needs_llm"):
        meta.setdefault("reason", resolution.get("reason"))
        return resolution, None

    if resolution.get("intent") == "latest_per_group":
        allowed_tables = ["public.product_url", "public.style", "public.brand"]
        candidate = next((c for c in candidates if c.get("key") == "public.product_url"), None)
        k_value = int(resolution.get("k") or 5)
        item_noun = (resolution.get("item_noun") or "").lower()
        group_noun = (resolution.get("group_noun") or "").lower()

        def _noun_matches(noun: str, keywords: Sequence[str]) -> bool:
            noun_lower = noun.lower()
            return any(keyword in noun_lower for keyword in keywords)

        if not candidate or not _noun_matches(item_noun, ["product url", "product urls", "url", "urls"]) or not _noun_matches(group_noun, ["brand", "brands"]):
            resolution["needs_llm"] = True
            resolution["reason"] = "latest_per_group_unsupported"
            meta.setdefault("reason", resolution["reason"])
            return resolution, None

        try:
            card_product_url = load_card("public", "product_url")
            card_style = load_card("public", "style")
            card_brand = load_card("public", "brand")
        except FileNotFoundError:
            resolution["needs_llm"] = True
            resolution["reason"] = "latest_per_group_cards_missing"
            meta.setdefault("reason", resolution["reason"])
            return resolution, None
        except Exception:
            resolution["needs_llm"] = True
            resolution["reason"] = "latest_per_group_cards_missing"
            meta.setdefault("reason", resolution["reason"])
            return resolution, None

        col_url = _find_column_name(card_product_url, "url")
        col_seen_at = _find_column_name(card_product_url, "seen_at")
        col_style_fk = _find_column_name(card_product_url, "style_id")
        col_is_current = _find_column_name(card_product_url, "is_current") or "is_current"
        col_style_id = _find_column_name(card_style, "id")
        col_style_brand_fk = _find_column_name(card_style, "brand_id")
        col_brand_id = _find_column_name(card_brand, "id")
        col_brand_name = _find_column_name(card_brand, "name")

        required_cols = [col_url, col_seen_at, col_style_fk, col_style_id, col_style_brand_fk, col_brand_id, col_brand_name]
        if any(value is None for value in required_cols):
            resolution["needs_llm"] = True
            resolution["reason"] = "latest_per_group_columns_missing"
            meta.setdefault("reason", resolution["reason"])
            return resolution, None

        k_value = max(1, min(k_value, 20))
        rows, timing, sql_text = _run_latest_urls_per_brand(
            k_value,
            {
                "url": col_url,
                "seen_at": col_seen_at,
                "style_fk": col_style_fk,
                "is_current": col_is_current,
                "style_id": col_style_id,
                "style_brand_fk": col_style_brand_fk,
                "brand_id": col_brand_id,
                "brand_name": col_brand_name,
            },
        )

        meta.update(
            {
                "intent": "latest_per_group",
                "catalog_ms": catalog_ms,
                "catalog_ms_slim": catalog_ms,
                "plan_ms": plan_ms,
                "engine_ms": timing.get("engine_ms", 0),
                "exec_ms": timing.get("exec_ms", 0),
                "llm_ms": 0,
                "k": k_value,
                "used_path": _LATEST_TEMPLATE_PATH,
                "handoff": False,
                "handoff_reason": None,
                "regenerated": False,
                "allowed_tables": allowed_tables,
                "reason": "latest_per_group",
            }
        )

        execution = {
            "rows": rows,
            "row_count": len(rows),
            "dry_run": False,
            "success": True,
            "meta": timing,
        }

        result = {
            "answer": rows,
            "sql": sql_text,
            "meta": meta,
            "execution": execution,
        }
        return resolution, result

    def _has_clear_lead() -> bool:
        if not candidates:
            return False
        if len(candidates) == 1:
            return candidates[0].get("score", 0.0) >= 0.40
        try:
            top_score = float(candidates[0]["score"])
            next_score = float(candidates[1]["score"])
            return top_score >= 0.40 and (top_score - next_score) >= 0.15
        except Exception:  # pragma: no cover - defensive
            return False

    intent = resolution.get("intent")

    if intent in {"count", "list"}:
        if not candidates:
            clarification = {
                "answer": "I don't see a matching table for that. Tell me the table (e.g., public.brand) and I'll run it.",
                "meta": {
                    "intent": intent,
                    "reason": "no_candidates",
                    "catalog_ms": catalog_ms,
                    "catalog_ms_slim": catalog_ms,
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
            top_score = float(candidates[0].get("score", 0.0))
            if margin < 0.15 or top_score < 0.40:
                options = ", ".join(
                    f'{c["schema"]}.{c["table"]}' for c in candidates[:3]
                )
                clarification = {
                    "answer": f"Did you mean one of these tables: {options}? Reply with one and I'll proceed.",
                    "meta": {
                        "intent": intent,
                        "reason": "ambiguous",
                        "catalog_ms": catalog_ms,
                        "catalog_ms_slim": catalog_ms,
                        "plan_ms": plan_ms,
                        "engine_ms": 0,
                        "exec_ms": 0,
                        "llm_ms": 0,
                    },
                }
                return resolution, clarification
        elif candidates:
            top_score = float(candidates[0].get("score", 0.0))
            if top_score < 0.40:
                clarification = {
                    "answer": "I'm not confident which table matches that. Tell me the table name and I'll continue.",
                    "meta": {
                        "intent": intent,
                        "reason": "low_confidence",
                        "catalog_ms": catalog_ms,
                        "catalog_ms_slim": catalog_ms,
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
                "catalog_ms_slim": catalog_ms,
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
            column_name = _select_list_column(top.get("columns") or [])

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
                    "catalog_ms_slim": catalog_ms,
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
    hydrated_params = ensure_limit_param(normalized_sql, hydrated_params)
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
    focus_cards: Sequence[Dict[str, Any]] | None = None,
) -> PlanResult:
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

    prompt_schema: Optional[str] = None
    if focus_cards:
        def _brief(card: Dict[str, Any]) -> str:
            schema = card.get("schema") or ""
            table = card.get("table") or ""
            qualified = f"{schema}.{table}" if schema and table else table or schema
            columns = card.get("columns") or []
            column_bits = []
            for col in columns[:16]:
                name = col.get("name")
                if not name:
                    continue
                col_type = col.get("type")
                col_text = f"{name}:{str(col_type).lower()}" if col_type is not None else str(name)
                column_bits.append(col_text)
            fks = card.get("fks") or []
            fk_bits = []
            for fk in fks[:12]:
                column = fk.get("column")
                ref_table = fk.get("ref_table")
                ref_column = fk.get("ref_column")
                if column and ref_table and ref_column:
                    fk_bits.append(f"{column}->{ref_table}.{ref_column}")
            cols_text = ", ".join(column_bits)
            fk_text = f" | fks[{', '.join(fk_bits)}]" if fk_bits else ""
            return f"{qualified} | cols[{cols_text}]" + fk_text

        schema_brief = "\n".join(_brief(card) for card in focus_cards)
        prompt_schema = (
            "Use only these tables/columns. Do not invent identifiers.\n"
            "SCHEMA:\n" + schema_brief
        )
        user_blocks = [
            prompt_schema,
            f"Task: {nl_request}",
        ]
    else:
        schema_ctx = f"schema_fingerprint={schema_fp}\n{schema_context}".strip()
        prompt_schema = schema_ctx
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

    allowed_table_names: List[str] = []
    allowed_tables: Set[Tuple[Optional[str], str]] = set()
    allowed_columns: Set[Tuple[Optional[str], Optional[str], str]] = set()
    if focus_cards:
        for card in focus_cards:
            schema = card.get("schema")
            table = card.get("table")
            if not schema or not table:
                continue
            allowed_table_names.append(f"{schema}.{table}")
            allowed_tables.add((schema, table))
            allowed_tables.add((None, table))
            allowed_tables.add(("", table))
            for col in card.get("columns") or []:
                name = col.get("name")
                if not name:
                    continue
                allowed_columns.add((schema, table, name))
                allowed_columns.add((None, table, name))
                allowed_columns.add(("", table, name))
                allowed_columns.add((None, None, name))

    if allowed_table_names:
        ordered_unique: List[str] = []
        for name in allowed_table_names:
            if name not in ordered_unique:
                ordered_unique.append(name)
        allowed_table_meta = ordered_unique
    else:
        allowed_table_meta = None

    if allowed_table_meta and {"public.product_url", "public.style", "public.brand"}.issubset(set(allowed_table_meta)):
        user_blocks.append(
            "Join product_url→style→brand (pu.style_id = s.id, s.brand_id = b.id). Use pu.seen_at to rank latest and return the latest 5 per brand: brand, url, seen_at."
        )

    base_user_blocks = list(user_blocks)

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

    regenerated = False

    if focus_cards:
        def _table_allowed(entry: Tuple[Optional[str], str]) -> bool:
            schema, table = entry
            if (schema, table) in allowed_tables:
                return True
            if (None, table) in allowed_tables or ("", table) in allowed_tables:
                return True
            return False

        def _column_allowed(entry: Tuple[Optional[str], Optional[str], str]) -> bool:
            schema, table, name = entry
            if name == "*":
                return True
            if (schema, table, name) in allowed_columns:
                return True
            if table and ((None, table, name) in allowed_columns or ("", table, name) in allowed_columns):
                return True
            if (None, None, name) in allowed_columns:
                return True
            return False

        analysis = analyse_sql(sql)
        bad_tables = [entry for entry in analysis.tables if not _table_allowed(entry)]
        bad_columns = [entry for entry in analysis.columns if not _column_allowed(entry)]

        if bad_tables or bad_columns:
            allowed_names = allowed_table_meta or [
                f"{card.get('schema')}.{card.get('table')}"
                for card in focus_cards
                if card.get("schema") and card.get("table")
            ]
            strict_hint = (
                "Use only these tables: "
                + ", ".join(sorted(allowed_names))
                + ". Do not reference anything else."
            )

            strict_blocks = list(base_user_blocks)
            strict_blocks.insert(1, strict_hint)
            strict_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "\n\n".join(strict_blocks)},
            ]

            try:
                resp2 = client.chat.completions.create(
                    model=settings.openai_model,
                    messages=strict_messages,
                    temperature=0,
                    max_tokens=400,
                )
            except Exception as e:
                raise RuntimeError(f"LLM call failed: {e}") from e

            choice2 = resp2.choices[0] if resp2.choices else None
            content2 = getattr(choice2.message, "content", None) if choice2 else None
            if not content2 or (isinstance(content2, str) and content2.strip().lower() in ("", "none", "null")):
                raise RuntimeError("Model returned an empty SQL response after strict hint.")
            if not isinstance(content2, str):
                content2 = str(content2)
            sql2 = _single_statement(_strip_fences(content2))
            if not sql2:
                raise RuntimeError("Empty SQL after strict regeneration.")

            analysis2 = analyse_sql(sql2)
            bad_tables2 = [entry for entry in analysis2.tables if not _table_allowed(entry)]
            bad_columns2 = [entry for entry in analysis2.columns if not _column_allowed(entry)]
            if bad_tables2 or bad_columns2:
                message = "I can't answer without leaving the allowed tables. Specify which tables to use."
                return PlanResult(
                    sql=None,
                    regenerated=True,
                    allowed_tables=allowed_table_meta,
                    clarification=message,
                )

            sql = sql2
            analysis = analysis2
            regenerated = True

    if is_select(sql):
        sql = add_limit(sql, 100)
    sql = sql.rstrip()
    if is_select(sql) and not sql.endswith(";"):
        sql += ";"

    if not allow_writes:
        kind = stmt_kind(sql)
        if kind != "SELECT":
            keyword = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
            raise RuntimeError(
                "Read-only mode requires a SELECT statement. "
                f"Received {keyword or 'non-SELECT SQL'}."
            )

    console.print(f"[green]Generated SQL:[/]\n{sql}")
    return PlanResult(
        sql=sql,
        regenerated=regenerated,
        allowed_tables=allowed_table_meta,
    )


def plan_sql_with_retry(
    nl_request: str,
    allow_writes: bool = False,
    force_refresh_schema: bool = False,
    param_hints: Dict[str, Any] | None = None,
    max_retries: int = 2,
    validator: Callable[[str, Dict[str, Any], bool], Any] | None = None,
    extra_system_hint: str | None = None,
    focus_cards: Sequence[Dict[str, Any]] | None = None,
) -> PlanResult:
    """Plan SQL with automatic retry on identifier or execution errors."""

    original_request = nl_request
    last_error = None
    identifier_hint = extra_system_hint
    auto_refresh_used = False

    def _plan_once(request_text: str, schema_state: Dict[str, Any]) -> PlanResult:
        plan_result = plan_sql(
            request_text,
            allow_writes,
            force_refresh_schema=False,
            param_hints=param_hints,
            extra_system_hint=identifier_hint,
            schema_state=schema_state,
            focus_cards=focus_cards,
        )

        if plan_result.clarification:
            return plan_result

        sql_text = plan_result.sql or ""
        raw_params: Dict[str, Any] = dict(param_hints or {})
        normalized_sql = normalize_limit_literal(sql_text, raw_params)
        hydrated_params = hydrate_readonly_params(normalized_sql, raw_params)

        if validator is not None:
            result_sql = validator(normalized_sql, hydrated_params, allow_writes)
            if isinstance(result_sql, str):
                normalized_sql = normalize_limit_literal(result_sql, raw_params)
                hydrated_params = hydrate_readonly_params(normalized_sql, raw_params)
        else:
            normalized_sql = _validate_with_guard(normalized_sql, raw_params, allow_writes)

        if is_select(normalized_sql):
            normalized_sql = add_limit(normalized_sql, 100)
        normalized_sql = normalized_sql.rstrip()
        if is_select(normalized_sql) and not normalized_sql.endswith(";"):
            normalized_sql += ";"

        plan_result.sql = normalized_sql
        return plan_result

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
            result = _plan_once(current_request, schema_state)
            if result.clarification:
                return result
            return result
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
                    result = _plan_once(original_request, refreshed_state)
                    if result.clarification:
                        return result
                    return result
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
