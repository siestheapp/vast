"""FastAPI application exposing Vast services over HTTP."""

from __future__ import annotations

from typing import Any, Dict, Optional
import json

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import service
from .conversation import VastConversation
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path


def _json_safe(value):
    """Recursively convert objects into JSON-serializable structures."""
    # Primitives
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    # Datetime / Path
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)

    # SQLAlchemy rows expose _mapping for dict-like access
    if hasattr(value, "_asdict"):
        try:
            return {k: _json_safe(v) for k, v in value._asdict().items()}
        except Exception:
            return str(value)
    if hasattr(value, "_mapping"):
        try:
            return {k: _json_safe(v) for k, v in value._mapping.items()}
        except Exception:
            return str(value)

    # Mappings / Sequences / Sets
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in list(value)]

    # Fallback
    try:
        return json.loads(json.dumps(value))
    except Exception:
        return str(value)


class RunSQLRequest(BaseModel):
    sql: str
    params: Dict[str, Any] = Field(default_factory=dict)
    allow_writes: bool = False
    force_write: bool = False


class AskRequest(BaseModel):
    question: str
    params: Dict[str, Any] = Field(default_factory=dict)
    allow_writes: bool = False
    force_write: bool = False
    refresh_schema: bool = False
    retry: bool = True
    max_retries: int = 2


class DumpRequest(BaseModel):
    outfile: Optional[str] = None
    container_name: str = "vast-pg"
    fmt: str = "custom"


class RestoreListRequest(BaseModel):
    dumpfile: str


class RestoreRequest(BaseModel):
    dumpfile: str
    target_db_url: str
    drop: bool = False


class ConversationRequest(BaseModel):
    session: str

class ConversationProcessRequest(BaseModel):
    message: str
    session: Optional[str] = None
    auto_execute: bool = False


class KnowledgeRefreshRequest(BaseModel):
    force: bool = False


class KnowledgeSearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=25)


class RepoWriteRequest(BaseModel):
    path: str
    content: str
    overwrite: bool = False


def create_app() -> FastAPI:
    app = FastAPI(title="Vast1 API", version="0.1.0")
    conversations: Dict[str, VastConversation] = {}

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "environment": service.environment_status(),
        }

    @app.get("/schema/tables")
    def get_tables() -> Dict[str, Any]:
        return {"tables": service.tables()}

    @app.get("/schema/tables/{schema}/{table}")
    def get_columns(schema: str, table: str) -> Dict[str, Any]:
        return {"schema": schema, "table": table, "columns": service.columns(schema, table)}

    @app.post("/sql/run")
    def run_sql(payload: RunSQLRequest) -> Dict[str, Any]:
        try:
            result = service.execute_sql(
                payload.sql,
                params=payload.params,
                allow_writes=payload.allow_writes,
                force_write=payload.force_write,
            )
            return {"sql": payload.sql, "result": result}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/agent/ask")
    def ask_agent(payload: AskRequest) -> Dict[str, Any]:
        try:
            outcome = service.plan_and_execute(
                payload.question,
                params=payload.params,
                allow_writes=payload.allow_writes,
                force_write=payload.force_write,
                refresh_schema=payload.refresh_schema,
                retry=payload.retry,
                max_retries=payload.max_retries,
            )
            return outcome
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/artifacts")
    def artifacts() -> Dict[str, Any]:
        return {"artifacts": service.list_artifacts()}

    @app.post("/operations/dump")
    def create_dump(payload: DumpRequest) -> Dict[str, Any]:
        try:
            return service.create_dump(outfile=payload.outfile, container_name=payload.container_name, fmt=payload.fmt)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/operations/restore/list")
    def restore_list(payload: RestoreListRequest) -> Dict[str, Any]:
        return service.restore_list(payload.dumpfile)

    @app.post("/operations/restore")
    def restore(payload: RestoreRequest) -> Dict[str, Any]:
        return service.restore_into(payload.dumpfile, payload.target_db_url, drop=payload.drop)

    @app.post("/conversations/get")
    def get_conversation(payload: ConversationRequest) -> Dict[str, Any]:
        convo = service.load_conversation(payload.session)
        if convo is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return convo

    @app.post("/conversations/process")
    def process_conversation(payload: ConversationProcessRequest) -> Dict[str, Any]:
        # Reuse or create a conversation instance for the session
        sess = payload.session or "desktop"
        if sess not in conversations:
            conversations[sess] = VastConversation(sess)
        conv = conversations[sess]
        try:
            resp_text = conv.process(payload.message, auto_execute=payload.auto_execute)
            return {
                "session": conv.session_name,
                "response": resp_text,
                "actions": _json_safe(conv.last_actions),
            }
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/knowledge/refresh")
    def refresh_knowledge(payload: KnowledgeRefreshRequest) -> Dict[str, Any]:
        try:
            snapshot = service.ensure_knowledge_snapshot(force=payload.force)
            return snapshot
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/knowledge/snapshots")
    def knowledge_snapshots(limit: int = Query(default=20, ge=1, le=100)) -> Dict[str, Any]:
        return {"snapshots": service.list_knowledge_snapshots(limit=limit)}

    @app.get("/knowledge/entries")
    def knowledge_entries(
        entry_type: Optional[str] = Query(default=None, description="Filter by entry type"),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> Dict[str, Any]:
        return {
            "entries": service.list_knowledge_entries(entry_type=entry_type, limit=limit)
        }

    @app.post("/knowledge/search")
    def knowledge_search(payload: KnowledgeSearchRequest) -> Dict[str, Any]:
        try:
            results = service.knowledge_search(payload.query, top_k=payload.top_k)
            return {"results": results}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/repo/files")
    def repo_files(subdir: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        try:
            return service.repo_files(subdir=subdir)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/repo/read")
    def repo_read(path: str = Query(...)) -> Dict[str, Any]:
        try:
            return service.repo_read(path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/repo/write")
    def repo_write(payload: RepoWriteRequest) -> Dict[str, Any]:
        try:
            return service.repo_write(payload.path, payload.content, overwrite=payload.overwrite)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/audit/events")
    def audit_events(limit: int = Query(200, ge=1, le=2000)) -> Dict[str, Any]:
        from .audit import AUDIT_FILE
        if not AUDIT_FILE.exists():
            return {"events": []}
        lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
        return {"events": [json.loads(x) for x in lines]}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
