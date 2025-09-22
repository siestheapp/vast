import os
import pytest

from src.vast.catalog_pg import largest_tables, seq_scans_by_table, unused_indexes


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="DATABASE_URL environment variable not set"
)


def _assert_keys(rows, expected_keys):
    assert rows, "expected at least one row"
    for row in rows:
        assert set(row.keys()) == expected_keys


def test_largest_tables_sorted_and_typed():
    rows = largest_tables(limit=5)
    _assert_keys(rows, {"table", "total_bytes", "approx_rows"})
    prev_bytes = None
    for row in rows:
        assert isinstance(row["table"], str)
        assert isinstance(row["total_bytes"], int)
        assert isinstance(row["approx_rows"], int)
        if prev_bytes is not None:
            assert row["total_bytes"] <= prev_bytes
        prev_bytes = row["total_bytes"]


def test_seq_scans_sorted_and_typed():
    rows = seq_scans_by_table(limit=5)
    _assert_keys(rows, {"table", "seq_scan", "idx_scan", "live_rows"})
    prev_seq = None
    for row in rows:
        assert isinstance(row["table"], str)
        assert isinstance(row["seq_scan"], int)
        assert isinstance(row["idx_scan"], int)
        assert isinstance(row["live_rows"], int)
        if prev_seq is not None:
            assert row["seq_scan"] <= prev_seq
        prev_seq = row["seq_scan"]


def test_unused_indexes_typed():
    rows = unused_indexes(limit=5)
    _assert_keys(rows, {"table", "index", "index_bytes", "idx_scan"})
    for row in rows:
        assert isinstance(row["table"], str)
        assert isinstance(row["index"], str)
        assert isinstance(row["index_bytes"], int)
        assert isinstance(row["idx_scan"], int)
