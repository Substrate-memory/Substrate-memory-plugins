#!/usr/bin/env python3
"""Substrate memory MCP stdio server for Grok Bot surfaces.

Standard library only. Speaks newline-delimited JSON-RPC 2.0 over stdio
(the MCP stdio transport) and exposes the three Substrate memory tools
with schemas identical to the Hermes reference plugin:

- ``memory_search``   — search Substrate memory
- ``memory_expand``   — expand a memory/page handle into bounded detail
- ``memory_evidence`` — evidence excerpts for a memory handle

Pre-turn context, completed-turn capture, and session markers live in
``bridge.py``: MCP has no hook channel, so the harness (or the install
snippets in ``instructions.md``) calls the bridge around each turn while
this server serves the tool calls the model makes.

Onboarding: when no credential exists, tool calls return an
``authorization_required`` payload containing the clickable
``verification_uri_complete`` browser approval URL (RFC 8628). The harness
must show that exact URL to the user. Never ask for a pasted key.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from substrate_core import runtime

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "substrate-memory"
SERVER_VERSION = "0.3.0"


def _tool_entries() -> list[dict[str, Any]]:
    entries = []
    for schema in (
        runtime.MEMORY_SEARCH_SCHEMA,
        runtime.MEMORY_EXPAND_SCHEMA,
        runtime.MEMORY_EVIDENCE_SCHEMA,
    ):
        entries.append(
            {
                "name": schema["name"],
                "description": schema["description"],
                "inputSchema": schema["parameters"],
            }
        )
    return entries


_HANDLERS = {
    "memory_search": runtime.memory_search,
    "memory_expand": runtime.memory_expand,
    "memory_evidence": runtime.memory_evidence,
}


def _call_tool(name: str, args: Any) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"content": [{"type": "text", "text": '{"error":"invalid_request"}'}], "isError": True}
    if args is None:
        args = {}
    try:
        text = handler(args if isinstance(args, dict) else {})
    except Exception:
        text = '{"error":"transport_error"}'
    try:
        payload = json.loads(text)
        is_error = isinstance(payload, dict) and "error" in payload
    except (ValueError, TypeError):
        is_error = True
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def handle_message(message: Any) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns None for notifications."""
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _tool_entries()}}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        return {"jsonrpc": "2.0", "id": msg_id, "result": _call_tool(name, args)}
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if msg_id is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Method not found"}}


def serve(stdin: Any = sys.stdin, stdout: Any = sys.stdout) -> int:
    """Serve JSON-RPC over stdio until EOF. Never raises on bad input."""
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
            continue
        try:
            response = handle_message(message)
        except Exception:
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id") if isinstance(message, dict) else None,
                "error": {"code": -32603, "message": "Internal error"},
            }
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
