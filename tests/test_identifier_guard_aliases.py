import pytest

from src.vast.config import settings
from src.vast.identifier_guard import IdentifierValidationError, ensure_valid_identifiers

pytestmark = pytest.mark.skipif(
    not settings.database_url_ro,
    reason="DATABASE_URL not configured",
)


def test_alias_columns_valid():
    sql = "SELECT f.title FROM public.film AS f LIMIT 1"
    ensure_valid_identifiers(sql)


def test_alias_unknown_column():
    sql = "SELECT f.not_a_col FROM public.film AS f LIMIT 1"
    with pytest.raises(IdentifierValidationError):
        ensure_valid_identifiers(sql)


def test_cte_columns_valid():
    sql = (
        "WITH x AS (SELECT film_id FROM public.film) "
        "SELECT film_id FROM x LIMIT 1"
    )
    ensure_valid_identifiers(sql)


def test_cte_unknown_column():
    sql = (
        "WITH x AS (SELECT film_id FROM public.film) "
        "SELECT nope FROM x LIMIT 1"
    )
    with pytest.raises(IdentifierValidationError):
        ensure_valid_identifiers(sql)
