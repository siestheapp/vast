import os

import pytest

from src.vast.db import get_engine
from src.vast.identifier_guard import (
    load_schema_cache,
    validate_identifiers,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL environment variable not set",
)


@pytest.fixture(scope="module")
def schema_cache():
    engine = get_engine(readonly=True)
    cache, _ = load_schema_cache(engine, force_refresh=True)
    return cache


@pytest.fixture(scope="module")
def engine():
    return get_engine(readonly=True)


def test_validate_identifiers_valid(engine, schema_cache):
    sql = "SELECT title FROM public.film LIMIT 1"
    ok, details = validate_identifiers(sql, engine, schema_cache)
    assert ok is True
    assert details["unknown_relations"] == []
    assert details["unknown_columns"] == {}
    assert details["explain_failed"] is False
    assert details["strict_violation"] is False


def test_validate_identifiers_unknown_table(engine, schema_cache):
    sql = "SELECT * FROM public.films LIMIT 1"
    ok, details = validate_identifiers(sql, engine, schema_cache)
    assert ok is False
    assert details["unknown_relations"] == ["public.films"]
    assert details["explain_failed"] is True
    assert details["strict_violation"] is True


def test_validate_identifiers_unknown_column(engine, schema_cache):
    sql = "SELECT not_a_col FROM public.film LIMIT 1"
    ok, details = validate_identifiers(sql, engine, schema_cache)
    assert ok is False
    assert details["unknown_relations"] == []
    assert details["unknown_columns"].get("public.film") == ["not_a_col"]
    assert details["explain_failed"] is True
    assert details["strict_violation"] is True


def test_validate_identifiers_missing_limit_param(engine, schema_cache):
    sql = "SELECT title FROM public.film LIMIT :limit"
    ok, details = validate_identifiers(sql, engine, schema_cache)
    assert ok is True
    assert details["unknown_relations"] == []
    assert details["unknown_columns"] == {}
    assert details["explain_failed"] is False
    assert details["strict_violation"] is False
