import pytest

from src.vast import service
from src.vast.identifier_guard import IdentifierValidationError
from src.vast.agent import PlanResult


def _raise_planner(*args, **kwargs):
    raise AssertionError("planner should not be called")


def test_rejects_unknown_col_passthrough(monkeypatch):
    sql = "select not_a_col from public.film limit 1"

    monkeypatch.setattr(service, "plan_sql_with_retry", _raise_planner)
    monkeypatch.setattr(service, "plan_sql", _raise_planner)

    def fake_ensure(sql, **kwargs):
        raise IdentifierValidationError({"unknown_columns": {"public.film": ["not_a_col"]}}, "Unknown column")

    monkeypatch.setattr(service, "ensure_valid_identifiers", fake_ensure)

    with pytest.raises(IdentifierValidationError):
        service.plan_and_execute(sql)


def test_accepts_valid_sql_passthrough(monkeypatch):
    sql = "select title from public.film limit 1"
    calls = {"ensure": 0, "execute": 0}

    monkeypatch.setattr(service, "plan_sql_with_retry", _raise_planner)
    monkeypatch.setattr(service, "plan_sql", _raise_planner)

    def fake_ensure(sql, **kwargs):
        calls["ensure"] += 1

    def fake_execute_sql(sql, **kwargs):
        calls["execute"] += 1
        return {"rows": []}

    monkeypatch.setattr(service, "ensure_valid_identifiers", fake_ensure)
    monkeypatch.setattr(service, "execute_sql", fake_execute_sql)

    result = service.plan_and_execute(sql)
    assert result["sql"] == sql
    assert result.get("passthrough") is True
    assert calls == {"ensure": 1, "execute": 1}


def test_planner_for_nl_text(monkeypatch):
    q = "show me a film title"
    planner_called = {"count": 0}

    def fake_plan(*args, **kwargs):
        planner_called["count"] += 1
        return PlanResult(sql="SELECT title FROM public.film LIMIT 1")

    def fake_execute_sql(sql, **kwargs):
        return {"rows": []}

    monkeypatch.setattr(service, "plan_sql_with_retry", fake_plan)
    monkeypatch.setattr(service, "execute_sql", fake_execute_sql)

    service.plan_and_execute(q)
    assert planner_called["count"] == 1
