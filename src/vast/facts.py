from __future__ import annotations

import json
import os
import re
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


IDENTITY_SQL = """
SELECT
  current_database()               AS database,
  inet_server_addr()::text         AS host,
  inet_server_port()               AS port,
  version()                        AS version
""".strip()

DB_SIZE_SQL = """
SELECT
  pg_database_size(current_database())                 AS size_bytes,
  pg_size_pretty(pg_database_size(current_database())) AS size_pretty
""".strip()

TABLE_COUNT_SQL = """
SELECT COUNT(*) AS table_count
FROM information_schema.tables
WHERE table_type = 'BASE TABLE'
  AND table_schema NOT IN ('pg_catalog','information_schema','pg_toast');
""".strip()


def _mask_host_port(s: str) -> str:
    s = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(:\d+)?\b", "•••:•••", s)
    s = re.sub(r"\b[a-zA-Z0-9_.-]+(:\d+)\b", "•••:•••", s)
    return s


def _maybe_mask(s: str) -> str:
    if os.getenv("VAST_MASK_HOST_PORT", "true").lower() in {"1", "true", "yes"}:
        return _mask_host_port(s)
    return s


@dataclass
class FactsRuntime:
    database_url: Optional[str] = None
    schema_fingerprint: Optional[str] = None
    schema_cache: Any | None = None
    engine: Any | None = None
    db: Any | None = None
    audit: Optional[List[Any]] = None
    auto_load_fingerprint: bool = True

    def __post_init__(self) -> None:
        if self.schema_cache is None:
            self.schema_cache = SchemaCache()

        if self.schema_fingerprint is None and self.auto_load_fingerprint:
            # Lazy import to avoid circular dependency at import time
            from .introspect import schema_fingerprint

            try:
                self.schema_fingerprint = schema_fingerprint()
            except Exception:
                self.schema_fingerprint = None

        if self.engine is None:
            try:
                self.engine = get_engine(readonly=True)
            except Exception:
                self.engine = None

        if self.audit is None:
            self.audit = []

    @classmethod
    def from_context(cls, ctx: Any) -> "FactsRuntime":
        cache = getattr(ctx, "schema_cache", None)
        fingerprint = (
            getattr(ctx, "schema_fingerprint", None)
            or getattr(ctx, "last_fingerprint", None)
            or getattr(cache, "fingerprint", None)
        )
        return cls(
            database_url=getattr(ctx, "database_url", None),
            schema_fingerprint=fingerprint,
            schema_cache=cache,
            engine=getattr(ctx, "engine", None),
            db=getattr(ctx, "db", None),
            audit=getattr(ctx, "audit", None),
            auto_load_fingerprint=False,
        )

    def _schema_cache_is_fresh(self) -> bool:
        cache = self.schema_cache
        fingerprint = self.schema_fingerprint
        if cache is None:
            return False
        if hasattr(cache, "is_table_count_fresh"):
            return cache.is_table_count_fresh(fingerprint)
        if hasattr(cache, "is_fresh"):
            try:
                return bool(cache.is_fresh())
            except TypeError:
                return False
        return False

    def _schema_cache_update(self, count: int) -> None:
        cache = self.schema_cache
        if cache is None:
            return
        if hasattr(cache, "update_table_count"):
            cache.update_table_count(count, self.schema_fingerprint)
            return
        if hasattr(cache, "table_count"):
            cache.table_count = count
        if hasattr(cache, "touch"):
            cache.touch()

    def fetch_db_identity(self) -> Optional[Dict[str, Any]]:
        if self.engine and hasattr(self.engine, "current_database") and hasattr(self.engine, "server_version"):
            # Adapter path for lightweight fakes/tests
            return {
                "database": self.engine.current_database(),
                "host": getattr(self.engine, "host", "localhost"),
                "port": getattr(self.engine, "port", None),
                "version": self.engine.server_version(),
                "sql": IDENTITY_SQL,
            }

        if not self.engine or not hasattr(self.engine, "begin"):
            return None
        with self.engine.begin() as conn:
            row = conn.execute(text(IDENTITY_SQL)).mappings().first()
        if row is None:
            return None
        payload = dict(row)
        payload.setdefault("sql", IDENTITY_SQL)
        return payload

    def fetch_table_count(self) -> tuple[Optional[int], str, Optional[Dict[str, Any]]]:
        cache_hit = self._schema_cache_is_fresh()
        cache = self.schema_cache

        if cache_hit:
            count = getattr(cache, "table_count", None)
            try:
                count = int(count) if count is not None else None
            except (TypeError, ValueError):
                count = None
            metadata = {
                "success": True,
                "type": "FACT",
                "fact_key": "table_count",
                "sql": TABLE_COUNT_SQL,
                "rows": [{"table_count": count}] if count is not None else [{}],
                "source": "facts",
            }
            log = {
                "content": "Facts: table count lookup (cache)",
                "metadata": metadata,
            }
            return count, "cache", log

        count: Optional[int] = None
        if self.db and hasattr(self.db, "scalar"):
            value = self.db.scalar(TABLE_COUNT_SQL)
            if value is not None:
                count = int(value)
        elif self.engine and hasattr(self.engine, "begin"):
            with self.engine.begin() as conn:
                row = conn.execute(text(TABLE_COUNT_SQL)).mappings().first()
            if row is not None:
                raw = row.get("table_count") if isinstance(row, dict) else row["table_count"]
                count = int(raw)
        else:
            count = None

        if count is None:
            return None, "live-sql", None

        self._schema_cache_update(count)
        metadata = {
            "success": True,
            "type": "FACT",
            "fact_key": "table_count",
            "sql": TABLE_COUNT_SQL,
            "rows": [{"table_count": count}],
            "source": "facts+live-sql",
        }
        log = {
            "content": "Facts: table count lookup",
            "metadata": metadata,
        }
        return count, "live-sql", log

    def fetch_db_size(self) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        if not self.engine or not hasattr(self.engine, "begin"):
            return None, None

        with self.engine.begin() as conn:
            row = conn.execute(text(DB_SIZE_SQL)).mappings().first()
        if row is None:
            return None, None

        size_bytes = int(row.get("size_bytes") if isinstance(row, dict) else row["size_bytes"])
        size_pretty = row.get("size_pretty") if isinstance(row, dict) else row["size_pretty"]
        payload = {
            "size_bytes": size_bytes,
            "size_pretty": size_pretty,
        }
        metadata = {
            "success": True,
            "type": "FACT",
            "fact_key": "db_size",
            "sql": DB_SIZE_SQL,
            "rows": [{"size_bytes": size_bytes, "size_pretty": size_pretty}],
            "source": "facts+live-sql",
        }
        log = {
            "content": "Facts: database size lookup",
            "metadata": metadata,
        }
        return payload, log


@dataclass
class ExecutionLog:
    content: str
    metadata: Dict[str, Any]


@dataclass
class FactResolution:
    key: str
    content: str
    source: str
    payload: Dict[str, Any]
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
    payload = {
        "database": db_name,
        "host": host,
        "port": port,
        "server_version": version,
        "table_count": count,
        "source": "facts" if source == "cache" else "facts+live-sql",
    }

    lines = [
        f"- Database: `{db_name}` (PostgreSQL {version}) at `{host}:{port}`",
        f"- User tables: {count} (excludes pg_catalog, information_schema)",
        f"_Source: facts (identity live SQL; table count {source_note})._",
    ]
    content = _maybe_mask("\n".join(lines))

    logs: List[ExecutionLog] = []
    identity_log = ExecutionLog(
        content="Facts: database identity lookup",
        metadata={
            "success": True,
            "type": "FACT",
            "fact_key": "db_identity",
            "sql": identity.get("sql"),
            "rows": [{
                "database": identity.get("database"),
                "host": identity.get("host"),
                "port": identity.get("port"),
                "version": identity.get("version"),
            }],
            "source": "facts",
        },
    )
    logs.append(identity_log)
    if log:
        logs.append(ExecutionLog(content=log["content"], metadata=log["metadata"]))

    return FactResolution(
        key="db_and_table_count",
        content=content,
        source=source,
        payload=payload,
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
    content = _maybe_mask("\n".join(lines))

    payload = {
        "database": db_name,
        "host": host,
        "port": port,
        "server_version": version,
        "source": "facts",
    }

    log = ExecutionLog(
        content="Facts: database identity lookup",
        metadata={
            "success": True,
            "type": "FACT",
            "fact_key": "db_identity",
            "sql": identity.get("sql"),
            "rows": [{
                "database": identity.get("database"),
                "host": identity.get("host"),
                "port": identity.get("port"),
                "version": identity.get("version"),
            }],
            "source": "facts",
        },
    )

    return FactResolution(
        key="db_identity",
        content=content,
        source="live-sql",
        payload=payload,
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
    content = _maybe_mask("\n".join(lines))

    logs: List[ExecutionLog] = []
    if log:
        logs.append(ExecutionLog(content=log["content"], metadata=log["metadata"]))

    payload = {
        "table_count": count,
        "source": "facts" if source == "cache" else "facts+live-sql",
    }

    return FactResolution(
        key="table_count",
        content=content,
        source=source,
        payload=payload,
        intents_consumed={"table_count"},
        log_entries=logs,
    )


def _detect_db_size(text: str) -> Optional[Dict[str, Any]]:
    ql = text.lower()
    size_tokens = (
        "database size",
        "size of the database",
        "how big is the database",
        "how large is the database",
        "db size",
        "how large is it",
        "how big is it",
    )
    return {} if any(token in ql for token in size_tokens) else None


def _resolve_db_size(runtime: FactsRuntime, slots: Dict[str, Any]) -> Optional[FactResolution]:
    payload, log = runtime.fetch_db_size()
    if payload is None:
        return None

    size_pretty = payload.get("size_pretty")
    size_bytes = payload.get("size_bytes")
    content = (
        f"Database size: **{size_pretty}** ({size_bytes} bytes).\n"
        "_Source: facts (live SQL)._"
    )

    logs: List[ExecutionLog] = []
    if log:
        logs.append(ExecutionLog(content=log["content"], metadata=log["metadata"]))

    return FactResolution(
        key="db_size",
        content=content,
        source="live-sql",
        payload={
            "database_size_pretty": size_pretty,
            "database_size_bytes": size_bytes,
            "source": "facts+live-sql",
        },
        intents_consumed={"db_size"},
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
    Fact(
        key="db_size",
        intents={"db_size"},
        detect=_detect_db_size,
        resolve=_resolve_db_size,
    ),
]


@dataclass
class FactAnswer:
    payload: Dict[str, Any]
    content: str
    log_entries: List[ExecutionLog] = field(default_factory=list)

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def keys(self):
        return self.payload.keys()

    def items(self):
        return self.payload.items()

    def __contains__(self, key: str) -> bool:
        return key in self.payload


def try_answer_with_facts(runtime_or_ctx: Any, user_text: str) -> Optional[FactAnswer]:
    if isinstance(runtime_or_ctx, FactsRuntime):
        runtime = runtime_or_ctx
    else:
        runtime = FactsRuntime.from_context(runtime_or_ctx)

    consumed: Set[str] = set()
    chunks: List[str] = []
    logs: List[ExecutionLog] = []
    payload: Dict[str, Any] = {}

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
        payload.update(resolution.payload)

    if not chunks:
        return None

    content = "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip())
    return FactAnswer(payload=payload, content=content, log_entries=logs)
