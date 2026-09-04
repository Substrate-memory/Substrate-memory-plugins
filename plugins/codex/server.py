#!/usr/bin/env python3
"""Substrate memory MCP stdio server for OpenAI Codex CLI.

Standard library only. Exposes the exact Hermes tool surface
(``memory_search``, ``memory_expand``, ``memory_evidence`` with identical
names, descriptions, JSON schemas, and bounded JSON string results) over
MCP stdio (newline-delimited JSON-RPC on stdin/stdout).

Register with Codex (see README.md)::

    codex mcp add substrate -- python3 /path/to/plugins/codex/server.py

No secrets are ever written to stdout or stderr: only tool names, byte
sizes, and bounded error categories are logged, and only to stderr.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from substrate_core import runtime  # noqa: E402

SERVER_NAME = "substrate-memory"
SERVER_VERSION = "0.4.0"

_TOOL_ORDER = ("memory_search", "memory_expand", "memory_evidence")
_TOOL_SCHEMAS = {
    "memory_search": runtime.MEMORY_SEARCH_SCHEMA,
    "memory_expand": runtime.MEMORY_EXPAND_SCHEMA,
    "memory_evidence": runtime.MEMORY_EVIDENCE_SCHEMA,
}
_TOOL_HANDLERS = {
    "memory_search": runtime.memory_search,
    "memory_expand": runtime.memory_expand,
    "memory_evidence": runtime.memory_evidence,
}


def _mcp_tool(name: str) -> dict[str, Any]:
    schema = _TOOL_SCHEMAS[name]
    return {
        "name": schema["name"],
        "description": schema["description"],
        "inputSchema": schema["parameters"],
    }


def _log(message: str) -> None:
    sys.stderr.write(f"[substrate-memory] {message}\n")
    sys.stderr.flush()


def _respond(message_id: Any, result: Any) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


def _respond_error(message_id: Any, code: int, message: str) -> None:
    sys.stdout.write(
        json.dumps(
            {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}
        )
        + "\n"
    )
    sys.stdout.flush()


def _handle(message: Any) -> bool:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return True
    method = message.get("method")
    message_id = message.get("id")
    if method == "initialize":
        _respond(
            message_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    elif method in ("notifications/initialized", "notifications/cancelled"):
        pass
    elif method == "ping":
        _respond(message_id, {})
    elif method == "tools/list":
        _respond(message_id, {"tools": [_mcp_tool(name) for name in _TOOL_ORDER]})
    elif method == "tools/call":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        name = params.get("name")
        handler = _TOOL_HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            _respond_error(message_id, -32602, f"unknown tool: {name}")
            return True
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        try:
            text = handler(arguments)
        except Exception:
            text = json.dumps({"error": "transport_error"})
        if not isinstance(text, str):
            text = json.dumps({"error": "transport_error"})
        _log(f"tool {name} -> {len(text.encode('utf-8'))} bytes")
        _respond(message_id, {"content": [{"type": "text", "text": text}]})
    else:
        if message_id is not None:
            _respond_error(message_id, -32601, f"method not found: {method}")
    return True


def main() -> int:
    _log(f"starting {SERVER_NAME} {SERVER_VERSION} (contract v1)")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(message)
        except Exception as exc:
            _log(f"handler error: {type(exc).__name__}")
            message_id = message.get("id") if isinstance(message, dict) else None
            if message_id is not None:
                _respond_error(message_id, -32603, "internal error")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
