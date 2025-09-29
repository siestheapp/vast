"""Deterministic utterance resolver that maps simple requests to schema catalog entries."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .db import get_ro_engine

_TEXT_TYPES = {"char", "varchar", "text", "citext", "name", "uuid"}
_PREFERRED_LIST_COLUMNS = ["username", "email", "name", "slug", "title"]
_ALIAS_WEIGHT = 0.6
_TABLE_WEIGHT = 0.3
_COLUMN_WEIGHT = 0.1
_TOP_K = 3
_TIMEOUT_MS = 2000

_INTENT_PATTERNS = {
    "count": re.compile(r"\b(count|how\s+many)\b", re.IGNORECASE),
    "list": re.compile(r"\b(list|show|give\s+me)\b", re.IGNORECASE),
}


def resolve_entities(utterance: str, cards: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve an utterance into an intent, table candidates, and column hints."""

    intent = _detect_intent(utterance)
    tokens = _tokenize(utterance)
    token_string = " ".join(tokens)
    candidates = _score_candidates(tokens, token_string, cards)

    column_hints: List[str] = []
    if intent == "list" and candidates:
        best_card = cards.get(candidates[0]["key"], {})
        column_hints = _column_hints(best_card)

    return {
        "intent": intent,
        "candidates": [
            {
                "schema": c["schema"],
                "table": c["table"],
                "score": c["score"],
            }
            for c in candidates[:_TOP_K]
        ],
        "column_hints": column_hints,
    }


def run_template_count(schema: str, table: str) -> Tuple[int, Dict[str, int]]:
    """Execute a deterministic COUNT template for the requested table."""

    engine_start = time.perf_counter()
    engine = get_ro_engine()
    engine_ms = int((time.perf_counter() - engine_start) * 1000)
    qualified = _qualified_identifier(engine, schema, table)
    query = text(f"SELECT COUNT(*) AS count FROM {qualified}")

    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = '{_TIMEOUT_MS}ms'"))
        exec_start = time.perf_counter()
        result = conn.execute(query).scalar()
        exec_ms = int((time.perf_counter() - exec_start) * 1000)

    return int(result or 0), {"engine_ms": engine_ms, "exec_ms": exec_ms}


def run_template_list(
    schema: str,
    table: str,
    column: str,
    limit: int = 50,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Execute a deterministic SELECT template returning a single column."""

    engine_start = time.perf_counter()
    engine = get_ro_engine()
    engine_ms = int((time.perf_counter() - engine_start) * 1000)
    qualified = _qualified_identifier(engine, schema, table)
    quoted_column = _quote_column(engine, column)
    alias = _quote_column(engine, column)
    query = text(
        f"SELECT {quoted_column} AS {alias} "
        f"FROM {qualified} "
        "LIMIT :limit"
    )

    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = '{_TIMEOUT_MS}ms'"))
        exec_start = time.perf_counter()
        result = conn.execute(query, {"limit": int(max(1, limit))}).mappings().all()
        exec_ms = int((time.perf_counter() - exec_start) * 1000)

    return [dict(row) for row in result], {"engine_ms": engine_ms, "exec_ms": exec_ms}


def _detect_intent(utterance: str) -> str:
    for intent, pattern in _INTENT_PATTERNS.items():
        if pattern.search(utterance or ""):
            return intent
    return "unknown"


def _tokenize(utterance: str) -> List[str]:
    raw_tokens = re.findall(r"[a-zA-Z0-9_]+", (utterance or "").lower())
    tokens = []
    for token in raw_tokens:
        singular = _singularize(token)
        if singular:
            tokens.append(singular)
    return tokens


def _singularize(token: str) -> str:
    if token.endswith("ies") and len(token) > 3:
        return token[:-3] + "y"
    if token.endswith("ses") and len(token) > 3:
        return token[:-2]
    if token.endswith("s") and len(token) > 3:
        return token[:-1]
    return token


def _score_candidates(tokens: Sequence[str], token_string: str, cards: Dict[str, Dict[str, Any]]):
    scored: List[Dict[str, Any]] = []
    token_set = set(tokens)

    for key, card in cards.items():
        schema = card.get("schema")
        table = card.get("table")
        if not schema or not table:
            continue

        alias_score = _score_aliases(card.get("aliases") or [], tokens, token_string)
        table_score = _score_table_name(table, tokens, token_set)
        column_score = _score_columns(card.get("columns") or [], token_set)

        score = (
            _ALIAS_WEIGHT * alias_score
            + _TABLE_WEIGHT * table_score
            + _COLUMN_WEIGHT * column_score
        )

        if score <= 0:
            continue

        scored.append(
            {
                "key": key,
                "schema": schema,
                "table": table,
                "score": round(score, 4),
            }
        )

    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored


def _score_aliases(aliases: Iterable[str], tokens: Sequence[str], token_string: str) -> float:
    if not aliases:
        return 0.0
    token_string = token_string.lower()
    score = 0.0
    for alias in aliases:
        alias_norm = alias.lower().strip()
        if not alias_norm:
            continue
        alias_tokens = alias_norm.split()
        if all(t in tokens for t in alias_tokens):
            score += 1.0
        elif alias_norm in token_string:
            score += 0.5
    return min(score, 3.0)


def _score_table_name(table: str, tokens: Sequence[str], token_set: set[str]) -> float:
    table_tokens = table.lower().replace("_", " ").split()
    hits = sum(1 for t in table_tokens if t in token_set)
    if not table_tokens:
        return 0.0
    char_overlap = _character_overlap_score(table.lower(), tokens)
    raw_score = (hits / len(table_tokens)) + char_overlap
    return min(raw_score, 1.5)


def _character_overlap_score(name: str, tokens: Sequence[str]) -> float:
    if not name or not tokens:
        return 0.0
    length = max(1, len(name) - 2)
    ngrams = {name[i : i + 3] for i in range(length)}
    if not ngrams:
        return 0.0
    token_string = "".join(tokens)
    hits = sum(1 for n in ngrams if n in token_string)
    return hits / len(ngrams)


def _score_columns(columns: Sequence[Dict[str, Any]], token_set: set[str]) -> float:
    if not columns:
        return 0.0
    score = 0.0
    for col in columns:
        name = str(col.get("name") or "").lower()
        if not name:
            continue
        if name in token_set:
            score += 0.5
    return min(score, 2.0)


def _column_hints(card: Dict[str, Any]) -> List[str]:
    columns = card.get("columns") or []
    available_names = {
        str(col.get("name") or "").lower(): str(col.get("name") or "")
        for col in columns
    }
    hints: List[str] = []

    for preferred in _PREFERRED_LIST_COLUMNS:
        column_name = available_names.get(preferred)
        if not column_name:
            continue
        column = _lookup_column(columns, column_name)
        if column and _is_text_like(column.get("type")):
            hints.append(column_name)
    if hints:
        return hints

    for col in columns:
        name = str(col.get("name") or "")
        if name and _is_text_like(col.get("type")):
            hints.append(name)
            break
    return hints


def _lookup_column(columns: Sequence[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for col in columns:
        if str(col.get("name") or "") == name:
            return col
    return None


def _is_text_like(col_type: Any) -> bool:
    if col_type is None:
        return False
    try:
        python_type = getattr(col_type, "python_type", None)
        if python_type is str:
            return True
    except NotImplementedError:
        pass
    return any(token in str(col_type).lower() for token in _TEXT_TYPES)


def _qualified_identifier(engine: Engine, schema: str, table: str) -> str:
    preparer = engine.dialect.identifier_preparer
    return f"{preparer.quote_schema(schema)}.{preparer.quote(table)}"


def _quote_column(engine: Engine, column: str) -> str:
    preparer = engine.dialect.identifier_preparer
    return preparer.quote(column)


__all__ = [
    "resolve_entities",
    "run_template_count",
    "run_template_list",
]
