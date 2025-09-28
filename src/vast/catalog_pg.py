"""Postgres catalog helpers and schema card utilities."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import String, bindparam, inspect, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from .db import get_engine
from .introspect import fingerprint_from_columns, list_tables

logger = logging.getLogger(__name__)

DATABASE_SIZE_SQL = """
SELECT
  pg_database_size(current_database())                 AS size_bytes,
  pg_size_pretty(pg_database_size(current_database())) AS size_pretty
""".strip()

CARDS_DIR = Path(".vast/schema_cards")
INDEX_PATH = Path(".vast/schema_index.json")
EXAMPLE_LIMIT = 5
EXAMPLE_TIMEOUT = "750ms"

_TEXT_SAMPLE_TYPES = {
    "char",
    "varchar",
    "text",
    "citext",
    "name",
    "uuid",
}

_ALIAS_SEEDS = {
    "users": ["user", "member", "account", "customer"],
    "user": ["member", "account", "customer"],
    "customers": ["customer", "client", "account"],
    "orders": ["order", "purchase"],
    "order_items": ["order item", "line item", "line"],
    "products": ["product", "item", "sku"],
    "payments": ["payment", "transaction"],
    "invoices": ["invoice", "bill"],
    "organizations": ["organization", "org", "company"],
    "companies": ["company", "organization", "org"],
}


def _fetch_all(sql: str, params: dict | None = None) -> List[Dict[str, object]]:
    """Execute the SQL against the read-only engine and return plain dict rows."""
    with get_engine(readonly=True).begin() as conn:
        result = conn.execute(text(sql), params or {})
        return [dict(row) for row in result.mappings()]


def database_size() -> Dict[str, object]:
    """Return the current database size in bytes and a pretty string."""
    rows = _fetch_all(DATABASE_SIZE_SQL)
    if not rows:
        return {}
    row = rows[0]
    size_bytes = row.get("size_bytes")
    size_pretty = row.get("size_pretty")
    return {
        "size_bytes": int(size_bytes) if size_bytes is not None else 0,
        "size_pretty": str(size_pretty) if size_pretty is not None else "0 bytes",
    }


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


def _is_privilege_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "permission denied" in message
        or "must be owner" in message
        or "does not exist" in message
    )


def _row_estimates(engine, schemas: Sequence[str]) -> Dict[Tuple[str, str], Optional[int]]:
    if not schemas:
        return {}
    stmt = (
        text(
            """
            SELECT schemaname, relname, n_live_tup
            FROM pg_stat_all_tables
            WHERE schemaname = ANY(:schemas)
            """
        ).bindparams(bindparam("schemas", type_=ARRAY(String)))
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt, {"schemas": list(schemas)}).mappings().all()
    out: Dict[Tuple[str, str], Optional[int]] = {}
    for row in rows:
        key = (row.get("schemaname"), row.get("relname"))
        try:
            out[key] = int(row.get("n_live_tup"))
        except (TypeError, ValueError):
            out[key] = None
    return out


def _is_stringish(col_type: Any) -> bool:
    if col_type is None:
        return False

    try:
        python_type = getattr(col_type, "python_type", None)
        if python_type is str:
            return True
    except NotImplementedError:
        pass

    text_name = str(col_type).lower()
    return any(token in text_name for token in _TEXT_SAMPLE_TYPES)


def _collect_examples(engine, schema: str, table: str, columns: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    if not columns:
        return {}

    examples: Dict[str, List[str]] = {}
    preparer = engine.dialect.identifier_preparer
    qualified = f"{preparer.quote_schema(schema)}.{preparer.quote(table)}"

    for col in columns:
        col_name = col.get("name")
        if not col_name:
            continue
        if not _is_stringish(col.get("type")):
            continue

        quoted_col = preparer.quote(col_name)
        query = text(
            f"SELECT DISTINCT {quoted_col} AS value "
            f"FROM {qualified} "
            f"WHERE {quoted_col} IS NOT NULL "
            "LIMIT :limit"
        )

        try:
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL statement_timeout = '{EXAMPLE_TIMEOUT}'"))
                result = conn.execute(query, {"limit": EXAMPLE_LIMIT}).mappings()
                values: List[str] = []
                for row in result:
                    value = row.get("value")
                    if value is None:
                        continue
                    values.append(str(value))
                    if len(values) >= EXAMPLE_LIMIT:
                        break
        except SQLAlchemyError as exc:
            logger.debug(
                "Failed to sample examples for %s.%s.%s: %s",
                schema,
                table,
                col_name,
                exc,
            )
            continue

        if values:
            examples[col_name] = values

    return examples


def _normalize_alias(value: str) -> str:
    return value.strip().lower().replace("_", " ")


def _maybe_singular(table: str) -> Iterable[str]:
    if table.endswith("ies"):
        yield table[:-3] + "y"
    elif table.endswith("ses"):
        yield table[:-2]
    elif table.endswith("s") and len(table) > 1:
        yield table[:-1]


def _alias_from_columns(columns: List[Dict[str, Any]]) -> List[str]:
    names = {c.get("name", "").lower() for c in columns}
    hints: List[str] = []
    account_markers = {"email", "password", "password_hash", "last_login", "first_name", "last_name", "username"}
    if names & account_markers:
        hints.extend(["user", "account", "profile"])
    if "sku" in names or "upc" in names:
        hints.extend(["product", "item"])
    if "total" in names or "amount" in names:
        hints.append("transaction")
    if "status" in names and "order_id" in names:
        hints.append("order")
    return hints


def _collect_aliases(table_name: str, columns: List[Dict[str, Any]], comment: Optional[str]) -> List[str]:
    aliases = set()
    lowered = table_name.lower()
    for seed in _ALIAS_SEEDS.get(lowered, []):
        aliases.add(_normalize_alias(seed))

    aliases.update(_normalize_alias(a) for a in _alias_from_columns(columns))

    for singular in _maybe_singular(lowered):
        aliases.add(_normalize_alias(singular))

    if comment:
        comment_low = comment.lower()
        for word in ("customer", "user", "payment", "invoice", "order", "session", "event"):
            if word in comment_low:
                aliases.add(_normalize_alias(word))

    aliases.discard(_normalize_alias(lowered))
    if not aliases:
        return []
    return sorted(aliases)


def build_schema_cards() -> Dict[str, Dict[str, Any]]:
    engine = get_engine(readonly=True)
    insp = inspect(engine)
    cards: Dict[str, Dict[str, Any]] = {}

    tables = list_tables()
    if not tables:
        return cards

    schemas = sorted({row["table_schema"] for row in tables})
    estimate_lookup = _row_estimates(engine, schemas)

    for entry in tables:
        schema = entry["table_schema"]
        table = entry["table_name"]
        key = f"{schema}.{table}"

        try:
            cols = insp.get_columns(table_name=table, schema=schema)
        except (ProgrammingError, SQLAlchemyError) as exc:
            if _is_privilege_error(exc):
                logger.debug("Column reflection blocked for %s: %s", key, exc)
                cols = []
            else:
                raise

        column_payload: List[Dict[str, Any]] = []
        for col in cols:
            column_payload.append(
                {
                    "name": col.get("name"),
                    "type": str(col.get("type")),
                    "nullable": bool(col.get("nullable", True)),
                    "comment": col.get("comment"),
                }
            )

        try:
            pk_info = insp.get_pk_constraint(table_name=table, schema=schema) or {}
        except SQLAlchemyError as exc:
            if _is_privilege_error(exc):
                logger.debug("PK reflection blocked for %s: %s", key, exc)
                pk_info = {}
            else:
                raise
        pk_columns = pk_info.get("constrained_columns") or []

        fk_payload: List[Dict[str, Any]] = []
        try:
            foreign_keys = insp.get_foreign_keys(table_name=table, schema=schema)
        except SQLAlchemyError as exc:
            if _is_privilege_error(exc):
                logger.debug("FK reflection blocked for %s: %s", key, exc)
                foreign_keys = []
            else:
                raise
        for fk in foreign_keys:
            constrained = fk.get("constrained_columns") or []
            referred_columns = fk.get("referred_columns") or []
            ref_schema = fk.get("referred_schema") or schema
            ref_table = fk.get("referred_table")
            for idx, col_name in enumerate(constrained):
                ref_col = referred_columns[idx] if idx < len(referred_columns) else None
                fk_payload.append(
                    {
                        "column": col_name,
                        "ref_table": f"{ref_schema}.{ref_table}" if ref_table else ref_table,
                        "ref_column": ref_col,
                    }
                )

        index_payload: List[Dict[str, Any]] = []
        try:
            indexes = insp.get_indexes(table_name=table, schema=schema)
        except SQLAlchemyError as exc:
            if _is_privilege_error(exc):
                logger.debug("Index reflection blocked for %s: %s", key, exc)
                indexes = []
            else:
                raise
        for idx in indexes:
            index_payload.append(
                {
                    "name": idx.get("name"),
                    "columns": list(idx.get("column_names") or []),
                    "unique": bool(idx.get("unique")),
                }
            )

        try:
            comment_info = insp.get_table_comment(table_name=table, schema=schema) or {}
        except SQLAlchemyError as exc:
            if _is_privilege_error(exc):
                logger.debug("Comment reflection blocked for %s: %s", key, exc)
                comment_info = {}
            else:
                raise
        table_comment = comment_info.get("text")

        try:
            examples = _collect_examples(engine, schema, table, column_payload)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to collect examples for %s: %s", key, exc)
            examples = {}

        aliases = _collect_aliases(table, column_payload, table_comment)

        cards[key] = {
            "schema": schema,
            "table": table,
            "pk": list(pk_columns),
            "columns": column_payload,
            "fks": fk_payload,
            "indexes": index_payload,
            "row_estimate": estimate_lookup.get((schema, table)),
            "examples": examples,
            "comments": {
                "table": table_comment,
                "columns": {
                    col.get("name"): col.get("comment") for col in column_payload if col.get("name")
                },
            },
            "aliases": aliases,
        }

        card = cards[key]
        alias_set = set(card.get("aliases") or [])
        table_name = card.get("table") or ""
        if table_name:
            table_lower = table_name.lower()
            alias_set.add(_normalize_alias(table_lower))
            if not table_lower.endswith("s"):
                alias_set.add(_normalize_alias(f"{table_lower}s"))
            if table_lower.endswith("s") and len(table_lower) > 1:
                alias_set.add(_normalize_alias(table_lower[:-1]))

        colnames = {
            (str(col.get("name")) or "").lower()
            for col in card.get("columns") or []
            if col.get("name")
        }

        if {"email", "username", "last_login", "is_active"} & colnames:
            alias_set.update(_normalize_alias(value) for value in ("user", "users", "account", "member"))
        if {"slug", "name"} & colnames:
            alias_set.update(_normalize_alias(value) for value in ("brand", "brands", "profile"))
        if {"price", "currency", "sku", "upc"} & colnames:
            alias_set.update(_normalize_alias(value) for value in ("product", "products", "item", "items"))

        card["aliases"] = sorted(alias_set)

    return cards


def schema_fingerprint(cards: Dict[str, Dict[str, Any]]) -> str:
    specs = []
    for key in sorted(cards, key=lambda k: (cards[k]["schema"], cards[k]["table"])):
        card = cards[key]
        schema = card.get("schema") or ""
        table = card.get("table") or ""
        columns = [
            (col.get("name"), str(col.get("type")))
            for col in (card.get("columns") or [])
        ]
        specs.append((schema, table, columns))
    return fingerprint_from_columns(specs)


def _card_path(schema: str, table: str) -> Path:
    return CARDS_DIR / f"{schema}.{table}.json"


def save_schema_cards(cards: Dict[str, Dict[str, Any]]) -> None:
    CARDS_DIR.mkdir(parents=True, exist_ok=True)

    for card in cards.values():
        schema = card.get("schema")
        table = card.get("table")
        if not schema or not table:
            continue
        path = _card_path(schema, table)
        path.write_text(json.dumps(card, indent=2, sort_keys=True))

    fp = schema_fingerprint(cards)
    tables_index = []
    for card in sorted(cards.values(), key=lambda c: (c.get("schema"), c.get("table"))):
        tables_index.append(
            {
                "schema": card.get("schema"),
                "table": card.get("table"),
                "aliases": card.get("aliases", []),
            }
        )

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps({"fingerprint": fp, "tables": tables_index}, indent=2, sort_keys=True)
    )


def _load_cards_from_disk(table_entries: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Dict[str, Any]]]:
    cards: Dict[str, Dict[str, Any]] = {}
    for entry in table_entries:
        schema = entry.get("schema")
        table = entry.get("table")
        if not schema or not table:
            continue
        path = _card_path(schema, table)
        if not path.exists():
            return None
        try:
            cards[f"{schema}.{table}"] = json.loads(path.read_text())
        except json.JSONDecodeError:
            logger.warning("Failed to parse schema card %s", path)
            return None
    return cards


def load_schema_cards(refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    if refresh or not INDEX_PATH.exists():
        cards = build_schema_cards()
        save_schema_cards(cards)
        return cards

    try:
        index_data = json.loads(INDEX_PATH.read_text())
    except json.JSONDecodeError:
        logger.warning("Schema index corrupted; rebuilding catalog")
        cards = build_schema_cards()
        save_schema_cards(cards)
        return cards

    tables_entry = index_data.get("tables") or []
    cards = _load_cards_from_disk(tables_entry)
    if cards is None:
        cards = build_schema_cards()
        save_schema_cards(cards)
        return cards

    stored_fp = index_data.get("fingerprint")
    current_fp: Optional[str] = None
    try:
        from .introspect import schema_fingerprint as live_schema_fingerprint

        current_fp = live_schema_fingerprint()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to compute live schema fingerprint: %s", exc)

    if stored_fp and current_fp and stored_fp != current_fp:
        cards = build_schema_cards()
        save_schema_cards(cards)
        return cards

    return cards


def table_aliases(cards: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for key, card in cards.items():
        aliases = card.get("aliases") or []
        out[key] = list(aliases)
    return out


__all__ = [
    "load_schema_cards",
    "build_schema_cards",
    "save_schema_cards",
    "schema_fingerprint",
    "table_aliases",
    "database_size",
    "largest_tables",
    "seq_scans_by_table",
    "unused_indexes",
    "DATABASE_SIZE_SQL",
]
