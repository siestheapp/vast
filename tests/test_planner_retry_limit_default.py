from types import SimpleNamespace

from src.vast.agent import plan_sql_with_retry


def test_planner_normalizes_limit_literal(monkeypatch):
    captured = {}

    def fake_plan_sql(*args, **kwargs):
        return "SELECT title FROM public.film LIMIT :limit"

    def fake_safe_execute(sql, params=None, allow_writes=False, force_write=False):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr("src.vast.agent.plan_sql", fake_plan_sql)
    monkeypatch.setattr("src.vast.agent.safe_execute", fake_safe_execute)
    monkeypatch.setattr("src.vast.agent.ensure_valid_identifiers", lambda *a, **k: None)
    monkeypatch.setattr("src.vast.agent.load_or_build_schema_summary", lambda *a, **k: "summary")
    monkeypatch.setattr("src.vast.agent.get_engine", lambda readonly=True: SimpleNamespace())

    sql = plan_sql_with_retry("return the latest film", allow_writes=False, max_retries=1)

    assert sql.strip().upper().endswith("LIMIT 1")
    assert captured["sql"].strip().upper().endswith("LIMIT 1")
    assert captured["params"] == {"limit": 1}
