from __future__ import annotations

import os

from typer.testing import CliRunner

import cli
from src.vast import service


runner = CliRunner()


def test_missing_env_ask_requires_ro_source(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_RO", raising=False)

    result = runner.invoke(cli.app, ["ask", "SELECT 1"], catch_exceptions=False)

    assert result.exit_code == 1
    output = result.stdout + result.stderr
    assert "DATABASE_URL_RO" in output
    assert "export DATABASE_URL_RO=" in output


def test_missing_env_demo_fails_helpfully(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL_RO", "postgresql://vast_ro:pass@localhost/db")
    monkeypatch.delenv("DATABASE_URL_RW", raising=False)

    result = runner.invoke(cli.app, ["demo:writes"], catch_exceptions=False)

    assert result.exit_code == 1
    output = result.stdout + result.stderr
    assert "DATABASE_URL_RW" in output
    assert "export DATABASE_URL_RW=" in output


def test_connection_info_prints(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL_RO", "postgresql://vast_ro:pass@localhost/db")
    monkeypatch.setenv("DATABASE_URL_RW", "postgresql://vast_rw:pass@localhost/db")
    monkeypatch.setenv("VAST_DIAG", "1")

    monkeypatch.setattr(cli, "get_engine", lambda readonly=True: object())
    monkeypatch.setattr(
        service,
        "connection_info",
        lambda engine: {
            "db": "pagila",
            "whoami": "vast_ro",
            "host": "127.0.0.1",
            "port": 5432,
        },
        raising=False,
    )
    monkeypatch.setattr(service, "probe_read", lambda engine: None, raising=False)
    monkeypatch.setattr(
        service,
        "plan_and_execute",
        lambda *args, **kwargs: {"sql": "SELECT 1", "execution": {"rows": []}},
        raising=False,
    )

    result = runner.invoke(cli.app, ["ask", "SELECT * FROM film"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Using db=pagila user=vast_ro host=127.0.0.1 port=5432 (mode=ro)" in result.stdout

    monkeypatch.delenv("VAST_DIAG", raising=False)


def test_privileges_block_demo(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL_RO", "postgresql://vast_ro:pass@localhost/db")
    monkeypatch.setenv("DATABASE_URL_RW", "postgresql://vast_rw:pass@localhost/db")

    monkeypatch.setattr(cli, "get_engine", lambda readonly=True: object())
    monkeypatch.setattr(
        service,
        "connection_info",
        lambda engine: {
            "db": "pagila",
            "whoami": "vast_rw",
            "host": "db.local",
            "port": 5432,
        },
        raising=False,
    )
    monkeypatch.setattr(service, "probe_read", lambda engine: None, raising=False)
    monkeypatch.setattr(
        service,
        "check_reference_privileges",
        lambda engine, tables: [{"table": "public.customer", "has_ref": False}],
        raising=False,
    )

    result = runner.invoke(cli.app, ["demo:writes"], catch_exceptions=False)

    assert result.exit_code == 1
    output = result.stdout + result.stderr
    assert "Insufficient privileges for vast_rw on public.customer (REFERENCES=false)." in output
    assert "python cli.py perms:bootstrap --schema public --yes" in output
