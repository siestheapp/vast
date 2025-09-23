from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.vast import agent
from src.vast.agent import plan_sql_with_retry, plan_sql
from src.vast.identifier_guard import IdentifierValidationError


def _make_identifier_error(message: str = "Unknown column") -> IdentifierValidationError:
    details = {
        "unknown_relations": ["public.film"],
        "unknown_columns": {"public.film": ["title"]},
        "strict_violation": False,
    }
    return IdentifierValidationError(details, message, "Use existing columns only")


def test_refresh_on_guard_failure_succeeds(monkeypatch):
    current_state = {
        "schema_summary": "public.film(title, film_id)",
        "schema_fingerprint": "old-fp",
    }
    refreshed_state = {
        "schema_summary": "public.film(name, film_id)",
        "schema_fingerprint": "new-fp",
    }

    refresh_calls: list[str] = []
    plan_fingerprints: list[str] = []

    def fake_get_schema_state(force_refresh: bool = False):
        return dict(current_state)

    def fake_refresh_schema_summary():
        refresh_calls.append("refresh")
        current_state.clear()
        current_state.update(refreshed_state)
        return dict(current_state)

    def fake_plan_sql(
        nl_request,
        allow_writes=False,
        force_refresh_schema=False,
        param_hints=None,
        extra_system_hint=None,
        schema_state=None,
    ):
        assert schema_state is not None, "schema_state should be provided"
        plan_fingerprints.append(schema_state["schema_fingerprint"])
        if schema_state["schema_fingerprint"] == "old-fp":
            return "SELECT title FROM public.film"
        return "SELECT name FROM public.film"

    def fake_validate(sql, params, allow_writes):
        if "title" in sql:
            raise _make_identifier_error()
        return sql

    monkeypatch.setattr(agent, "get_schema_state", fake_get_schema_state)
    monkeypatch.setattr(agent, "refresh_schema_summary", fake_refresh_schema_summary)
    monkeypatch.setattr(agent, "plan_sql", fake_plan_sql)
    monkeypatch.setattr("src.vast.agent._validate_with_guard", fake_validate, raising=False)

    sql = plan_sql_with_retry("find film name")

    assert sql == "SELECT name FROM public.film"
    assert refresh_calls == ["refresh"]
    assert plan_fingerprints == ["old-fp", "new-fp"], "planner should re-run with refreshed fingerprint"
    assert agent.get_schema_state()["schema_fingerprint"] == "new-fp"


def test_no_infinite_retry(monkeypatch):
    current_state = {
        "schema_summary": "public.film(name)",
        "schema_fingerprint": "fp-1",
    }

    refresh_calls: list[str] = []
    plan_invocations: list[str] = []

    def fake_get_schema_state(force_refresh: bool = False):
        return dict(current_state)

    def fake_refresh_schema_summary():
        refresh_calls.append("refresh")
        current_state["schema_fingerprint"] = "fp-2"
        return dict(current_state)

    def fake_plan_sql(
        nl_request,
        allow_writes=False,
        force_refresh_schema=False,
        param_hints=None,
        extra_system_hint=None,
        schema_state=None,
    ):
        plan_invocations.append(schema_state["schema_fingerprint"])
        return "SELECT ghost FROM public.film"

    def always_invalid(sql, params, allow_writes):
        raise _make_identifier_error("Unknown column ghost")

    monkeypatch.setattr(agent, "get_schema_state", fake_get_schema_state)
    monkeypatch.setattr(agent, "refresh_schema_summary", fake_refresh_schema_summary)
    monkeypatch.setattr(agent, "plan_sql", fake_plan_sql)
    monkeypatch.setattr("src.vast.agent._validate_with_guard", always_invalid, raising=False)

    with pytest.raises(IdentifierValidationError) as exc_info:
        plan_sql_with_retry("find missing column")

    assert refresh_calls == ["refresh"], "should refresh exactly once"
    assert plan_invocations == ["fp-1", "fp-2"], "planner should stop after single retry"
    assert "Schema was refreshed; identifier remains unknown." in (exc_info.value.hint or "")


def test_fingerprint_propagates_to_prompt(monkeypatch):
    captured = {}

    class FakeCompletions:
        def create(self, model, messages, temperature, max_tokens):
            captured["messages"] = messages
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="SELECT 1"))]
            )

    class FakeClient:
        def __init__(self, *_, **__):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(agent, "OpenAI", FakeClient)
    monkeypatch.setattr(agent.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(agent.settings, "openai_model", "gpt-test")

    state = {
        "schema_summary": "public.film(title)",
        "schema_fingerprint": "abc123",
    }

    sql = plan_sql(
        "list films",
        param_hints={"limit": 5},
        schema_state=state,
    )

    assert sql == "SELECT 1"
    message_content = captured["messages"][1]["content"]
    assert "schema_fingerprint=abc123" in message_content


def test_refresh_failure_does_not_corrupt_cache(monkeypatch):
    initial_state = {
        "schema_summary": "public.film(title)",
        "schema_fingerprint": "orig-fp",
    }
    state_holder = dict(initial_state)
    refresh_attempts: list[str] = []

    def fake_get_schema_state(force_refresh: bool = False):
        return dict(state_holder)

    def fake_refresh_schema_summary():
        refresh_attempts.append("refresh")
        raise RuntimeError("db unavailable")

    def fake_plan_sql(
        nl_request,
        allow_writes=False,
        force_refresh_schema=False,
        param_hints=None,
        extra_system_hint=None,
        schema_state=None,
    ):
        return "SELECT title FROM public.film"

    monkeypatch.setattr(agent, "get_schema_state", fake_get_schema_state)
    monkeypatch.setattr(agent, "refresh_schema_summary", fake_refresh_schema_summary)
    monkeypatch.setattr(agent, "plan_sql", fake_plan_sql)

    def invalid(sql, params, allow_writes):
        raise _make_identifier_error("Unknown column title")

    monkeypatch.setattr("src.vast.agent._validate_with_guard", invalid, raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        plan_sql_with_retry("find film title")

    assert "Failed to refresh schema summary" in str(exc_info.value)
    assert refresh_attempts == ["refresh"]
    assert state_holder == initial_state, "state must remain unchanged on refresh failure"


def test_deterministic_fingerprint(monkeypatch, tmp_path):
    cache_path = tmp_path / "schema_cache.json"
    monkeypatch.setattr(agent, "CACHE_PATH", cache_path)
    monkeypatch.setattr(agent, "_SCHEMA_STATE", {"schema_summary": None, "schema_fingerprint": None})

    tables = [
        {"table_schema": "public", "table_name": "film"},
        {"table_schema": "public", "table_name": "actor"},
    ]

    column_variants = {
        "first": {
            ("public", "film"): [
                {"column_name": "title", "data_type": "text", "is_nullable": "NO", "column_default": None},
                {"column_name": "film_id", "data_type": "integer", "is_nullable": "NO", "column_default": None},
            ],
            ("public", "actor"): [
                {"column_name": "actor_id", "data_type": "integer", "is_nullable": "NO", "column_default": None},
                {"column_name": "first_name", "data_type": "text", "is_nullable": "NO", "column_default": None},
            ],
        },
        "second": {
            ("public", "film"): [
                {"column_name": "film_id", "data_type": "integer", "is_nullable": "NO", "column_default": None},
                {"column_name": "title", "data_type": "text", "is_nullable": "NO", "column_default": None},
            ],
            ("public", "actor"): [
                {"column_name": "first_name", "data_type": "text", "is_nullable": "NO", "column_default": None},
                {"column_name": "actor_id", "data_type": "integer", "is_nullable": "NO", "column_default": None},
            ],
        },
    }

    order_state = {"key": "first"}

    def fake_list_tables():
        if order_state["key"] == "first":
            return list(tables)
        return list(reversed(tables))

    def fake_table_columns(schema: str, table: str):
        return list(column_variants[order_state["key"]][(schema, table)])

    monkeypatch.setattr(agent, "list_tables", fake_list_tables)
    monkeypatch.setattr(agent, "table_columns", fake_table_columns)
    monkeypatch.setattr(agent, "schema_summary", lambda *a, **k: "SUMMARY")

    first_fp = agent.refresh_schema_summary()["schema_fingerprint"]
    order_state["key"] = "second"
    second_fp = agent.refresh_schema_summary()["schema_fingerprint"]

    assert first_fp == second_fp
