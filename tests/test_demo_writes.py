import pytest
from typer.testing import CliRunner

from src.vast.actions import build_review_feature_migration
from src.vast.sql_params import stmt_kind
from src.vast import service
from src.vast.config import settings
from src.vast.db import get_engine
from sqlalchemy import text

import cli


def test_stmt_kind_classification():
    assert stmt_kind("  create table x (id int)") == "DDL"
    assert stmt_kind("insert into t values (1)") == "DML"
    assert stmt_kind(" select * from t") == "SELECT"
    assert stmt_kind("with cte as (select 1) select * from cte") == "SELECT"
    assert stmt_kind("vacuum") == "OTHER"


class DummyConnection:
    def __init__(self, engine):
        self.engine = engine
        self.closed = False

    def execute(self, clause, params=None):
        sql_text = getattr(clause, "text", str(clause))
        if params:
            sql_text = f"{sql_text} -- params={params}"
        self.engine.statements.append(sql_text)
        self.engine.call_count += 1
        if self.engine.fail_on and self.engine.call_count == self.engine.fail_on:
            raise RuntimeError("boom")
        return DummyResult()

    def exec_driver_sql(self, sql, params=None):
        if params:
            sql_text = f"{sql} -- params={params}"
        else:
            sql_text = sql
        self.engine.statements.append(sql_text)
        self.engine.call_count += 1
        if self.engine.fail_on and self.engine.call_count == self.engine.fail_on:
            raise RuntimeError("boom")
        if "current_user" in sql:
            return DummyResult([("demo_user", "demo_session")])
        if "pg_get_serial_sequence" in sql:
            return DummyResult([("public.review_review_id_seq",)])
        return DummyResult()

    def begin(self):
        self.engine.events.append("BEGIN")
        return DummyTransaction(self.engine)

    def close(self):
        self.closed = True
        self.engine.events.append("CLOSE")


class DummyTransaction:
    def __init__(self, engine):
        self.engine = engine

    def rollback(self):
        self.engine.events.append("ROLLBACK")

    def commit(self):
        self.engine.events.append("COMMIT")


class DummyContextTransaction:
    def __init__(self, engine):
        self.engine = engine
        self.connection = DummyConnection(engine)

    def __enter__(self):
        self.engine.events.append("BEGIN")
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.engine.events.append("ROLLBACK")
        else:
            self.engine.events.append("COMMIT")
        return False


class DummyEngine:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.statements = []
        self.events = []
        self.call_count = 0


class DummyResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def first(self):
        return self.fetchone()

    def scalar(self):
        row = self.fetchone()
        if row and len(row) >= 1:
            return row[0]
        return None

    def connect(self):
        return DummyConnection(self)

    def begin(self):
        return DummyContextTransaction(self)


@pytest.fixture
def demo_statements():
    return build_review_feature_migration()


@pytest.fixture
def patch_services(monkeypatch):
    rw_engine = DummyEngine()
    ro_engine = object()

    def fake_get_engine(readonly=True):
        return ro_engine if readonly else rw_engine

    monkeypatch.setattr(service, "get_engine", fake_get_engine)

    return {
        "rw_engine": rw_engine,
        "ro_engine": ro_engine,
    }


def test_preflight_runs_ddl_and_rolls_back(monkeypatch, patch_services, demo_statements):
    ctx = patch_services
    notes = service.preflight_statements(demo_statements)

    assert ctx["rw_engine"].events == ["BEGIN", "ROLLBACK", "CLOSE"]
    assert any(note.startswith("OK DDL CREATE") for note in notes)
    assert any(note.startswith("OK EXPLAIN DML") for note in notes)
    executed = ctx["rw_engine"].statements
    assert any(stmt.startswith("CREATE TABLE") for stmt in executed)
    assert any(stmt.startswith("EXPLAIN") for stmt in executed)


def test_preflight_idempotent(monkeypatch, patch_services, demo_statements):
    ctx = patch_services
    notes_first = service.preflight_statements(demo_statements)
    notes_second = service.preflight_statements(demo_statements)

    assert notes_first and notes_second
    assert ctx["rw_engine"].events == [
        "BEGIN",
        "ROLLBACK",
        "CLOSE",
        "BEGIN",
        "ROLLBACK",
        "CLOSE",
    ]


def test_apply_statements_commits(monkeypatch, patch_services, demo_statements):
    ctx = patch_services
    service.apply_statements(demo_statements)

    assert ctx["rw_engine"].events == ["BEGIN", "COMMIT", "CLOSE"]
    executed = ctx["rw_engine"].statements
    assert executed[0].startswith("SELECT current_user")
    expected = [stmt.strip() for stmt in demo_statements]
    assert [stmt.strip() for stmt in executed[1:1 + len(demo_statements)]] == expected
    assert any(stmt.startswith("GRANT SELECT ON TABLE public.review") for stmt in executed)
    assert any(stmt.startswith("SELECT pg_get_serial_sequence") for stmt in executed)
    assert any(stmt.startswith("GRANT USAGE, SELECT ON SEQUENCE public.review_review_id_seq") for stmt in executed)


def test_apply_statements_idempotent(monkeypatch, patch_services, demo_statements):
    ctx = patch_services
    service.apply_statements(demo_statements)
    first_events = list(ctx["rw_engine"].events)
    first_statements = list(ctx["rw_engine"].statements)

    service.apply_statements(demo_statements)

    assert ctx["rw_engine"].events == first_events + ["BEGIN", "COMMIT", "CLOSE"]
    assert ctx["rw_engine"].statements == first_statements + first_statements


def test_apply_statements_rolls_back_on_error(monkeypatch, demo_statements):
    failing_engine = DummyEngine(fail_on=3)
    ro_engine = object()

    def fake_get_engine(readonly=True):
        return ro_engine if readonly else failing_engine

    monkeypatch.setattr(service, "get_engine", fake_get_engine)
    with pytest.raises(RuntimeError):
        service.apply_statements(demo_statements)

    assert failing_engine.events == ["BEGIN", "ROLLBACK", "CLOSE"]
    assert len(failing_engine.statements) == 3


@pytest.mark.skipif(not settings.database_url_rw, reason="DATABASE_URL not configured")
def test_demo_writes_integration():
    import os
    from src.vast import db

    original_rw = str(settings.database_url_rw)
    desired_rw = original_rw.replace("vast_ro", "vast_rw") if original_rw else original_rw

    original_database_url = str(settings.database_url_ro)
    os.environ["DATABASE_URL"] = original_database_url

    if desired_rw and desired_rw != original_rw:
        os.environ["DATABASE_URL_RW"] = desired_rw
        settings.database_url_rw = desired_rw
        db._engine_rw = None

    engine = get_engine(readonly=False)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS public.review CASCADE"))

    runner = CliRunner()
    result = runner.invoke(cli.app, ["demo:writes"])
    assert result.exit_code == 0, result.output

    with engine.begin() as conn:
        count = conn.execute(text("SELECT count(*) FROM public.review"))
        total = count.scalar()
    assert total and total > 0

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS public.review CASCADE"))

    if desired_rw and desired_rw != original_rw:
        settings.database_url_rw = original_rw
        os.environ["DATABASE_URL_RW"] = original_rw
        db._engine_rw = None
