"""Helpers for handling optional SQL parameters in read-only queries."""

from __future__ import annotations

import re
from typing import Dict

from .config import settings


DEFAULT_LIMIT = getattr(settings, "VAST_DEFAULT_LIMIT", 10) or 10


DEFAULTS_RO: Dict[str, int] = {
    "limit": DEFAULT_LIMIT,
    "offset": 0,
}

_BIND_RE = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")
_LEADING_COMMENT_RE = re.compile(r"^(\s*--[^\n]*\n|\s*/\*.*?\*/\s*)+", re.DOTALL)
_LIMIT_BIND_RE = re.compile(r"(?i)\bLIMIT\s+:limit\b")


def _strip_leading_comments(sql: str) -> str:
    """Remove leading SQL comments so we can detect the first keyword."""

    remaining = sql or ""
    while True:
        match = _LEADING_COMMENT_RE.match(remaining)
        if not match:
            break
        remaining = remaining[match.end():]
    return remaining.lstrip()


def is_select_only(sql: str) -> bool:
    """Return True if the SQL statement is a plain SELECT."""

    head = _strip_leading_comments(sql)
    return head.lower().startswith("select")


def hydrate_readonly_params(sql: str, params: Dict[str, object] | None) -> Dict[str, object]:
    """Ensure pagination parameters exist for read-only statements.

    For SELECT-only statements, if `:limit` or `:offset` named binds are present
    but not supplied, this function injects safe defaults.
    """

    provided = dict(params or {})
    if not is_select_only(sql):
        return provided

    binds = set(_BIND_RE.findall(sql))
    for key, default in DEFAULTS_RO.items():
        if key in binds and key not in provided:
            provided[key] = default
    return provided


def normalize_limit_literal(sql: str, params: Dict[str, object] | None) -> str:
    """Replace `LIMIT :limit` with a literal default when the parameter is missing."""

    if not is_select_only(sql):
        return sql

    if params and params.get("limit") is not None:
        return sql

    if not _LIMIT_BIND_RE.search(sql or ""):
        return sql

    return _LIMIT_BIND_RE.sub(f"LIMIT {DEFAULT_LIMIT}", sql)


def stmt_kind(sql: str) -> str:
    """Classify a SQL statement into broad categories."""

    if not sql:
        return "OTHER"

    match = re.match(r"\s*(\w+)", sql)
    if not match:
        return "OTHER"

    token = match.group(1).upper()
    if token in {"CREATE", "ALTER", "DROP", "TRUNCATE"}:
        return "DDL"
    if token in {"INSERT", "UPDATE", "DELETE", "MERGE"}:
        return "DML"
    if token in {"SELECT", "WITH"}:
        return "SELECT"
    return "OTHER"
