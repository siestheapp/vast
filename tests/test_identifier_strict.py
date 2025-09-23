import pytest

from src.vast import service
from src.vast.identifier_guard import IdentifierValidationError


def test_strict_unknown_column_no_execute(monkeypatch):
    called = False

    def fake_safe_execute(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("src.vast.service.safe_execute", fake_safe_execute)

    with pytest.raises(IdentifierValidationError) as exc:
        service.execute_sql("SELECT not_a_col FROM public.film LIMIT 1")

    assert not called
    details = exc.value.details
    assert details["strict_violation"] is True
    assert details["unknown_columns"].get("public.film") == ["not_a_col"]


def test_strict_unknown_table_no_execute(monkeypatch):
    called = False

    def fake_safe_execute(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("src.vast.service.safe_execute", fake_safe_execute)

    with pytest.raises(IdentifierValidationError) as exc:
        service.execute_sql("SELECT title FROM public.films LIMIT 1")

    assert not called
    details = exc.value.details
    assert details["strict_violation"] is True
    assert details["unknown_relations"] == ["public.films"]
