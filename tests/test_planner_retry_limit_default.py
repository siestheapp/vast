from types import SimpleNamespace

from src.vast.agent import plan_sql_with_retry, PlanResult


def test_planner_normalizes_limit_literal(monkeypatch):
    captured = {}

    def fake_plan_sql(*args, **kwargs):
        return PlanResult(sql="SELECT title FROM public.film LIMIT :limit")

    def fake_safe_execute(sql, params=None, allow_writes=False, force_write=False):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr("src.vast.agent.plan_sql", fake_plan_sql)
    monkeypatch.setattr("src.vast.agent.safe_execute", fake_safe_execute)
    monkeypatch.setattr("src.vast.agent.ensure_valid_identifiers", lambda *a, **k: None)
    monkeypatch.setattr("src.vast.agent.load_or_build_schema_summary", lambda *a, **k: "summary")
    monkeypatch.setattr("src.vast.agent.get_engine", lambda readonly=True: SimpleNamespace())

    result = plan_sql_with_retry("return the latest film", allow_writes=False, max_retries=1)

    assert isinstance(result, PlanResult)
    assert result.sql.strip().upper().endswith("LIMIT 10;")
    assert captured["sql"].strip().upper().endswith("LIMIT 10")
    assert captured["params"] == {"limit": 10}


def test_plan_and_execute_infers_limit_from_prompt(monkeypatch):
    captured = {}

    def fake_plan_sql_with_retry(*args, **kwargs):
        return PlanResult(sql="SELECT title FROM public.film ORDER BY film_id DESC LIMIT :limit")

    def fake_execute_sql(sql, params=None, allow_writes=False, force_write=False):
        captured["sql"] = sql
        captured["params"] = params
        return {"rows": [], "row_count": 0, "dry_run": False}

    monkeypatch.setattr("src.vast.service.plan_sql_with_retry", fake_plan_sql_with_retry)
    monkeypatch.setattr("src.vast.service.execute_sql", fake_execute_sql)
    monkeypatch.setattr("src.vast.service.load_or_build_schema_summary", lambda *a, **k: "summary")
    monkeypatch.setattr("src.vast.service.get_engine", lambda readonly=True: SimpleNamespace())
    monkeypatch.setattr("src.vast.service.ensure_valid_identifiers", lambda *a, **k: None)

    from src.vast import service

    result = service.plan_and_execute("top 5 films", allow_writes=False, max_retries=1)

    assert result["sql"].strip().upper().endswith("LIMIT :LIMIT;")
    assert captured["params"]["limit"] == 5
