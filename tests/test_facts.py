import types

# ---- fakes ----
class FakeEngine:
    def current_database(self):
        return "pagila"

    def server_version(self):
        return "16.10"

class FakeCache:
    def __init__(self, fresh, count=None):
        self._fresh = fresh
        self.table_count = count
        self.touched = False

    def is_fresh(self):
        return self._fresh

    def touch(self):
        self.touched = True

class FakeDB:
    def __init__(self, value):
        self.calls = 0
        self.value = value
        self.last_sql = None

    def scalar(self, sql):
        assert "information_schema.tables" in sql
        self.calls += 1
        self.last_sql = sql
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
    assert "table_type = 'BASE TABLE'" in ctx.db.last_sql
    assert "pg_toast" in ctx.db.last_sql
    assert ctx.schema_cache.table_count == 55
    assert ctx.schema_cache.touched

def test_single_fact_paths_also_work():
    ctx = make_ctx(fresh=True, count=10)
    p1 = try_answer_with_facts(ctx, "which database are you connected to?")
    p2 = try_answer_with_facts(ctx, "how many tables are there?")
    assert p1["database"] == "pagila"
    assert p2["table_count"] == 10


class FakeBeginConn:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        return self.engine

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeSizeEngine:
    def __init__(self, size_bytes, size_pretty):
        self.size_bytes = size_bytes
        self.size_pretty = size_pretty
        self.last_sql = None

    # satisfy identity fallback
    def current_database(self):
        return "pagila"

    def server_version(self):
        return "16.10"

    def begin(self):
        return FakeBeginConn(self)

    def execute(self, sql):
        self.last_sql = sql

        class Result:
            def mappings(_self):
                class Map:
                    def first(__self):
                        return {
                            "size_bytes": self.size_bytes,
                            "size_pretty": self.size_pretty,
                        }

                return Map()

        return Result()


def test_db_size_fact_pronoun_only(monkeypatch):
    monkeypatch.setattr(
        "src.vast.catalog_pg.database_size",
        lambda: {"size_bytes": 123456789, "size_pretty": "118 MB"},
    )
    ctx = types.SimpleNamespace()
    ctx.engine = FakeSizeEngine(size_bytes=123456789, size_pretty="118 MB")
    ctx.schema_cache = FakeCache(fresh=False, count=5)
    ctx.db = FakeDB(value=5)
    ctx.audit = []

    answer = try_answer_with_facts(ctx, "how large is it")

    assert answer["database_size_bytes"] == 123456789
    assert answer["database_size_pretty"] == "118 MB"
    assert answer["source"] == "facts+live-sql"
    assert "118 MB" in answer.content
    assert "123456789" in answer.content
    log = answer.log_entries[0].metadata
    assert log["type"] == "FACT"
    assert log["fact_key"] == "db_size"
    assert "pg_database_size" in log["sql"]


def test_combined_identity_and_size(monkeypatch):
    monkeypatch.setattr(
        "src.vast.catalog_pg.database_size",
        lambda: {"size_bytes": 22334455, "size_pretty": "21 MB"},
    )
    ctx = types.SimpleNamespace()
    ctx.engine = FakeSizeEngine(size_bytes=22334455, size_pretty="21 MB")
    ctx.schema_cache = FakeCache(fresh=True, count=5)
    ctx.db = FakeDB(value=5)
    ctx.audit = []

    answer = try_answer_with_facts(ctx, "what database are you connected to and how large is it")

    assert answer["database"] == "pagila"
    assert answer["database_size_bytes"] == 22334455
    assert "21 MB" in answer.content

    fact_keys = {log.metadata.get("fact_key") for log in answer.log_entries}
    assert {"db_identity", "db_size"}.issubset(fact_keys)


def test_fact_identity_masks_host_port_by_default(monkeypatch):
    monkeypatch.delenv("VAST_MASK_HOST_PORT", raising=False)
    ctx = make_ctx(fresh=True, count=10)
    ctx.engine.host = "172.17.0.2"
    ctx.engine.port = 5432

    answer = try_answer_with_facts(ctx, "which database are you connected to?")

    assert "•••:•••" in answer.content
    assert "172.17.0.2:5432" not in answer.content


def test_fact_identity_unmasked_when_env_false(monkeypatch):
    monkeypatch.setenv("VAST_MASK_HOST_PORT", "false")
    ctx = make_ctx(fresh=True, count=10)
    ctx.engine.host = "172.17.0.2"
    ctx.engine.port = 5432

    answer = try_answer_with_facts(ctx, "which database are you connected to?")

    assert "172.17.0.2:5432" in answer.content
    assert "•••:•••" not in answer.content
