"""SQL identifier validation helpers using PostgreSQL EXPLAIN output."""

from __future__ import annotations

import json
import difflib
import re
from typing import Dict, Set, Tuple, Iterable

from sqlalchemy import text

from .db import get_engine
from .introspect import list_tables, table_columns, schema_fingerprint
from .sql_params import hydrate_readonly_params


class IdentifierValidationError(ValueError):
    """Raised when SQL references tables or columns that do not exist."""

    def __init__(self, details: Dict[str, any], message: str, hint: str | None = None):
        super().__init__(message)
        self.details = details
        self.hint = hint


_SCHEMA_CACHE: Dict[str, Dict[str, Set[str]]] | None = None
_SCHEMA_FINGERPRINT: str | None = None


def load_schema_cache(engine=None, force_refresh: bool = False) -> Tuple[Dict[str, Dict[str, Set[str]]], str]:
    """Load a cached map of schema -> table -> set(columns)."""

    global _SCHEMA_CACHE, _SCHEMA_FINGERPRINT

    if engine is None:
        engine = get_engine(readonly=True)

    current_fp = schema_fingerprint()

    if force_refresh or _SCHEMA_CACHE is None or _SCHEMA_FINGERPRINT != current_fp:
        schema_map: Dict[str, Dict[str, Set[str]]] = {}
        for tbl in list_tables():
            schema = tbl["table_schema"]
            table = tbl["table_name"]
            cols = {col["column_name"] for col in table_columns(schema, table)}
            schema_map.setdefault(schema, {})[table] = cols

        _SCHEMA_CACHE = schema_map
        _SCHEMA_FINGERPRINT = current_fp

    return _SCHEMA_CACHE, _SCHEMA_FINGERPRINT


def _strip_quotes(name: str | None) -> str | None:
    if name is None:
        return None
    value = name.strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        value = value[1:-1]
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        value = value[1:-1]
    return value


def _normalize_relation(token: str | None) -> str | None:
    if not token:
        return None
    raw = token.strip().strip(',;')
    if not raw:
        return None
    raw = raw.split()[0]
    raw = raw.replace('"', "").replace("'", "")
    if raw.startswith('.'):
        raw = raw[1:]
    parts = [part for part in raw.split('.') if part]
    if not parts:
        return None
    if len(parts) == 1:
        schema, table = "public", parts[0]
    else:
        schema, table = parts[-2], parts[-1]
    if not schema:
        schema = "public"
    return f"{schema}.{table}"


def _extract_relations_from_sql(sql: str) -> Set[str]:
    pattern = re.compile(r"(?i)\b(?:from|join)\s+([a-zA-Z0-9_\"'.]+)")
    relations: Set[str] = set()
    for token in pattern.findall(sql or ""):
        normalized = _normalize_relation(token)
        if normalized:
            relations.add(normalized)
    return relations


def _split_select_list(sql: str) -> list[str]:
    match = re.search(r"(?is)\bselect\b(.*?)\bfrom\b", sql or "")
    if not match:
        return []
    select_part = match.group(1)
    tokens: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in select_part:
        if ch == ',' and depth == 0:
            token = ''.join(current).strip()
            if token:
                tokens.append(token)
            current = []
            continue
        current.append(ch)
        if ch == '(':
            depth += 1
        elif ch == ')' and depth > 0:
            depth -= 1
    tail = ''.join(current).strip()
    if tail:
        tokens.append(tail)

    columns: list[str] = []
    for token in tokens:
        text = token.strip()
        if not text:
            continue
        parts = text.split()
        if len(parts) >= 3 and parts[-2].upper() == "AS":
            base = " ".join(parts[:-2])
        elif len(parts) >= 2 and parts[-1].isidentifier():
            base = parts[0]
        else:
            base = text

        if '.' in base:
            base = base.split('.')[-1]
        base = _strip_quotes(base) or ""
        base = base.strip()

        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", base):
            columns.append(base)

    return columns


def _should_analyse(sql: str) -> bool:
    match = re.match(r"\s*([A-Za-z]+)", sql or "")
    if not match:
        return False
    return match.group(1).upper() in {"SELECT", "INSERT", "UPDATE"}


def extract_identifiers(sql: str, engine=None, params: Dict[str, object] | None = None) -> Dict[str, any]:
    """Extract relation and column identifiers from EXPLAIN (VERBOSE) output."""

    if not _should_analyse(sql):
        return {
            "relations": set(),
            "columns": {},
            "explain_failed": False,
            "error_text": "",
        }

    engine = engine or get_engine(readonly=True)
    explain_failed = False
    error_text = ""
    prepared_params = hydrate_readonly_params(sql, params)

    try:
        with engine.begin() as conn:
            stmt = text(f"EXPLAIN (VERBOSE, FORMAT JSON) {sql}")
            if prepared_params:
                result = conn.execute(stmt, prepared_params)
            else:
                result = conn.execute(stmt)
            payload = result.scalar()
    except Exception as exc:
        explain_failed = True
        error_text = str(exc)
        relations = _extract_relations_from_sql(sql)
        select_cols = _split_select_list(sql)
        columns: Dict[str, Set[str]] = {}
        if relations and select_cols:
            primary = next(iter(sorted(relations)))
            columns[primary] = set(select_cols)
        return {
            "relations": relations,
            "columns": columns,
            "explain_failed": True,
            "error_text": error_text,
        }

    try:
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, list):
            plan = payload[0].get("Plan", {}) if payload else {}
        else:
            plan = payload.get("Plan", {})
    except Exception:
        plan = {}

    relations: Set[str] = set()
    columns: Dict[str, Set[str]] = {}

    def _normalise_column(expr: str | None) -> str | None:
        if not expr:
            return None
        name = str(expr)
        name = name.split(" -> ")[-1]
        name = name.split("::")[0]
        name = name.strip('"')
        if "(" in name or ")" in name:
            return None
        if "." in name:
            name = name.split(".")[-1]
        name = name.strip()
        if not name:
            return None
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            return None
        return name

    def _visit(node: Dict[str, any], current_relation: str | None = None) -> None:
        if not isinstance(node, dict):
            return

        relation = node.get("Relation Name")
        schema = node.get("Schema")
        normalized_rel = None

        if relation:
            token = f"{schema}.{relation}" if schema else relation
            normalized_rel = _normalize_relation(token)
            if normalized_rel:
                relations.add(normalized_rel)

        outputs = node.get("Output")
        target_rel = normalized_rel or current_relation
        normalized_target = _normalize_relation(target_rel) if target_rel else None
        if outputs and normalized_target:
            cols = columns.setdefault(normalized_target, set())
            for entry in outputs:
                col = _normalise_column(entry)
                if col:
                    cols.add(col)

        next_relation = normalized_rel or current_relation
        for child in node.get("Plans", []) or []:
            _visit(child, next_relation)

    _visit(plan)

    return {
        "relations": relations,
        "columns": columns,
        "explain_failed": explain_failed,
        "error_text": error_text,
    }


def extract_requested_identifiers(sql: str) -> Dict[str, Set[str]]:
    relations = _extract_relations_from_sql(sql)
    columns_map: Dict[str, Set[str]] = {}
    select_cols = _split_select_list(sql)
    if select_cols and len(relations) == 1:
        relation = next(iter(relations))
        columns_map[relation] = set(select_cols)
    return {
        "relations": relations,
        "columns": columns_map,
    }


def validate_identifiers(
    sql: str,
    engine=None,
    schema_cache: Dict[str, Dict[str, Set[str]]] | None = None,
    params: Dict[str, object] | None = None,
    requested: Dict[str, Set[str]] | None = None,
) -> Tuple[bool, Dict[str, any]]:
    """Validate identifiers against schema cache."""

    engine = engine or get_engine(readonly=True)
    if schema_cache is None:
        schema_cache, _ = load_schema_cache(engine)

    identifiers = extract_identifiers(sql, engine, params=params)
    relations = identifiers.get("relations", set())
    columns_map = identifiers.get("columns", {})
    explain_failed = identifiers.get("explain_failed", False)
    error_text = identifiers.get("error_text", "")
    if requested is None:
        requested = extract_requested_identifiers(sql)
    requested_relations = set(requested.get("relations", set()))
    requested_columns = {
        key: set(value) for key, value in (requested.get("columns", {}) or {}).items()
    }
    unknown_relations: Set[str] = set()
    unknown_columns: Dict[str, Set[str]] = {}

    for relation in relations:
        if "." in relation:
            schema, table = relation.split(".", 1)
        else:
            schema, table = "public", relation

        table_map = schema_cache.get(schema, {})
        if table not in table_map:
            unknown_relations.add(f"{schema}.{table}")
            continue

        cols = columns_map.get(relation, set())
        missing = {col for col in cols if col not in table_map.get(table, set())}
        if missing:
            unknown_columns[f"{schema}.{table}"] = missing

    if not columns_map:
        select_cols = _split_select_list(sql)
        if select_cols and len(relations) == 1:
            relation = next(iter(relations))
            schema, table = relation.split(".", 1)
            table_map = schema_cache.get(schema, {})
            allowed = table_map.get(table, set())
            missing = {col for col in select_cols if col not in allowed}
            if missing:
                unknown_columns[relation] = missing

    strict_violation = False
    if requested_relations:
        if any(rel in requested_relations for rel in unknown_relations):
            strict_violation = True
    if requested_columns:
        for rel, cols in unknown_columns.items():
            requested_cols = requested_columns.get(rel)
            if requested_cols and any(col in requested_cols for col in cols):
                strict_violation = True

    details = {
        "unknown_relations": sorted(unknown_relations),
        "unknown_columns": {rel: sorted(cols) for rel, cols in sorted(unknown_columns.items())},
        "explain_failed": explain_failed,
        "error_text": error_text,
        "strict_violation": strict_violation,
    }

    ok = not details["unknown_relations"] and not details["unknown_columns"] and not explain_failed
    return ok, details


def format_identifier_error(details: Dict[str, any]) -> str:
    parts: list[str] = []
    if details.get("unknown_relations"):
        parts.append("Unknown tables: " + ", ".join(details["unknown_relations"]))
    if details.get("unknown_columns"):
        column_bits = []
        for rel, cols in details["unknown_columns"].items():
            column_bits.append(f"{rel}: {', '.join(cols)}")
        parts.append("Unknown columns → " + "; ".join(column_bits))
    if not parts:
        error_text = details.get("error_text")
        if error_text:
            return error_text
        return "SQL references unknown identifiers"
    return "; ".join(parts)


def _flatten_tables(schema_cache: Dict[str, Dict[str, Set[str]]]) -> Iterable[str]:
    for schema, tables in schema_cache.items():
        for table in tables.keys():
            yield f"{schema}.{table}"


def build_identifier_hint(details: Dict[str, any], schema_cache: Dict[str, Dict[str, Set[str]]], schema_summary: str) -> str:
    """Construct a hint listing allowed identifiers and close matches."""

    allowed_relations = sorted(_flatten_tables(schema_cache))
    lines = [
        "Use only tables and columns that exist in this database.",
        "Schema overview:",
        schema_summary.strip(),
    ]

    if not details.get("unknown_relations") and not details.get("unknown_columns"):
        error_text = details.get("error_text")
        if error_text:
            lines.append(f"- Planner error: {error_text}")

    for relation in details.get("unknown_relations", []):
        suggestions = difflib.get_close_matches(relation, allowed_relations, n=3)
        suggestion_text = ", ".join(suggestions) if suggestions else "no close matches"
        lines.append(f"- Unknown table `{relation}` (did you mean: {suggestion_text})")

    for relation, cols in details.get("unknown_columns", {}).items():
        schema, table = relation.split(".", 1)
        allowed_cols = sorted(schema_cache.get(schema, {}).get(table, set()))
        for col in cols:
            suggestions = difflib.get_close_matches(col, allowed_cols, n=3)
            suggestion_text = ", ".join(suggestions) if suggestions else "no close matches"
            lines.append(
                f"- Unknown column `{col}` on `{relation}` (existing: {', '.join(allowed_cols[:10])}; suggestions: {suggestion_text})"
            )

    return "\n".join(lines)


def ensure_valid_identifiers(
    sql: str,
    *,
    engine=None,
    schema_map: Dict[str, Dict[str, Set[str]]] | None = None,
    schema_summary: str | None = None,
    params: Dict[str, object] | None = None,
    requested: Dict[str, Set[str]] | None = None,
) -> Dict[str, Dict[str, Set[str]]]:
    """Validate SQL identifiers and raise IdentifierValidationError on failure."""

    engine = engine or get_engine(readonly=True)
    if schema_map is not None:
        schema_cache = schema_map
    else:
        schema_cache, _ = load_schema_cache(engine)
    if requested is None:
        requested = extract_requested_identifiers(sql)
    ok, details = validate_identifiers(
        sql,
        engine,
        schema_cache,
        params=params,
        requested=requested,
    )

    if not ok and schema_map is None:
        schema_cache, _ = load_schema_cache(engine, force_refresh=True)
        ok, details = validate_identifiers(
            sql,
            engine,
            schema_cache,
            params=params,
            requested=requested,
        )

    if not ok:
        if schema_summary is None:
            from .agent import load_or_build_schema_summary  # lazy import to avoid cycle

            schema_summary = load_or_build_schema_summary()

        hint = build_identifier_hint(details, schema_cache, schema_summary)
        message = format_identifier_error(details)
        raise IdentifierValidationError(details, message, hint)

    return schema_cache
