import logging
from contextlib import contextmanager

import pytest

from src.vast import service


class DummyResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def first(self):
        return self.fetchone()

    def scalar(self):
        row = self.fetchone()
        if row and len(row) >= 1:
            return row[0]
        return None


class TrackingConnection:
    def __init__(self, engine=None):
        self.engine = engine
        self.events = []
        self.statements = []
        self.closed = False

    def begin(self):
        self.events.append("BEGIN")
        return _Txn(self)

    def close(self):
        self.closed = True
        self.events.append("CLOSE")

    def execute(self, clause, params=None):
        sql = getattr(clause, "text", str(clause))
        self.statements.append(sql)
        if "current_user" in sql:
            return DummyResult([("demo_user", "demo_session")])
        return DummyResult()

    def exec_driver_sql(self, sql, params=None):
        self.statements.append(sql)
        if "current_user" in sql:
            return DummyResult([("demo_user", "demo_session")])
        if "pg_get_serial_sequence" in sql:
            return DummyResult([(None,)])
        return DummyResult()


class _Txn:
    def __init__(self, conn: TrackingConnection):
        self.conn = conn
        self._completed = False

    def commit(self):
        if not self._completed:
            self.conn.events.append("COMMIT")
            self._completed = True

    def rollback(self):
        if not self._completed:
            self.conn.events.append("ROLLBACK")
            self._completed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._completed:
            if exc_type:
                self.conn.events.append("ROLLBACK")
            else:
                self.conn.events.append("COMMIT")
            self._completed = True
        return False


def test_preflight_logs_writer_identity(monkeypatch, caplog):
    connections = []

    @contextmanager
    def fake_writer_conn():
        conn = TrackingConnection()
        connections.append(conn)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(service, "_writer_conn", fake_writer_conn)
    caplog.set_level(logging.INFO, logger="src.vast.service")

    service.preflight_statements(["CREATE TABLE demo_table(id int)"])

    assert connections
    first_conn = connections[0]
    assert first_conn.events == ["BEGIN", "ROLLBACK", "CLOSE"]
    assert any("SELECT current_user" in stmt for stmt in first_conn.statements)
    assert any("writer session:" in record.message for record in caplog.records)


def test_apply_logs_writer_identity(monkeypatch, caplog):
    dummy_engine = _ApplyDummyEngine()

    def fake_get_engine(readonly=True):
        return object() if readonly else dummy_engine

    monkeypatch.setattr(service, "get_engine", fake_get_engine)
    caplog.set_level(logging.INFO, logger="src.vast.service")

    service.apply_statements(["SELECT 1"])

    assert len(dummy_engine.connections) == 1
    conn = dummy_engine.connections[0]
    assert conn.events == ["BEGIN", "COMMIT", "CLOSE"]
    log_messages = [record.message for record in caplog.records]
    assert log_messages.count("writer session: current_user=demo_user session_user=demo_session") == 1


class _ApplyDummyEngine:
    def __init__(self):
        self.connections = []

    def connect(self):
        conn = TrackingConnection(self)
        self.connections.append(conn)
        return conn
