"""Persistent knowledge store for schema snapshots, rules, and embeddings."""

from __future__ import annotations

import json
import math
import sqlite3
from array import array
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from uuid import uuid4

from openai import OpenAI

from .config import settings
from .introspect import list_tables, table_columns, schema_fingerprint

KNOWLEDGE_DIR = Path(".vast/knowledge")
DB_PATH = KNOWLEDGE_DIR / "knowledge.db"
EMBED_DIM_META_KEY = "embedding_dim"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ensure_dir() -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class KnowledgeEntry:
    id: str
    type: str
    title: str
    content: str
    metadata: Dict[str, Any]
    embedding: Optional[Sequence[float]] = None
    created_at: str = _now()
    updated_at: str = _now()

    def to_row(self) -> Dict[str, Any]:
        data = asdict(self)
        emb = data.pop("embedding", None)
        if emb is not None:
            buf = array("f", emb).tobytes()
        else:
            buf = None
        data["metadata"] = json.dumps(data["metadata"] or {})
        data["embedding"] = buf
        return data

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "KnowledgeEntry":
        embedding_blob = row["embedding"]
        embedding = None
        if embedding_blob:
            embedding = array("f")
            embedding.frombytes(embedding_blob)
            embedding = embedding.tolist()
        metadata = json.loads(row["metadata"] or "{}")
        return cls(
            id=row["id"],
            type=row["type"],
            title=row["title"],
            content=row["content"],
            metadata=metadata,
            embedding=embedding,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class Snapshot:
    id: str
    fingerprint: str
    summary: str
    raw: Dict[str, Any]
    created_at: str = _now()

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "fingerprint": self.fingerprint,
            "summary": self.summary,
            "raw": json.dumps(self.raw, indent=2),
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Snapshot":
        return cls(
            id=row["id"],
            fingerprint=row["fingerprint"],
            summary=row["summary"],
            raw=json.loads(row["raw"] or "{}"),
            created_at=row["created_at"],
        )


class KnowledgeStore:
    def __init__(self) -> None:
        self._client: Optional[OpenAI] = None
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Schema & metadata helpers
    # ------------------------------------------------------------------
    def _ensure_schema(self) -> None:
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    raw TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    embedding BLOB,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_entries_type ON entries(type);
                CREATE INDEX IF NOT EXISTS idx_entries_created ON entries(created_at);
                """
            )

    def _get_client(self) -> Optional[OpenAI]:
        if not settings.openai_api_key:
            return None
        if self._client is None:
            self._client = OpenAI(api_key=settings.openai_api_key)
        return self._client

    # ------------------------------------------------------------------
    # Snapshot management
    # ------------------------------------------------------------------
    def latest_snapshot(self) -> Optional[Snapshot]:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM snapshots ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return Snapshot.from_row(row)

    def list_snapshots(self, limit: int = 20) -> List[Snapshot]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM snapshots ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Snapshot.from_row(r) for r in rows]

    def capture_schema_snapshot(self, force: bool = False) -> Snapshot:
        fp = schema_fingerprint()
        latest = self.latest_snapshot()
        if latest and latest.fingerprint == fp and not force:
            return latest

        tables = []
        for tbl in list_tables():
            schema = tbl["table_schema"]
            name = tbl["table_name"]
            columns = table_columns(schema, name)
            tables.append({
                "schema": schema,
                "name": name,
                "columns": columns,
            })

        summary_lines = [
            f"{t['schema']}.{t['name']}: "
            + ", ".join(
                f"{c['column_name']} {c['data_type']}"
                + (" NOT NULL" if c.get("is_nullable") == "NO" else "")
                for c in t["columns"]
            )
            for t in tables
        ]
        summary = "\n".join(summary_lines)

        snap = Snapshot(
            id=str(uuid4()),
            fingerprint=fp,
            summary=summary,
            raw={"tables": tables},
        )

        with _connect() as conn:
            conn.execute(
                "INSERT INTO snapshots (id, fingerprint, summary, raw, created_at)"
                " VALUES (:id,:fingerprint,:summary,:raw,:created_at)",
                snap.to_row(),
            )

        # Remove stale entries for this fingerprint before re-inserting
        with _connect() as conn:
            conn.execute(
                "DELETE FROM entries WHERE json_extract(metadata, '$.fingerprint') = ?",
                (snap.fingerprint,),
            )

        entries = self._build_entries_from_snapshot(snap)
        self.upsert_entries(entries)
        return snap

    # ------------------------------------------------------------------
    # Entry management
    # ------------------------------------------------------------------
    def _build_entries_from_snapshot(self, snap: Snapshot) -> List[KnowledgeEntry]:
        entries: List[KnowledgeEntry] = []
        created = _now()
        for table in snap.raw.get("tables", []):
            schema = table["schema"]
            name = table["name"]
            cols = table.get("columns", [])
            column_lines = [
                f"- {c['column_name']}: {c['data_type']}"
                + (" NOT NULL" if c.get("is_nullable") == "NO" else "")
                + (f" DEFAULT {c['column_default']}" if c.get("column_default") else "")
                for c in cols
            ]
            content = (
                f"Table {schema}.{name}\nColumns:\n" + "\n".join(column_lines)
            )
            entries.append(
                KnowledgeEntry(
                    id=str(uuid4()),
                    type="table",
                    title=f"{schema}.{name}",
                    content=content,
                    metadata={
                        "schema": schema,
                        "table": name,
                        "fingerprint": snap.fingerprint,
                    },
                    created_at=created,
                    updated_at=created,
                )
            )
        # Whole schema summary entry
        entries.append(
            KnowledgeEntry(
                id=str(uuid4()),
                type="schema_summary",
                title="Schema Overview",
                content=snap.summary,
                metadata={"fingerprint": snap.fingerprint},
                created_at=created,
                updated_at=created,
            )
        )
        return entries

    def upsert_entries(self, entries: Iterable[KnowledgeEntry]) -> None:
        entries = list(entries)
        if not entries:
            return

        embeddings_needed = [e for e in entries if settings.openai_api_key]
        if embeddings_needed:
            texts = [e.content for e in embeddings_needed]
            embeddings = self._embed(texts)
            for entry, emb in zip(embeddings_needed, embeddings):
                entry.embedding = emb

        with _connect() as conn:
            for entry in entries:
                conn.execute(
                    """
                    INSERT INTO entries
                        (id,type,title,content,metadata,embedding,created_at,updated_at)
                    VALUES
                        (:id,:type,:title,:content,:metadata,:embedding,:created_at,:updated_at)
                    ON CONFLICT(id) DO UPDATE SET
                        type=excluded.type,
                        title=excluded.title,
                        content=excluded.content,
                        metadata=excluded.metadata,
                        embedding=excluded.embedding,
                        updated_at=excluded.updated_at
                    """,
                    entry.to_row(),
                )

    def list_entries(self, entry_type: Optional[str] = None, limit: int = 50) -> List[KnowledgeEntry]:
        sql = "SELECT * FROM entries"
        params: List[Any] = []
        if entry_type:
            sql += " WHERE type = ?"
            params.append(entry_type)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [KnowledgeEntry.from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------
    def _embed(self, texts: Sequence[str]) -> List[List[float]]:
        client = self._get_client()
        if client is None:
            return [[] for _ in texts]
        model = getattr(settings, "openai_embedding_model", "text-embedding-3-small")
        response = client.embeddings.create(model=model, input=list(texts))
        vectors = [item.embedding for item in response.data]
        # Persist vector dimension for sanity checking
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES (?, ?)",
                (EMBED_DIM_META_KEY, str(len(vectors[0]) if vectors else 0)),
            )
        return vectors

    def search(self, query: str, top_k: int = 5) -> List[KnowledgeEntry]:
        with _connect() as conn:
            rows = conn.execute("SELECT * FROM entries").fetchall()
        entries = [KnowledgeEntry.from_row(r) for r in rows]
        if not entries:
            return []

        client = self._get_client()
        if client is None:
            # fallback: naive keyword ranking
            query_lower = query.lower()
            scored = [
                (entry, entry.content.lower().count(query_lower))
                for entry in entries
            ]
            scored.sort(key=lambda x: x[1], reverse=True)
            return [e for e, score in scored[:top_k] if score > 0]

        query_vec = self._embed([query])[0]
        if not query_vec:
            return entries[:top_k]

        scored = []
        for entry in entries:
            if not entry.embedding:
                continue
            score = _cosine_similarity(query_vec, entry.embedding)
            scored.append((entry, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [e for e, _ in scored[:top_k]]


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# Singleton accessor ------------------------------------------------------

_knowledge_store: Optional[KnowledgeStore] = None


def get_knowledge_store() -> KnowledgeStore:
    global _knowledge_store
    if _knowledge_store is None:
        _knowledge_store = KnowledgeStore()
    return _knowledge_store


__all__ = ["KnowledgeStore", "KnowledgeEntry", "Snapshot", "get_knowledge_store"]
