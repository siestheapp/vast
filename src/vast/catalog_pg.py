"""Postgres catalog and health helpers.

These functions centralize the SQL we use to inspect Postgres internals so
they can be shared across features while remaining version-safe (PG 12–16).
"""

from __future__ import annotations

from typing import Dict, List

from sqlalchemy import text

from .db import get_engine


def _fetch_all(sql: str, params: dict | None = None) -> List[Dict[str, object]]:
    """Execute the SQL against the read-only engine and return plain dict rows."""
    with get_engine(readonly=True).begin() as conn:
        result = conn.execute(text(sql), params or {})
        return [dict(row) for row in result.mappings()]


def largest_tables(limit: int = 10) -> List[Dict[str, object]]:
    """Top tables by total relation size."""
    return _fetch_all(
        """
        SELECT
          n.nspname || '.' || c.relname AS table,
          pg_total_relation_size(c.oid) AS total_bytes,
          COALESCE(pg_stat_get_live_tuples(c.oid), 0) AS approx_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r'
        ORDER BY pg_total_relation_size(c.oid) DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )


def seq_scans_by_table(limit: int = 10) -> List[Dict[str, object]]:
    """Tables ordered by sequential scan volume."""
    return _fetch_all(
        """
        SELECT
          schemaname || '.' || relname AS table,
          seq_scan,
          idx_scan,
          n_live_tup AS live_rows
        FROM pg_stat_user_tables
        ORDER BY seq_scan DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )


def unused_indexes(limit: int = 10) -> List[Dict[str, object]]:
    """Indexes that have never been scanned since stats collection began."""
    return _fetch_all(
        """
        SELECT
          s.schemaname || '.' || s.relname AS table,
          ic.relname AS index,
          pg_relation_size(i.indexrelid) AS index_bytes,
          COALESCE(x.idx_scan, 0) AS idx_scan
        FROM pg_class c
        JOIN pg_index i ON i.indrelid = c.oid
        JOIN pg_class ic ON ic.oid = i.indexrelid
        JOIN pg_stat_user_indexes x ON x.indexrelid = i.indexrelid
        JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE x.idx_scan = 0
        ORDER BY pg_relation_size(i.indexrelid) DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )

