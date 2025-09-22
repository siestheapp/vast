from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from sqlalchemy import text

from .db import get_engine


CACHE_PATH = Path(".vast/schema_cache.json")


class SchemaCache:
    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        self.data: Dict[str, Any] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except Exception:
                self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    @property
    def fingerprint(self) -> Optional[str]:
        return self.data.get("fingerprint")

    @property
    def table_count(self) -> Optional[int]:
        val = self.data.get("table_count")
        try:
            return int(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def table_count_refreshed_at(self) -> Optional[str]:
        return self.data.get("table_count_refreshed_at")

    def is_table_count_fresh(self, fingerprint: Optional[str]) -> bool:
        return bool(
            fingerprint
            and self.table_count is not None
            and self.data.get("fingerprint") == fingerprint
        )

    def update_table_count(self, count: int, fingerprint: Optional[str]) -> None:
        self.data["table_count"] = int(count)
        self.data["table_count_refreshed_at"] = datetime.utcnow().isoformat()
        if fingerprint:
            self.data["fingerprint"] = fingerprint
        self.save()


@dataclass
class FactsRuntime:
    database_url: str
    schema_fingerprint: Optional[str] = None
    schema_cache: SchemaCache = field(default_factory=SchemaCache)

    def __post_init__(self) -> None:
        if self.schema_fingerprint is None:
            # Lazy import to avoid circular dependency
            from .introspect import schema_fingerprint

            try:
                self.schema_fingerprint = schema_fingerprint()
            except Exception:
                self.schema_fingerprint = None

    @property
    def engine(self):
        return get_engine(readonly=True)

    def fetch_db_identity(self) -> Optional[Dict[str, Any]]:
        sql = """
        SELECT
          current_database()               AS database,
          inet_server_addr()::text         AS host,
          inet_server_port()               AS port,
          version()                        AS version
        """
        with self.engine.begin() as conn:
            row = conn.execute(text(sql)).mappings().first()
        if row is None:
            return None
        payload = dict(row)
        payload.setdefault("sql", sql)
        return payload

    def fetch_table_count(self) -> tuple[Optional[int], str, Optional[Dict[str, Any]]]:
        if self.schema_cache.is_table_count_fresh(self.schema_fingerprint):
            return self.schema_cache.table_count, "cache", None

        sql = """
        SELECT COUNT(*) AS table_count
        FROM information_schema.tables
        WHERE table_schema NOT IN ('pg_catalog','information_schema');
        """
        with self.engine.begin() as conn:
            row = conn.execute(text(sql)).mappings().first()
        if row is None:
            return None, "live-sql", None
        count = int(row.get("table_count") or row["table_count"])
        self.schema_cache.update_table_count(count, self.schema_fingerprint)
        metadata = {
            "success": True,
            "type": "table_count",
            "sql": sql.strip(),
            "rows": [dict(row)],
            "count": 1,
            "source": "facts",
        }
        log = {
            "content": "Facts: table count lookup",
            "metadata": metadata,
        }
        return count, "live-sql", log


@dataclass
class ExecutionLog:
    content: str
    metadata: Dict[str, Any]


@dataclass
class FactResolution:
    key: str
    content: str
    source: str
    intents_consumed: Set[str] = field(default_factory=set)
    log_entries: List[ExecutionLog] = field(default_factory=list)


@dataclass
class Fact:
    key: str
    intents: Set[str]
    detect: Callable[[str], Optional[Dict[str, Any]]]
    resolve: Callable[[FactsRuntime, Dict[str, Any]], Optional[FactResolution]]


def _detect_db_and_table_count(text: str) -> Optional[Dict[str, Any]]:
    ql = text.lower()
    hits_identity = ("what database" in ql) or ("which database" in ql) or ("connected to" in ql and "database" in ql) or ("what db" in ql)
    hits_tables = (
        "how many tables" in ql
        or "tables does it have" in ql
        or "table count" in ql
        or "number of tables" in ql
    )
    return {} if hits_identity and hits_tables else None


def _detect_db_identity(text: str) -> Optional[Dict[str, Any]]:
    ql = text.lower()
    if ("what database" in ql) or ("which database" in ql) or ("connected to" in ql and "database" in ql) or ("what db" in ql):
        return {}
    return None


def _detect_table_count(text: str) -> Optional[Dict[str, Any]]:
    ql = text.lower()
    if (
        "how many tables" in ql
        or "tables does it have" in ql
        or "table count" in ql
        or "number of tables" in ql
    ):
        return {}
    return None


def _format_version(raw: Optional[str]) -> str:
    if not raw:
        return "unknown"
    head = raw.split(" on ", 1)[0]
    return head.strip()


def _resolve_db_and_table_count(runtime: FactsRuntime, slots: Dict[str, Any]) -> Optional[FactResolution]:
    identity = runtime.fetch_db_identity()
    if identity is None:
        return None

    count, source, log = runtime.fetch_table_count()
    if count is None:
        return None

    host = identity.get("host") or "localhost"
    port = identity.get("port")
    version = _format_version(identity.get("version"))
    db_name = identity.get("database") or "unknown"
    source_note = "cache" if source == "cache" else "live SQL"

    lines = [
        f"- Database: `{db_name}` (PostgreSQL {version}) at `{host}:{port}`",
        f"- User tables: {count} (excludes pg_catalog, information_schema)",
        f"_Source: facts (identity live SQL; table count {source_note})._",
    ]

    logs: List[ExecutionLog] = []
    identity_log = ExecutionLog(
        content="Facts: database identity lookup",
        metadata={
            "success": True,
            "type": "db_identity",
            "sql": identity.get("sql"),
            "rows": [{
                "database": identity.get("database"),
                "host": identity.get("host"),
                "port": identity.get("port"),
                "version": identity.get("version"),
            }],
            "count": 1,
            "source": "facts",
        },
    )
    logs.append(identity_log)
    if log:
        logs.append(ExecutionLog(content=log["content"], metadata=log["metadata"]))

    return FactResolution(
        key="db_and_table_count",
        content="\n".join(lines),
        source=source,
        intents_consumed={"db_identity", "table_count"},
        log_entries=logs,
    )


def _resolve_db_identity(runtime: FactsRuntime, slots: Dict[str, Any]) -> Optional[FactResolution]:
    identity = runtime.fetch_db_identity()
    if identity is None:
        return None

    host = identity.get("host") or "localhost"
    port = identity.get("port")
    version = _format_version(identity.get("version"))
    db_name = identity.get("database") or "unknown"

    lines = [
        f"Connected to `{db_name}` at `{host}:{port}` (PostgreSQL {version}).",
        "_Source: facts (live SQL)._",
    ]

    log = ExecutionLog(
        content="Facts: database identity lookup",
        metadata={
            "success": True,
            "type": "db_identity",
            "sql": identity.get("sql"),
            "rows": [{
                "database": identity.get("database"),
                "host": identity.get("host"),
                "port": identity.get("port"),
                "version": identity.get("version"),
            }],
            "count": 1,
            "source": "facts",
        },
    )

    return FactResolution(
        key="db_identity",
        content="\n".join(lines),
        source="live-sql",
        intents_consumed={"db_identity"},
        log_entries=[log],
    )


def _resolve_table_count(runtime: FactsRuntime, slots: Dict[str, Any]) -> Optional[FactResolution]:
    count, source, log = runtime.fetch_table_count()
    if count is None:
        return None

    source_note = "cache" if source == "cache" else "live SQL"
    lines = [
        f"User tables: {count} (excludes pg_catalog, information_schema).",
        f"_Source: facts (table count {source_note})._",
    ]

    logs: List[ExecutionLog] = []
    if log:
        logs.append(ExecutionLog(content=log["content"], metadata=log["metadata"]))

    return FactResolution(
        key="table_count",
        content="\n".join(lines),
        source=source,
        intents_consumed={"table_count"},
        log_entries=logs,
    )


FACTS: List[Fact] = [
    Fact(
        key="db_and_table_count",
        intents={"db_identity", "table_count"},
        detect=_detect_db_and_table_count,
        resolve=_resolve_db_and_table_count,
    ),
    Fact(
        key="db_identity",
        intents={"db_identity"},
        detect=_detect_db_identity,
        resolve=_resolve_db_identity,
    ),
    Fact(
        key="table_count",
        intents={"table_count"},
        detect=_detect_table_count,
        resolve=_resolve_table_count,
    ),
]


@dataclass
class FactAnswer:
    content: str
    log_entries: List[ExecutionLog] = field(default_factory=list)


def try_answer_with_facts(runtime: FactsRuntime, user_text: str) -> Optional[FactAnswer]:
    consumed: Set[str] = set()
    chunks: List[str] = []
    logs: List[ExecutionLog] = []

    for fact in FACTS:
        if fact.intents & consumed:
            continue
        slots = fact.detect(user_text)
        if slots is None:
            continue
        resolution = fact.resolve(runtime, slots)
        if resolution is None:
            continue
        consumed.update(resolution.intents_consumed or fact.intents)
        chunks.append(resolution.content)
        logs.extend(resolution.log_entries)

    if not chunks:
        return None

    content = "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip())
    return FactAnswer(content=content, log_entries=logs)
