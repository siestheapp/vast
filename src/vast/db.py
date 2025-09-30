from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple, Set

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlglot import parse, parse_one
from sqlglot import exp
from sqlglot.errors import ParseError

from .config import settings, write_url, read_url
from .sql_params import stmt_kind
from .audit import audit_event


class StatementType(Enum):
    READ = auto()
    WRITE = auto()
    DDL = auto()


_engine_ro: Engine | None = None
_engine_rw: Engine | None = None


@dataclass
class SQLAnalysis:
    statement_type: StatementType
    normalized_sql: str
    tables: Set[Tuple[Optional[str], str]]
    columns: Set[Tuple[Optional[str], Optional[str], str]]
    is_select: bool


# Compatibility tuple for sqlglot set operations across versions
# Some versions expose a common SetOperation base; others only provide
# concrete nodes like Union/Except/Intersect.
SET_OPERATION_TYPES = tuple(
    t
    for t in (
        getattr(exp, "Union", None),
        getattr(exp, "Except", None),
        getattr(exp, "Intersect", None),
        getattr(exp, "SetOperation", None),
    )
    if t is not None
)


def _mk_engine(url: str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "options": (
                f"-c statement_timeout={settings.default_statement_timeout_ms}"
                f" -c idle_in_transaction_session_timeout={settings.idle_in_tx_timeout_ms}"
            )
        },
    )


def get_ro_engine() -> Engine:
    global _engine_ro
    if _engine_ro is None:
        url = read_url()
        connect_args = {
            "application_name": "vast_ro",
            "options": (
                f"-c statement_timeout={settings.default_statement_timeout_ms}"
                f" -c idle_in_transaction_session_timeout={settings.idle_in_tx_timeout_ms}"
            ),
        }
        _engine_ro = create_engine(
            url,
            pool_size=3,
            max_overflow=0,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args=connect_args,
        )
    return _engine_ro


def get_engine(readonly: bool = True) -> Engine:
    global _engine_ro, _engine_rw
    if readonly:
        return get_ro_engine()
    else:
        if _engine_rw is None:
            _engine_rw = _mk_engine(write_url())
        return _engine_rw


def _unwrap_with(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.With):
        node = node.this
    return node


def _command_name(command: exp.Command) -> str:
    name = command.name or ""
    return name.upper()


def _classify_statement(expr: exp.Expression) -> StatementType:
    expr = _unwrap_with(expr)

    # DDL nodes across sqlglot versions (AlterTable -> Alter; TruncateTable -> Truncate)
    ddl_create = getattr(exp, "Create", None)
    ddl_alter = getattr(exp, "AlterTable", None) or getattr(exp, "Alter", None)
    ddl_drop = getattr(exp, "Drop", None)
    ddl_truncate = getattr(exp, "TruncateTable", None) or getattr(exp, "Truncate", None)
    ddl_nodes = tuple(t for t in (ddl_create, ddl_alter, ddl_drop, ddl_truncate) if t is not None)
    if isinstance(expr, ddl_nodes) or any(expr.find(node) for node in ddl_nodes):
        return StatementType.DDL

    if isinstance(expr, exp.Command):
        command = _command_name(expr)
        if command in {"EXPLAIN", "SHOW"}:
            return StatementType.READ
        if command in {"ANALYZE", "VACUUM"}:
            return StatementType.WRITE
        return StatementType.DDL

    if isinstance(expr, (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.LoadData)):
        return StatementType.WRITE

    if isinstance(expr, exp.Select):
        return StatementType.READ

    # Fall back to scanning children
    if expr.find(exp.Insert) or expr.find(exp.Update) or expr.find(exp.Delete) or expr.find(exp.Merge):
        return StatementType.WRITE

    if (
        (ddl_create and expr.find(ddl_create))
        or (ddl_drop and expr.find(ddl_drop))
        or (ddl_alter and expr.find(ddl_alter))
        or (ddl_truncate and expr.find(ddl_truncate))
    ):
        return StatementType.DDL

    # Fail closed for unknown statements.
    return StatementType.DDL


def analyze_sql(sql: str) -> SQLAnalysis:
    try:
        expressions = parse(sql, read="postgres")
    except ParseError as exc:
        raise ValueError(f"Unable to parse SQL: {exc}") from exc

    if not expressions:
        raise ValueError("No SQL statements provided")
    if len(expressions) != 1:
        raise ValueError("Only single statements are allowed")

    expr = expressions[0]
    classification = _classify_statement(expr)
    normalized = expr.sql(dialect="postgres")

    try:
        root = parse_one(sql, read="postgres")
    except ParseError:
        root = expr

    alias_map: dict[str, Tuple[Optional[str], str]] = {}
    tables: Set[Tuple[Optional[str], str]] = set()
    columns: Set[Tuple[Optional[str], Optional[str], str]] = set()

    for table_expr in root.find_all(exp.Table):
        table_name = table_expr.name
        if not table_name:
            continue
        schema_name = table_expr.db or None
        tables.add((schema_name, table_name))
        tables.add((None, table_name))
        # Alias compatibility across sqlglot versions: may be a string or an expression
        alias_value = getattr(table_expr, "alias", None)
        alias_name = None
        if isinstance(alias_value, str):
            alias_name = alias_value
        elif alias_value is not None:
            # Prefer direct .name if present; otherwise try .this.name
            name_attr = getattr(alias_value, "name", None)
            if isinstance(name_attr, str) and name_attr:
                alias_name = name_attr
            else:
                alias_this = getattr(alias_value, "this", None)
                alias_name = getattr(alias_this, "name", None)
        if alias_name:
            alias_map[alias_name] = (schema_name, table_name)

    for column_expr in root.find_all(exp.Column):
        column_name = column_expr.name
        if not column_name:
            continue
        table_ref = column_expr.table
        schema_ref: Optional[str] = None
        table_resolved: Optional[str] = None
        if table_ref:
            mapping = alias_map.get(table_ref)
            if mapping:
                schema_ref, table_resolved = mapping
            else:
                table_resolved = table_ref
        columns.add((schema_ref, table_resolved, column_name))
        if table_resolved:
            columns.add((None, table_resolved, column_name))
        else:
            columns.add((None, None, column_name))

    target = expr
    while isinstance(target, exp.With):
        target = target.this
    is_select_stmt = isinstance(target, (exp.Select, *SET_OPERATION_TYPES))

    return SQLAnalysis(
        statement_type=classification,
        normalized_sql=normalized,
        tables=tables,
        columns=columns,
        is_select=is_select_stmt,
    )


def analyse_sql(sql: str) -> SQLAnalysis:  # noqa: D401
    """British alias for analyze_sql."""
    return analyze_sql(sql)


def _estimate_write_rows(sql: str, params: dict | None) -> Optional[int]:
    """
    For UPDATE/DELETE/MERGE: try EXPLAIN (FORMAT JSON) to estimate affected rows.
    Returns None if estimate unavailable.
    """
    explain_sql = f"EXPLAIN (FORMAT JSON) {sql}"
    with get_ro_engine().begin() as conn:  # EXPLAIN is read-only
        res = conn.execute(text(explain_sql), params or {})
        row = res.fetchone()
        if not row:
            return None
        try:
            payload = row[0]  # JSON column
            # Postgres returns a JSON array with a single object
            plan = payload[0]["Plan"]
            return int(plan.get("Plan Rows") or plan.get("Rows") or 0)
        except Exception:
            return None


def _consume_result(res) -> Tuple[List[Dict[str, Any]], List[str], int]:
    """Materialise a SQLAlchemy result into plain rows/columns."""

    rows: List[Dict[str, Any]] = []
    columns: List[str] = []
    row_count = 0

    if getattr(res, "returns_rows", False):
        try:
            mappings = res.mappings().all()
            rows = [dict(row) for row in mappings]
        except Exception:
            fetched = res.fetchall()
            rows = []
            for row in fetched:
                mapping = getattr(row, "_mapping", None)
                if mapping is not None:
                    rows.append(dict(mapping))
                elif isinstance(row, dict):
                    rows.append(dict(row))
                else:
                    try:
                        rows.append(dict(row))
                    except Exception:
                        rows.append({"value": row})
        try:
            columns = list(res.keys())
        except Exception:
            if rows:
                columns = list(rows[0].keys())
        row_count = len(rows)
    else:
        try:
            row_count = int(res.rowcount)
        except Exception:
            row_count = 0
        if row_count < 0:
            row_count = 0

    return rows, columns, row_count


def _normalize_explain_rows(
    rows: List[Dict[str, Any]],
    columns: List[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    if not rows:
        return rows, ["plan"]

    normalized: List[Dict[str, Any]] = []
    for row in rows:
        value: Any = row
        if isinstance(row, dict):
            if len(row) == 1:
                value = next(iter(row.values()))
            else:
                value = row
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                value = value.strip()
        normalized.append({"plan": value})
    return normalized, ["plan"]


def safe_execute(
    sql: str,
    params: dict | None = None,
    allow_writes: bool = False,
    force_write: bool = False,
):
    """
    - DDL is blocked (use migration workflow).
    - Writes require allow_writes; if !force_write => DRY RUN (returns preview).
    - For writes, run EXPLAIN gate and block if estimate exceeds max_write_rows.
    - Reads use RO engine; actual write execution uses RW engine.
    """
    sql_stripped = sql.strip() if sql else ""
    if sql_stripped.upper().startswith("EXPLAIN"):
        analysis = SQLAnalysis(
            statement_type=StatementType.READ,
            normalized_sql=sql_stripped,
            tables=set(),
            columns=set(),
            is_select=False,
        )
    else:
        analysis = analyze_sql(sql)
    stmt_type = analysis.statement_type
    normalized_sql = analysis.normalized_sql

    if stmt_type is StatementType.DDL:
        raise ValueError("DDL statements are blocked. Use a migration workflow.")

    sql_kind = stmt_kind(normalized_sql)

    try:
        # before
        audit_event({
            "phase": "pre",
            "stmt_type": stmt_type.name,
            "sql": normalized_sql,
            "params": params or {},
            "allow_writes": allow_writes,
            "force_write": force_write,
        })

        def _build_payload() -> Dict[str, Any]:
            return {
                "rows": [],
                "columns": [],
                "row_count": 0,
                "stmt_kind": sql_kind,
                "write": sql_kind not in {"SELECT", "EXPLAIN"},
                "dry_run": False,
                "exec_ms": 0,
                "engine_ms": 0,
                "meta": {"engine_ms": 0, "exec_ms": 0},
            }

        if stmt_type is StatementType.WRITE:
            if not allow_writes:
                raise ValueError("Write queries are disabled. Use --write to permit writes.")

            # Estimate rows and gate
            est = _estimate_write_rows(normalized_sql, params)
            if est is not None and est > settings.max_write_rows:
                raise ValueError(
                    f"Write blocked: estimated affected rows {est} exceeds limit {settings.max_write_rows}."
                )

            if not force_write:
                audit_event({"phase": "dry_run", "estimated_rows": est})
                notice = {
                    "_notice": "DRY RUN — not executed",
                    "_sql": normalized_sql,
                    "_params": params or {},
                    "_estimated_rows": est,
                }
                payload = _build_payload()
                payload["rows"] = [notice]
                payload["columns"] = list(notice.keys())
                payload["dry_run"] = True
                return payload

            # Execute with RW engine only when truly writing
            with get_engine(readonly=False).begin() as conn:
                start = time.perf_counter()
                res = conn.execute(text(normalized_sql), params or {})
                rows, columns, row_count = _consume_result(res)
                if sql_kind == "EXPLAIN":
                    rows, columns = _normalize_explain_rows(rows, columns)
                    row_count = len(rows)
                payload = _build_payload()
                payload.update({
                    "rows": rows,
                    "columns": columns,
                    "row_count": row_count,
                })
                duration_ms = int((time.perf_counter() - start) * 1000)
                payload["engine_ms"] = duration_ms
                payload["exec_ms"] = duration_ms
                payload["meta"] = {"engine_ms": duration_ms, "exec_ms": duration_ms}
        else:
            # READ path — strictly RO engine
            with get_ro_engine().begin() as conn:
                start = time.perf_counter()
                res = conn.execute(text(normalized_sql), params or {})
                rows, columns, row_count = _consume_result(res)
                if sql_kind == "EXPLAIN":
                    rows, columns = _normalize_explain_rows(rows, columns)
                    row_count = len(rows)
                duration_ms = int((time.perf_counter() - start) * 1000)
                payload = _build_payload()
                payload.update({
                    "rows": rows,
                    "columns": columns,
                    "row_count": row_count,
                })
                payload["engine_ms"] = duration_ms
                payload["exec_ms"] = duration_ms
                payload["meta"] = {"engine_ms": duration_ms, "exec_ms": duration_ms}

        # after success
        audit_event({
            "phase": "post",
            "success": True,
            "rows": payload.get("row_count"),
        })
        return payload
    except Exception as exc:
        audit_event({
            "phase": "post",
            "success": False,
            "error": str(exc),
        })
        raise


def is_select(sql: str) -> bool:
    analysis = analyze_sql(sql)
    return analysis.is_select


def add_limit(sql: str, limit: int) -> str:
    """Append a LIMIT clause when one is not present.

    The previous implementation attempted to mutate the sqlglot AST by setting a
    Limit node directly. In some environments/versions this produced malformed SQL
    where the numeric literal was concatenated to the preceding identifier (e.g.
    "public.brand100 LIMIT;"), causing table names like "brand100". To keep
    behaviour robust across sqlglot versions, fall back to a simple and safe
    string-based append when a LIMIT token isn't already present.
    """

    original = sql or ""
    stripped = original.strip()
    if not stripped:
        return original

    # If any LIMIT appears, leave the statement unchanged
    lower = stripped.lower()
    if " limit " in f" {lower} ":
        return original

    # Preserve trailing semicolon if present
    has_semicolon = stripped.endswith(";")
    body = stripped[:-1] if has_semicolon else stripped
    augmented = f"{body} LIMIT {int(limit)}"
    return augmented + (";" if has_semicolon else "")
