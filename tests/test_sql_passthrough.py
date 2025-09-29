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
        return {
            "rows": [{"title": "A Film"}],
            "row_count": 1,
            "meta": {"engine_ms": 5, "exec_ms": 7},
        }

    monkeypatch.setattr(service, "ensure_valid_identifiers", fake_ensure)
    monkeypatch.setattr(service, "execute_sql", fake_execute_sql)

    result = service.plan_and_execute(sql)
    assert result["sql"] == sql
    assert result.get("passthrough") is True
    assert result["intent"] == "read"
    assert result["result"]["columns"] == ["title"]
    assert result["result"]["row_count"] == 1
    assert result["metrics"]["exec_ms"] == 7
    assert calls == {"ensure": 1, "execute": 1}


def test_planner_for_nl_text(monkeypatch):
    q = "show me a film title"
    planner_called = {"count": 0}

    def fake_plan(*args, **kwargs):
        planner_called["count"] += 1
        return PlanResult(sql="SELECT title FROM public.film LIMIT 1")

    def fake_execute_sql(sql, **kwargs):
        return {
            "rows": [{"title": "A Film", "url": "http://example.com"}],
            "row_count": 1,
            "meta": {"engine_ms": 4, "exec_ms": 6},
        }

    monkeypatch.setattr(service, "plan_sql_with_retry", fake_plan)
    monkeypatch.setattr(service, "execute_sql", fake_execute_sql)
    monkeypatch.setattr(service, "resolver_shortcut", lambda *_args, **_kwargs: (None, None))

    res = service.plan_and_execute(q)
    assert res["intent"] == "read"
    assert res["result"]["columns"] == ["title", "url"]
    assert res["metrics"]["exec_ms"] == 6
    assert planner_called["count"] == 1


def test_plan_and_execute_write_intent(monkeypatch):
    q = "change a film title"

    def fake_plan(*args, **kwargs):
        return PlanResult(sql="UPDATE public.film SET title = 'New' WHERE film_id = 1;")

    executed = {"called": False, "allow_writes": None}

    def fake_execute_sql(sql, allow_writes=False, force_write=False, **_):
        executed["called"] = True
        executed["allow_writes"] = allow_writes
        return {"rows": [], "meta": {"engine_ms": 5, "exec_ms": 5}}

    monkeypatch.setattr(service, "plan_sql_with_retry", fake_plan)
    monkeypatch.setattr(service, "execute_sql", fake_execute_sql)
    monkeypatch.setattr(service, "resolver_shortcut", lambda *_args, **_kwargs: (None, None))

    result = service.plan_and_execute(q, allow_writes=True, force_write=True)

    assert executed["called"] is True
    assert executed["allow_writes"] is True
    assert result["intent"] == "write"


def test_plan_and_execute_read_payload_details(monkeypatch):
    sql = "select url, seen_at from public.product_url limit 2"

    monkeypatch.setattr(service, "plan_sql_with_retry", _raise_planner)
    monkeypatch.setattr(service, "plan_sql", _raise_planner)

    def fake_ensure(sql, **kwargs):
        return None

    def fake_execute_sql(sql, **kwargs):
        return {
            "rows": [
                {"url": "http://example.com/one", "seen_at": "2024-01-01T00:00:00"},
                {"url": "http://example.com/two", "seen_at": "2024-01-02T00:00:00"},
            ],
            "row_count": 2,
            "meta": {"engine_ms": 3, "exec_ms": 5},
        }

    monkeypatch.setattr(service, "ensure_valid_identifiers", fake_ensure)
    monkeypatch.setattr(service, "execute_sql", fake_execute_sql)

    result = service.plan_and_execute(sql)

    assert result["intent"] == "read"
    assert result["result"]["columns"] == ["url", "seen_at"]
    assert len(result["result"]["rows"]) == 2
    assert result["metrics"] == {"engine_ms": 3, "exec_ms": 5}
    assert result["linkable_columns"] == ["url"]
    assert any("seen_at" in note for note in result.get("notes", []))
