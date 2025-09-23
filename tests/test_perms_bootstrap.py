from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

import cli
from src.vast import perms
from src.vast.config import settings


class RecordingConnection:
    def __init__(self, sink):
        self._sink = sink

    def execute(self, clause):
        sql = getattr(clause, "text", None)
        if sql is None:
            sql = str(clause)
        self._sink.append(sql)


class RecordingTransaction:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return RecordingConnection(self._sink)

    def __exit__(self, exc_type, exc, tb):
        return False


class RecordingEngine:
    def __init__(self):
        self.statements: list[str] = []

    def begin(self):
        return RecordingTransaction(self.statements)

    def dispose(self):
        pass


def test_bootstrap_perms_executes_expected_statements():
    engine = RecordingEngine()
    result = perms.bootstrap_perms(engine, "public", "vast_ro", "vast_rw")

    expected = [
        'GRANT USAGE ON SCHEMA "public" TO "vast_ro", "vast_rw"',
        'GRANT SELECT ON ALL TABLES IN SCHEMA "public" TO "vast_ro"',
        'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "public" TO "vast_ro"',
        'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "public" TO "vast_rw"',
        'GRANT REFERENCES ON ALL TABLES IN SCHEMA "public" TO "vast_rw"',
        'ALTER DEFAULT PRIVILEGES FOR ROLE "vast_rw" IN SCHEMA "public" GRANT SELECT ON TABLES TO "vast_ro"',
        'ALTER DEFAULT PRIVILEGES FOR ROLE "vast_rw" IN SCHEMA "public" GRANT USAGE, SELECT ON SEQUENCES TO "vast_ro"',
    ]

    assert result["statements"] == expected
    assert engine.statements == expected


def test_cli_perms_bootstrap_invokes_helper(monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("DATABASE_URL_OWNER", "postgresql://owner:pass@localhost/db")
    monkeypatch.setenv("VAST_WRITE_ROLE", "vast_rw")

    call_args: dict[str, object] = {}

    class DummyEngine:
        def dispose(self):
            call_args["disposed"] = True

    def fake_owner_engine(url: str):
        call_args["owner_url"] = url
        return DummyEngine()

    def fake_bootstrap(engine, schema, ro_role, rw_role):
        call_args["engine"] = engine
        call_args["schema"] = schema
        call_args["ro_role"] = ro_role
        call_args["rw_role"] = rw_role
        return {
            "schema": schema,
            "read_role": ro_role,
            "write_role": rw_role,
            "statements": ["ok"],
        }

    monkeypatch.setattr("cli._owner_engine", fake_owner_engine)
    monkeypatch.setattr("cli.bootstrap_perms", fake_bootstrap)

    result = runner.invoke(cli.app, ["perms:bootstrap", "--schema", "public", "--yes"])

    assert result.exit_code == 0
    assert call_args["owner_url"] == "postgresql://owner:pass@localhost/db"
    assert call_args["schema"] == "public"
    assert call_args["ro_role"] == settings.read_role
    assert call_args["rw_role"] == "vast_rw"
    assert call_args.get("disposed") is True
    assert "Permissions bootstrapped successfully" in result.stdout


def test_cli_perms_bootstrap_smoke(monkeypatch):
    owner_url = os.getenv("DATABASE_URL_OWNER")
    if not owner_url:
        pytest.skip("DATABASE_URL_OWNER not configured")

    runner = CliRunner()

    class DummyEngine:
        def dispose(self):
            pass

        def begin(self):
            return RecordingTransaction([])

    monkeypatch.setattr("cli._owner_engine", lambda url: DummyEngine())
    monkeypatch.setattr("cli.bootstrap_perms", lambda *args, **kwargs: {
        "schema": kwargs.get("schema", "public"),
        "read_role": kwargs.get("ro_role", settings.read_role),
        "write_role": kwargs.get("rw_role", "vast_rw"),
        "statements": ["noop"],
    })

    result = runner.invoke(cli.app, ["perms:bootstrap", "--schema", "public", "--yes"])
    assert result.exit_code == 0
