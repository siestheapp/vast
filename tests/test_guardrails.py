import pytest
from src.vast.db import analyse_sql, StatementType, safe_execute
from src.vast.config import settings

def test_single_statement_enforced():
    with pytest.raises(ValueError):
        analyse_sql("SELECT 1; SELECT 2")

def test_classifies_with_update_cte():
    t, _ = analyse_sql("WITH x AS (SELECT 1) UPDATE films SET title = title")
    assert t is StatementType.WRITE

def test_blocks_ddl():
    with pytest.raises(ValueError):
        safe_execute("DROP TABLE actor")

def test_write_requires_flag():
    with pytest.raises(ValueError):
        safe_execute("UPDATE actor SET first_name = 'test'")

def test_dry_run_when_not_forced():
    out = safe_execute("UPDATE actor SET first_name='test' WHERE actor_id < 10", allow_writes=True, force_write=False)
    assert out and out[0]["_notice"].startswith("DRY RUN")

def test_row_estimate_gate(monkeypatch):
    # Simulate a huge estimate by monkeypatching _estimate_write_rows
    import src.vast.db as db
    monkeypatch.setattr(db, "_estimate_write_rows", lambda *_: settings.max_write_rows + 1)
    with pytest.raises(ValueError):
        safe_execute("UPDATE actor SET first_name='test'", allow_writes=True, force_write=True)
