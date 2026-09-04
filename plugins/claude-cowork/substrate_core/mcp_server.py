#!/usr/bin/env python3
"""Substrate memory MCP stdio server for Claude Cowork (stdlib only).

Exposes the same three tools as the Hermes reference plugin with identical
names, descriptions, JSON schemas, bounded JSON-string results, and identical
error strings: ``memory_search``, ``memory_expand``, ``memory_evidence``.

Protocol: newline-delimited JSON-RPC 2.0 on stdin/stdout (MCP stdio
transport). Methods: ``initialize``, ``notifications/initialized`` (no reply),
``tools/list``, ``tools/call``. Anything else answers ``-32601``.
Ping (``ping``) answers ``{}``. Fail-closed: handler exceptions become
``{"error": ...}`` tool text or JSON-RPC ``-32603``; a token is never printed.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from substrate_core import runtime

SERVER_NAME = "substrate-cowork-memory"
SERVER_VERSION = "0.3.0"


def _tool_defs() -> list[dict]:
    defs = []
    for schema in (
        runtime.MEMORY_SEARCH_SCHEMA,
        runtime.MEMORY_EXPAND_SCHEMA,
        runtime.MEMORY_EVIDENCE_SCHEMA,
    ):
        defs.append(
            {
                "name": schema["name"],
                "description": schema["description"],
                "inputSchema": schema["parameters"],
            }
        )
    return defs


_HANDLERS = {
    "memory_search": runtime.memory_search,
    "memory_expand": runtime.memory_expand,
    "memory_evidence": runtime.memory_evidence,
}


def _error(message_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def handle(message: object) -> dict | None:
    """Handle one JSON-RPC message; return the reply, or None for no reply."""
    if not isinstance(message, dict):
        return _error(None, -32700, "parse error")
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if not isinstance(method, str):
        return _error(message_id, -32600, "invalid request")
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": _tool_defs()}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        handler = _HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            return _error(message_id, -32602, "unknown tool")
        if not isinstance(arguments, dict):
            return _error(message_id, -32602, "invalid params")
        try:
            text = handler(arguments)
        except Exception:
            text = '{"error":"transport_error"}'
        if not isinstance(text, str):
            text = '{"error":"transport_error"}'
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": False},
        }
    return _error(message_id, -32601, "method not found")


def main() -> int:
    # Persistent line loop: the host keeps this process alive and frames one
    # JSON-RPC message per line; reply incrementally, never buffer to EOF.
    stdin = sys.stdin.buffer
    stdout = sys.stdout
    while True:
        raw = stdin.readline()
        if not raw:
            return 0
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        try:
            reply = handle(message)
        except Exception:
            reply = _error(message.get("id") if isinstance(message, dict) else None,
                           -32603, "internal error")
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
