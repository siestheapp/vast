"""SQL identifier validation helpers using PostgreSQL EXPLAIN output."""

from __future__ import annotations

import json
import difflib
import re
from typing import Any, Dict, Iterable, Set, Tuple

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


_ALIAS_PATTERN = re.compile(
    r"(?is)\b(?:from|join)\s+([a-zA-Z0-9_.\"']+)(?:\s+(?:AS\s+)?([a-zA-Z_][a-zA-Z0-9_]*))?"
)


def _extract_aliases(sql: str) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for match in _ALIAS_PATTERN.finditer(sql or ""):
        base = _normalize_relation(match.group(1))
        alias = match.group(2)
        if alias and base:
            aliases[alias] = base
    return aliases


def _extract_ctes(sql: str) -> Dict[str, str]:
    ctes: Dict[str, str] = {}
    if not re.match(r"\s*WITH\b", sql or "", re.I):
        return ctes

    idx = re.search(r"\bWITH\b", sql, re.I)
    if not idx:
        return ctes
    pos = idx.end()
    text = sql
    length = len(text)

    while pos < length:
        while pos < length and text[pos].isspace():
            pos += 1
        name_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", text[pos:])
        if not name_match:
            break
        name = name_match.group(1)
        pos += len(name)
        while pos < length and text[pos].isspace():
            pos += 1
        as_match = re.match(r"AS\s*\(", text[pos:], re.I)
        if not as_match:
            break
        pos += as_match.end()
        depth = 1
        body_start = pos
        while pos < length and depth > 0:
            char = text[pos]
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            pos += 1
        body = text[body_start : pos - 1]
        ctes[name] = body
        while pos < length and text[pos].isspace():
            pos += 1
        if pos < length and text[pos] == ',':
            pos += 1
            continue
        break

    return ctes


def _extract_select_alias_columns(sql: str) -> Dict[str, Set[str]]:
    alias_columns: Dict[str, Set[str]] = {}
    match = re.search(r"(?is)\bselect\b(.*?)\bfrom\b", sql or "")
    if not match:
        return alias_columns
    select_part = match.group(1)
    pattern = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")
    for alias, column in pattern.findall(select_part):
        alias_columns.setdefault(alias, set()).add(column)
    return alias_columns


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
        alias_map = _extract_aliases(sql)
        alias_columns = _extract_select_alias_columns(sql)
        select_cols = _split_select_list(sql)
        columns: Dict[str, Set[str]] = {}
        for alias, base in alias_map.items():
            if alias_columns.get(alias):
                columns[alias] = set(alias_columns[alias])
                columns.setdefault(base, set()).update(alias_columns[alias])
                relations.add(base)
        if relations and select_cols and len(relations) == 1:
            primary = next(iter(sorted(relations)))
            columns.setdefault(primary, set()).update(select_cols)
        ctes = _extract_ctes(sql)
        cte_columns: Dict[str, Set[str]] = {}
        for name, body in ctes.items():
            cols = set(_split_select_list(body))
            if cols:
                cte_columns[name] = cols
        return {
            "relations": relations,
            "columns": columns,
            "aliases": alias_map,
            "cte_columns": cte_columns,
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
    alias_map: Dict[str, str] = {}
    cte_columns: Dict[str, Set[str]] = {}

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

    def _visit(node: Dict[str, Any], current_relation: str | None = None, current_cte: str | None = None) -> None:
        if not isinstance(node, dict):
            return

        relation = node.get("Relation Name")
        schema = node.get("Schema")
        alias = node.get("Alias")
        cte_name = node.get("CTE Name")
        normalized_rel = None

        if relation and schema:
            token = f"{schema}.{relation}"
            normalized_rel = _normalize_relation(token)
            if normalized_rel:
                relations.add(normalized_rel)

        outputs = node.get("Output") or []

        if alias and normalized_rel:
            alias_map[alias] = normalized_rel
        elif alias and cte_name:
            alias_map[alias] = cte_name

        if outputs:
            targets: list[str] = []
            if normalized_rel:
                targets.append(normalized_rel)
            if alias:
                targets.append(alias)
            if cte_name:
                targets.append(cte_name)
            if current_cte and not (normalized_rel or alias or cte_name):
                targets.append(current_cte)
            for target in targets:
                cols = columns.setdefault(target, set())
                for entry in outputs:
                    col = _normalise_column(entry)
                    if col:
                        cols.add(col)
                if target == cte_name:
                    cte_columns.setdefault(cte_name, set()).update(cols)

        next_relation = normalized_rel or current_relation
        next_cte = cte_name or current_cte
        for child in node.get("Plans", []) or []:
            _visit(child, next_relation, next_cte)

    _visit(plan)

    return {
        "relations": relations,
        "columns": columns,
        "aliases": alias_map,
        "cte_columns": cte_columns,
        "explain_failed": explain_failed,
        "error_text": error_text,
    }


def extract_requested_identifiers(sql: str) -> Dict[str, Set[str]]:
    relations = _extract_relations_from_sql(sql)
    aliases = _extract_aliases(sql)
    alias_columns = _extract_select_alias_columns(sql)
    ctes = _extract_ctes(sql)
    columns_map: Dict[str, Set[str]] = {}

    for alias, base in aliases.items():
        if alias_columns.get(alias):
            columns_map[alias] = set(alias_columns[alias])
            relations.add(base)

    select_cols = _split_select_list(sql)
    if select_cols:
        if len(relations) == 1 and not aliases:
            relation = next(iter(relations))
            columns_map.setdefault(relation, set()).update(select_cols)
        elif len(ctes) == 1 and not aliases:
            cte_name = next(iter(ctes.keys()))
            columns_map.setdefault(cte_name, set()).update(select_cols)

    relations.update(ctes.keys())

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
    relations = set(identifiers.get("relations", set()))
    columns_map = identifiers.get("columns", {})
    explain_failed = identifiers.get("explain_failed", False)
    error_text = identifiers.get("error_text", "")
    alias_map = identifiers.get("aliases", {}) or {}
    cte_columns_map = identifiers.get("cte_columns", {}) or {}
    if requested is None:
        requested = extract_requested_identifiers(sql)
    requested_relations = set(requested.get("relations", set()))
    requested_columns = {
        key: set(value) for key, value in (requested.get("columns", {}) or {}).items()
    }
    unknown_relations: Set[str] = set()
    unknown_columns: Dict[str, Set[str]] = {}

    base_relations: Set[str] = set()
    for rel in relations:
        if "." in rel:
            base_relations.add(rel)
    for mapped in alias_map.values():
        if isinstance(mapped, str) and "." in mapped:
            base_relations.add(mapped)

    for relation in base_relations:
        schema, table = relation.split(".", 1)
        table_map = schema_cache.get(schema, {})
        if table not in table_map:
            unknown_relations.add(f"{schema}.{table}")

    for relation_key, cols in columns_map.items():
        if not cols:
            continue
        storage_key = relation_key
        target_relation = None

        mapped = alias_map.get(relation_key)
        if mapped:
            if mapped in cte_columns_map:
                allowed = cte_columns_map.get(mapped, set())
                missing = {col for col in cols if col not in allowed}
                if missing:
                    unknown_columns.setdefault(relation_key, set()).update(missing)
                continue
            target_relation = mapped
        elif relation_key in cte_columns_map:
            allowed = cte_columns_map.get(relation_key, set())
            missing = {col for col in cols if col not in allowed}
            if missing:
                unknown_columns.setdefault(relation_key, set()).update(missing)
            continue
        elif "." in relation_key:
            target_relation = relation_key
        else:
            continue

        if target_relation and "." in target_relation:
            schema, table = target_relation.split(".", 1)
            table_map = schema_cache.get(schema, {})
            allowed = table_map.get(table, set())
            if allowed is None:
                unknown_relations.add(f"{schema}.{table}")
                continue
            missing = {col for col in cols if col not in allowed}
            if missing:
                key = storage_key if storage_key in alias_map else f"{schema}.{table}"
                unknown_columns.setdefault(key, set()).update(missing)

    if not columns_map:
        select_cols = _split_select_list(sql)
        if select_cols and len(base_relations) == 1:
            relation = next(iter(base_relations))
            schema, table = relation.split(".", 1)
            table_map = schema_cache.get(schema, {})
            allowed = table_map.get(table, set())
            missing = {col for col in select_cols if col not in (allowed or set())}
            if missing:
                unknown_columns[relation] = missing
        elif select_cols and len(cte_columns_map) == 1:
            cte_name = next(iter(cte_columns_map.keys()))
            allowed = cte_columns_map.get(cte_name, set())
            missing = {col for col in select_cols if col not in allowed}
            if missing:
                unknown_columns[cte_name] = missing

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
