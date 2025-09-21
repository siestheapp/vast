from __future__ import annotations

from enum import Enum, auto

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlglot import parse
from sqlglot import exp
from sqlglot.errors import ParseError

from .config import settings


class StatementType(Enum):
    READ = auto()
    WRITE = auto()
    DDL = auto()


_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = settings.database_url.strip()
        if url.startswith("DATABASE_URL="):
            url = url.split("=", 1)[1]
        if not url:
            raise RuntimeError("DATABASE_URL missing. Set it in .env")
        _engine = create_engine(url, pool_pre_ping=True)
    return _engine


def _unwrap_with(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.With):
        node = node.this
    return node


def _command_name(command: exp.Command) -> str:
    name = command.name or ""
    return name.upper()


def _classify_statement(expr: exp.Expression) -> StatementType:
    expr = _unwrap_with(expr)

    ddl_nodes = (exp.Create, exp.Alter, exp.Drop, exp.Truncate)
    if isinstance(expr, ddl_nodes) or any(expr.find(node) for node in ddl_nodes):
        return StatementType.DDL

    if isinstance(expr, exp.Command):
        command = _command_name(expr)
        if command in {"EXPLAIN", "SHOW"}:
            return StatementType.READ
        if command in {"ANALYZE", "VACUUM"}:
            return StatementType.WRITE
        return StatementType.DDL

    if isinstance(expr, (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Copy, exp.LoadData)):
        return StatementType.WRITE

    if isinstance(expr, exp.Select):
        return StatementType.READ

    # Fall back to scanning children
    if expr.find(exp.Insert) or expr.find(exp.Update) or expr.find(exp.Delete) or expr.find(exp.Merge):
        return StatementType.WRITE

    if expr.find(exp.Create) or expr.find(exp.Drop) or expr.find(exp.Alter) or expr.find(exp.Truncate):
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


def safe_execute(
    sql: str,
    params: dict | None = None,
    allow_writes: bool = False,
    force_write: bool = False,
):
    """Analyse and execute SQL with strict guardrails."""

    stmt_type, normalized_sql = analyse_sql(sql)

    if stmt_type is StatementType.DDL:
        raise ValueError("DDL statements are blocked. Use a migration workflow.")

    if stmt_type is StatementType.WRITE:
        if not allow_writes:
            raise ValueError("Write queries are disabled. Use --write to permit writes.")
        if not force_write:
            return [{"_notice": "DRY RUN — not executed", "_sql": normalized_sql, "_params": params or {}}]

    with get_engine().begin() as conn:
        return list(conn.execute(text(normalized_sql), params or {}))
