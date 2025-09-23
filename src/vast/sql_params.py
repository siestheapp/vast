"""Helpers for handling optional SQL parameters in read-only queries."""

from __future__ import annotations

import re
from typing import Dict


DEFAULTS_RO: Dict[str, int] = {
    "limit": 1,
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
    """Replace `LIMIT :limit` with `LIMIT 1` when no limit parameter is provided."""

    if not is_select_only(sql):
        return sql

    if params and params.get("limit") is not None:
        return sql

    if not _LIMIT_BIND_RE.search(sql or ""):
        return sql

    return _LIMIT_BIND_RE.sub("LIMIT 1", sql)
