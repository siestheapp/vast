from __future__ import annotations

from enum import Enum, auto
from typing import Optional, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlglot import parse
from sqlglot import exp
from sqlglot.errors import ParseError

from .config import settings
from .audit import audit_event


class StatementType(Enum):
    READ = auto()
    WRITE = auto()
    DDL = auto()


_engine_ro: Engine | None = None
_engine_rw: Engine | None = None


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


def get_engine(readonly: bool = True) -> Engine:
    global _engine_ro, _engine_rw
    if readonly:
        if _engine_ro is None:
            _engine_ro = _mk_engine(str(settings.database_url_ro))
        return _engine_ro
    else:
        if _engine_rw is None:
            _engine_rw = _mk_engine(str(settings.database_url_rw))
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

    ddl_nodes = (exp.Create, exp.AlterTable, exp.Drop, exp.TruncateTable)
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

    if expr.find(exp.Create) or expr.find(exp.Drop) or expr.find(exp.AlterTable) or expr.find(exp.TruncateTable):
        return StatementType.DDL

    # Fail closed for unknown statements.
    return StatementType.DDL


def analyse_sql(sql: str) -> tuple[StatementType, str]:
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
    return classification, normalized


def _estimate_write_rows(sql: str, params: dict | None) -> Optional[int]:
    """
    For UPDATE/DELETE/MERGE: try EXPLAIN (FORMAT JSON) to estimate affected rows.
    Returns None if estimate unavailable.
    """
    explain_sql = f"EXPLAIN (FORMAT JSON) {sql}"
    with get_engine(readonly=True).begin() as conn:  # EXPLAIN is read-only
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
    stmt_type, normalized_sql = analyse_sql(sql)

    if stmt_type is StatementType.DDL:
        raise ValueError("DDL statements are blocked. Use a migration workflow.")

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
                return [{"_notice": "DRY RUN — not executed", "_sql": normalized_sql, "_params": params or {}, "_estimated_rows": est}]

            # Execute with RW engine only when truly writing
            with get_engine(readonly=False).begin() as conn:
                result = list(conn.execute(text(normalized_sql), params or {}))
        else:
            # READ path — strictly RO engine
            with get_engine(readonly=True).begin() as conn:
                result = list(conn.execute(text(normalized_sql), params or {}))

        # after success
        audit_event({
            "phase": "post",
            "success": True,
            "rows": len(result) if isinstance(result, list) else None,
        })
        return result
    except Exception as exc:
        audit_event({
            "phase": "post",
            "success": False,
            "error": str(exc),
        })
        raise
