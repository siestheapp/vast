import logging
from contextlib import contextmanager

import pytest

from src.vast import service


class DummyResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchone(self):
        return self._rows[0] if self._rows else None


class DummyTransaction:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self):
        self.conn.commits += 1

    def rollback(self):
        self.conn.rollbacks += 1


class DummyConnection:
    def __init__(self):
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, stmt, params=None):
        sql = getattr(stmt, "text", str(stmt))
        self.executed.append(sql)
        if "current_user" in sql:
            return DummyResult([("demo_user", "demo_session")])
        return DummyResult()

    def begin(self):
        return DummyTransaction(self)

    def close(self):
        pass


def _patch_writer_conn(monkeypatch, bucket):
    @contextmanager
    def fake_writer_conn():
        conn = DummyConnection()
        bucket.append(conn)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(service, "_writer_conn", fake_writer_conn)


def test_preflight_logs_writer_identity(monkeypatch, caplog):
    connections = []
    _patch_writer_conn(monkeypatch, connections)
    caplog.set_level(logging.INFO, logger="src.vast.service")

    service.preflight_statements(["CREATE TABLE demo_table(id int)"])

    assert connections
    first_conn = connections[0]
    assert any("SELECT current_user" in sql for sql in first_conn.executed)
    assert any("writer user:" in record.message for record in caplog.records)


def test_apply_logs_writer_identity(monkeypatch, caplog):
    connections = []
    _patch_writer_conn(monkeypatch, connections)
    caplog.set_level(logging.INFO, logger="src.vast.service")

    service.apply_statements(["SELECT 1"])

    assert len(connections) == 2  # main tx + grants
    log_messages = [record.message for record in caplog.records]
    assert log_messages.count("writer user: demo_user (session: demo_session)") == 2
