from typer.testing import CliRunner

import cli
import src.vast.service as service


def test_cli_run_hydrates_limit(monkeypatch):
    runner = CliRunner()
    captured = {}

    def fake_ensure(*args, **kwargs):
        return None

    def fake_load_summary(*args, **kwargs):
        return "summary"

    def fake_safe_execute(sql, params=None, allow_writes=False, force_write=False):
        captured["sql"] = sql
        captured["params"] = params
        return {"rows": [], "columns": [], "row_count": 0, "dry_run": False}

    # Ensure CLI uses the same service module object we're patching
    monkeypatch.setattr(cli, "service", service, raising=False)
    monkeypatch.setattr(service, "ensure_valid_identifiers", fake_ensure, raising=False)
    monkeypatch.setattr(service, "load_or_build_schema_summary", fake_load_summary, raising=False)
    monkeypatch.setattr(service, "safe_execute", fake_safe_execute, raising=False)

    result = runner.invoke(cli.app, [
        "run",
        "SELECT title FROM public.film ORDER BY film_id LIMIT :limit",
    ])

    assert result.exit_code == 0
    assert captured["sql"].strip().upper().endswith("LIMIT 10")
    assert captured["params"] == {"limit": 10}
