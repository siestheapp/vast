from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

import anyio

from mcp import server as mcp_server
from mcp.server.stdio import stdio_server
from mcp.server.websocket import websocket_server
from mcp.shared.exceptions import McpError
from mcp.types import CallToolRequest, CallToolResult, TextContent, Tool

from vast.audit import AUDIT_FILE
from vast.service import (
    columns as service_columns,
    environment_status,
    execute_sql as service_execute_sql,
    plan_and_execute as service_plan_and_execute,
    tables as service_tables,
)

logger = logging.getLogger(__name__)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if as_int == value else float(value)
    if isinstance(value, Path):
        return str(value)
    return value


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, default=_json_default, indent=2, sort_keys=True)


async def _run_sync(func, *args, **kwargs):
    return await anyio.to_thread.run_sync(func, *args, **kwargs)


def _ensure_dict(obj: Any, label: str) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    raise McpError(f"Expected {label} to be an object")


def _format_sql_result(result: Dict[str, Any]) -> Dict[str, Any]:
    meta = _ensure_dict(result.get("meta"), "meta")
    return {
        "rows": result.get("rows", []),
        "columns": result.get("columns", []),
        "row_count": result.get("row_count", len(result.get("rows", []) or [])),
        "stmt_kind": result.get("stmt_kind"),
        "exec_ms": result.get("exec_ms", meta.get("exec_ms")),
        "engine_ms": result.get("engine_ms", meta.get("engine_ms")),
    }


def _format_plan_result(result: Dict[str, Any]) -> Dict[str, Any]:
    execution = result.get("execution")
    formatted_exec: Optional[Dict[str, Any]] = None
    if isinstance(execution, dict):
        formatted_exec = _format_sql_result(execution)
        if execution.get("dry_run"):
            formatted_exec["dry_run"] = True
    payload: Dict[str, Any] = {
        "sql": result.get("sql"),
        "answer": result.get("answer"),
        "intent": result.get("intent"),
    }
    if formatted_exec is not None:
        payload["execution"] = formatted_exec
    if "meta" in result and result["meta"] is not None:
        payload["meta"] = result["meta"]
    if "breadcrumbs" in result and result["breadcrumbs"]:
        payload["breadcrumbs"] = result["breadcrumbs"]
    if "resolver" in result and result["resolver"]:
        payload["resolver"] = result["resolver"]
    return payload


def _load_breadcrumb_events(limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    if not AUDIT_FILE.exists():
        return []
    try:
        lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as exc:  # pragma: no cover - defensive
        logger.debug("Failed to read audit log: %s", exc)
        return []
    events: list[dict[str, Any]] = []
    for raw in lines[-limit:]:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        crumbs = parsed.get("breadcrumbs")
        if crumbs:
            events.append(
                {
                    "ts": parsed.get("ts"),
                    "breadcrumbs": crumbs,
                    "meta": parsed.get("meta"),
                    "sql": parsed.get("sql"),
                }
            )
    return events


def _normalize_table_arguments(arguments: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    schema = arguments.get("schema")
    table = arguments.get("table")
    if schema is not None and not isinstance(schema, str):
        raise McpError("'schema' must be a string if provided")
    if table is not None and not isinstance(table, str):
        raise McpError("'table' must be a string if provided")
    return schema, table


def _normalize_query_arguments(arguments: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    sql = arguments.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise McpError("'sql' is required and must be a non-empty string")
    params = arguments.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise McpError("'params' must be an object if provided")
    return sql, params


def _normalize_resolver_arguments(arguments: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    prompt = arguments.get("prompt") or arguments.get("question")
    if not isinstance(prompt, str) or not prompt.strip():
        raise McpError("'prompt' is required and must be a non-empty string")
    params = arguments.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise McpError("'params' must be an object if provided")
    return prompt, params


def _tool_response(payload: Dict[str, Any]) -> CallToolResult:
    """Create a CallToolResult with both text and structured payload."""

    return CallToolResult(
        content=[TextContent(type="text", text=_json_dumps(payload))],
        structuredContent=payload,
        isError=False,
    )


class VastMCPServer:
    """Adapter exposing VAST service functionality over the MCP protocol."""

    def __init__(self) -> None:
        self._app = mcp_server.Server("vast-mcp")
        self._register_tools()
        self._init_options = self._app.create_initialization_options()

    def _register_tools(self) -> None:
        app = self._app

        tools: list[Tool] = [
            Tool(
                name="health.ping",
                description="Health check with environment snapshot.",
                inputSchema={"type": "object"},
            ),
            Tool(
                name="db.info",
                description="Inspect configured VAST/DB environment state.",
                inputSchema={"type": "object"},
            ),
            Tool(
                name="catalog.show",
                description="List schemas, tables, and columns visible to this VAST instance.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "schema": {"type": "string", "description": "Optional schema name to filter."},
                        "table": {"type": "string", "description": "Optional table name to expand columns."},
                    },
                },
            ),
            Tool(
                name="query.read",
                description="Execute a read-only SQL statement via the VAST guardrails.",
                inputSchema={
                    "type": "object",
                    "required": ["sql"],
                    "properties": {
                        "sql": {"type": "string", "description": "Read-only SQL to execute."},
                        "params": {
                            "type": "object",
                            "description": "Optional SQL parameters (e.g., {\"limit\": 10}).",
                        },
                    },
                },
            ),
            Tool(
                name="resolver.run",
                description="Run the natural language to SQL resolver in read-only mode.",
                inputSchema={
                    "type": "object",
                    "required": ["prompt"],
                    "properties": {
                        "prompt": {"type": "string", "description": "Natural language request."},
                        "params": {
                            "type": "object",
                            "description": "Optional parameter hints for the resolver (e.g., limit values).",
                        },
                    },
                },
            ),
            Tool(
                name="events.breadcrumbs",
                description="Return recent audit events that include breadcrumb metadata.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000,
                            "default": 50,
                            "description": "Maximum number of audit events to return.",
                        }
                    },
                },
            ),
        ]

        @app.list_tools()
        async def list_tools() -> list[Tool]:
            return tools

        @app.call_tool()
        async def call_tool(
            name: str,
            arguments: Dict[str, Any],
            _request: CallToolRequest | None = None,
        ) -> Tuple[Sequence[TextContent], Dict[str, Any]]:
            arguments = _ensure_dict(arguments, "arguments")

            if name == "health.ping":
                payload = {
                    "ok": True,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
                result = _tool_response(payload)
                return result.content, result.structuredContent or {}

            if name == "db.info":
                info = environment_status()
                result = _tool_response(info)
                return result.content, result.structuredContent or {}

            if name == "catalog.show":
                schema, table = _normalize_table_arguments(arguments)
                if table:
                    cols = await _run_sync(service_columns, schema or "public", table)
                    payload = {
                        "schema": schema or "public",
                        "table": table,
                        "columns": cols,
                    }
                else:
                    tables = await _run_sync(service_tables)
                    if schema:
                        tables = [t for t in tables if t.get("table_schema") == schema]
                    payload = {"tables": tables}
                result = _tool_response(payload)
                return result.content, result.structuredContent or {}

            if name == "query.read":
                sql, params = _normalize_query_arguments(arguments)
                result = await _run_sync(
                    service_execute_sql,
                    sql,
                    params,
                    False,
                    False,
                )
                payload = _format_sql_result(result)
                result = _tool_response(payload)
                return result.content, result.structuredContent or {}

            if name == "resolver.run":
                prompt, params = _normalize_resolver_arguments(arguments)
                result = await _run_sync(
                    service_plan_and_execute,
                    prompt,
                    params,
                    False,
                    False,
                )
                payload = _format_plan_result(result)
                result = _tool_response(payload)
                return result.content, result.structuredContent or {}

            if name == "events.breadcrumbs":
                limit = arguments.get("limit", 50)
                if not isinstance(limit, int):
                    raise McpError("'limit' must be an integer if provided")
                events = await _run_sync(_load_breadcrumb_events, limit)
                result = _tool_response({"events": events})
                return result.content, result.structuredContent or {}

            raise McpError(f"Unknown tool: {name}")

    async def _run_stdio(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self._app.run(read_stream, write_stream, self._init_options)

    async def _run_websocket(self, host: str, port: int) -> None:
        from starlette.applications import Starlette
        from starlette.routing import WebSocketRoute

        async def websocket_endpoint(websocket):
            async with websocket_server(websocket.scope, websocket.receive, websocket.send) as streams:
                await self._app.run(streams[0], streams[1], self._init_options)

        app = Starlette(debug=False, routes=[WebSocketRoute("/ws", websocket_endpoint)])

        _install_uvloop()
        import uvicorn

        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()

    def run_stdio(self) -> None:
        logger.info("Starting vast-mcp on stdio transport")
        anyio.run(self._run_stdio)

    def run(self, host: str, port: int) -> None:
        logger.info("Starting vast-mcp websocket transport on %s:%s", host, port)
        anyio.run(self._run_websocket, host, port)


def _install_uvloop() -> None:
    try:
        import uvloop

        uvloop.install()
    except ImportError:  # pragma: no cover - optional dependency
        logger.debug("uvloop not available; using default asyncio loop")


def create_server() -> VastMCPServer:
    return VastMCPServer()


def main(argv: Optional[Iterable[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="VAST MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "ws"),
        default="stdio",
        help="Transport to use: stdio or ws",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Websocket host (ws mode)")
    parser.add_argument(
        "--port",
        default=8901,
        type=int,
        help="Websocket port (ws mode)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    server = create_server()
    if args.transport == "stdio":
        server.run_stdio()
    else:
        server.run(args.host, args.port)


if __name__ == "__main__":
    main()
