import types

# ---- fakes ----
class FakeEngine:
    def current_database(self): return "pagila"
    def server_version(self):   return "16.10"

class FakeCache:
    def __init__(self, fresh, count=None):
        self._fresh = fresh
        self.table_count = count
        self.touched = False
    def is_fresh(self): return self._fresh
    def touch(self):    self.touched = True

class FakeDB:
    def __init__(self, value):
        self.calls = 0
        self.value = value
    def scalar(self, sql):
        assert "information_schema.tables" in sql
        self.calls += 1
        return self.value

def make_ctx(fresh, count=None, live_value=42):
    ctx = types.SimpleNamespace()
    ctx.engine = FakeEngine()
    ctx.schema_cache = FakeCache(fresh=fresh, count=count)
    ctx.db = FakeDB(value=live_value)
    # optional: audit spy
    ctx.audit = []
    return ctx

# ---- tests ----
from src.vast.facts import try_answer_with_facts

def test_compound_uses_cache_when_fresh():
    ctx = make_ctx(fresh=True, count=123)
    payload = try_answer_with_facts(ctx, "what database are you connected to and how many tables does it have")
    assert payload["database"] == "pagila"
    assert payload["server_version"] == "16.10"
    assert payload["table_count"] == 123
    assert payload["source"].startswith("facts")
    assert ctx.db.calls == 0  # no live SQL

def test_compound_runs_live_sql_when_stale_and_updates_cache():
    ctx = make_ctx(fresh=False, count=None, live_value=55)
    payload = try_answer_with_facts(ctx, "what database are you connected to and how many tables does it have")
    assert payload["table_count"] == 55
    assert payload["source"] == "facts+live-sql"
    assert ctx.db.calls == 1
    assert ctx.schema_cache.table_count == 55
    assert ctx.schema_cache.touched

def test_single_fact_paths_also_work():
    ctx = make_ctx(fresh=True, count=10)
    p1 = try_answer_with_facts(ctx, "which database are you connected to?")
    p2 = try_answer_with_facts(ctx, "how many tables are there?")
    assert p1["database"] == "pagila"
    assert p2["table_count"] == 10
