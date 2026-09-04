#!/usr/bin/env python3
"""Substrate memory MCP stdio server for Claude Code. Standard library only.

Exposes the same 3 tools as the Hermes plugin with identical names,
descriptions, JSON schemas, and bounded JSON string results:

- ``memory_search``  -> POST /api/v1/memory/search
- ``memory_expand``  -> POST /api/v1/memory/expand
- ``memory_evidence`` -> POST /api/v1/memory/evidence

Transport is MCP over stdio (newline-delimited JSON-RPC). Every failure is a
bounded ``{"error": <category>}`` tool result; the server never prints a
credential and never disables TLS verification.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime  # noqa: E402

SERVER_NAME = "substrate-memory"
SERVER_VERSION = "0.4.0"

_TOOL_SCHEMAS = (
    runtime.MEMORY_SEARCH_SCHEMA,
    runtime.MEMORY_EXPAND_SCHEMA,
    runtime.MEMORY_EVIDENCE_SCHEMA,
)

_HANDLERS = {
    "memory_search": runtime.memory_search,
    "memory_expand": runtime.memory_expand,
    "memory_evidence": runtime.memory_evidence,
}


def _tool_def(schema: dict) -> dict:
    return {
        "name": schema["name"],
        "description": schema["description"],
        "inputSchema": schema["parameters"],
    }


def _handle(message: object) -> dict | None:
    """Handle one JSON-RPC message. Returns None for notifications."""
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "invalid_request"}}
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if method == "initialize":
        requested = params.get("protocolVersion", "2024-11-05")
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"protocolVersion": requested,
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": SERVER_NAME,
                                          "version": SERVER_VERSION}}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": [_tool_def(schema) for schema in _TOOL_SCHEMAS]}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": []}}
    if method == "tools/call":
        name = params.get("name")
        handler = _HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602, "message": "unknown_tool"}}
        args = params.get("arguments")
        try:
            text = handler(args if isinstance(args, dict) else {})
        except Exception:
            text = json.dumps({"error": "transport_error"})
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}]}}
    if msg_id is None:
        return None  # Unknown notification; stay silent.
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": "method_not_found"}}


def serve(stdin=None, stdout=None) -> int:
    """Serve one stdio session. Split for offline tests."""
    stream_in = stdin or sys.stdin
    stream_out = stdout or sys.stdout
    for line in stream_in:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, UnicodeError):
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "parse_error"}}
            stream_out.write(json.dumps(response) + "\n")
            stream_out.flush()
            continue
        try:
            response = _handle(message)
        except Exception:
            response = {"jsonrpc": "2.0",
                        "id": message.get("id") if isinstance(message, dict) else None,
                        "error": {"code": -32603, "message": "internal_error"}}
        if response is not None:
            stream_out.write(json.dumps(response) + "\n")
            stream_out.flush()
    return 0


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
