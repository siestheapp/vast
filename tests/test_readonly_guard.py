import pytest
from vast.service import plan_and_execute, execute_sql


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET x=1",
    "DELETE FROM t",
    "ALTER TABLE t ADD COLUMN x int",
    "DROP TABLE t",
    "TRUNCATE t",
    "CALL some_proc()",
    "DO $$ BEGIN RAISE NOTICE 'x'; END $$;",
])
def test_mutations_blocked(sql):
    # Test via execute_sql (lower-level) since plan_and_execute doesn't take sql directly
    with pytest.raises(ValueError) as exc_info:
        execute_sql(
            sql=sql,
            params=None,
            allow_writes=False,           # explicitly RO
            force_write=False
        )
    # should NOT execute; expect a read-only error
    err = str(exc_info.value).lower()
    assert "read-only" in err or "expected a select" in err


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "SELECT * FROM information_schema.tables",
    "WITH cte AS (SELECT 1) SELECT * FROM cte",
])
def test_readonly_allowed(sql):
    # Test that read-only operations are allowed
    out = execute_sql(
        sql=sql,
        params=None,
        allow_writes=False,
        force_write=False
    )
    # should execute successfully
    assert out.get("success", True)
    assert "error" not in out or not out["error"]


@pytest.mark.parametrize("sql", [
    "EXPLAIN SELECT 1",
    "SHOW version()",
])
def test_other_operations_blocked(sql):
    # Test that OTHER operations (EXPLAIN, SHOW) are blocked in read-only mode
    with pytest.raises(ValueError) as exc_info:
        execute_sql(
            sql=sql,
            params=None,
            allow_writes=False,
            force_write=False
        )
    # should NOT execute; expect a read-only error
    err = str(exc_info.value).lower()
    assert "read-only" in err or "expected a select" in err


def test_plan_and_execute_readonly_nl():
    # Test that plan_and_execute with natural language respects allow_writes=False
    out = plan_and_execute(
        nl_request="show me all tables in the database",
        params=None,
        allow_writes=False,
        force_write=False,
        retry=False
    )
    # should execute successfully for read-only queries
    assert out.get("execution", {}).get("success", True)
    assert "error" not in out.get("execution", {}) or not out["execution"]["error"]


def test_plan_and_execute_mutation_nl():
    # Test that plan_and_execute blocks mutations even when requested in natural language
    with pytest.raises(ValueError) as exc_info:
        plan_and_execute(
            nl_request="create a new table called test_table with id and name columns",
            params=None,
            allow_writes=False,
            force_write=False,
            retry=False
        )
    # should fail with read-only error
    err = str(exc_info.value).lower()
    assert "read-only" in err or "expected a select" in err
