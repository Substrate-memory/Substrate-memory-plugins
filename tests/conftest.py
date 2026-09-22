"""Shared fixtures for the Hermes reference plugin tests."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERMES_SRC = str(Path(__file__).resolve().parent.parent / "plugins" / "substrate-hermes" / "src")
if HERMES_SRC not in sys.path:
    sys.path.insert(0, HERMES_SRC)

# --- Hermes reference plugin fixtures (ported from the plugin source) ---
# No module patching: the real ``substrate.spool`` interface is used
# directly. ``FakeSpool`` is a plain in-memory double for fast unit tests;
# ``real_spool`` wires a real ``Spool`` in a temp dir (never the live DB);
# ``fake_mcp`` is an offline MCP JSON-RPC stub (no live API).


class FakeSpool:
    """In-memory stand-in: records enqueues, never sends."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.start_calls: list = []
        self._next = 0

    def enqueue(self, envelope: dict, *, priority: int, kind: str,
                capture_origin: str) -> str:
        self._next += 1
        item_id = f"item-{self._next}"
        self.items.append({
            "item_id": item_id,
            "envelope": envelope,
            "priority": priority,
            "kind": kind,
            "capture_origin": capture_origin,
        })
        return item_id

    def start(self, client) -> None:
        self.start_calls.append(client)

    def stop(self, timeout: float = 5.0) -> None:
        pass

    def counters(self) -> dict:
        return {"queued": len(self.items)}

    def reset(self) -> None:
        self.items.clear()
        self.start_calls.clear()


@pytest.fixture
def fake_spool(monkeypatch):
    from substrate import plugin as _plugin

    fake = FakeSpool()
    monkeypatch.setattr(_plugin, "get_spool", lambda: fake)
    return fake


@pytest.fixture
def clean_session_state():
    from substrate import plugin as _plugin

    _plugin._SESSION_STATE["active"] = None
    _plugin._SESSION_STATE["high_water"].clear()
    _plugin._SUBAGENT_PARENTS.clear()
    yield
    _plugin._SESSION_STATE["active"] = None
    _plugin._SESSION_STATE["high_water"].clear()
    _plugin._SUBAGENT_PARENTS.clear()


@pytest.fixture(autouse=True)
def _fresh_mcp_init_cache():
    """Never reuse a verified initialize across tests."""
    from substrate import client as _client

    _client.reset_init_cache()
    yield
    _client.reset_init_cache()


def _spool_base(tmp_path):
    """Prefer tmpfs to avoid disk-journal stalls; disk tests opt out."""
    shm = Path("/dev/shm")
    try:
        if shm.is_dir():
            probe = shm / f"substrate-probe-{os.getpid()}"
            probe.mkdir(exist_ok=True)
            probe.rmdir()
            return Path(
                tempfile.mkdtemp(prefix="substrate-test-spool-", dir=str(shm))
            )
    except OSError:
        pass
    return tmp_path


@pytest.fixture
def real_spool(tmp_path):
    """A real Spool in a temp dir (tmpfs when available), as the singleton."""
    import substrate.spool as spool_module

    spool_module.configure_spool(_spool_base(tmp_path) / "spool")
    yield spool_module.get_spool()
    spool_module.reset_spool()


# ---------------------------------------------------------------------------
# Offline fake MCP server (test-only stand-in, not the real server)
#
# Speaks MCP JSON-RPC on POST /mcp: ``initialize`` (verifying the
# substrate-memory identity), ``tools/list`` (all 12 contract tools), and
# ``tools/call`` for the memory tools. Tool arguments are validated as
# closed schemas per the contract: an unknown argument or a missing
# required field fails the call with ``isError`` + ``invalid_request``.
# Accepts plain-JSON bodies; with ``server.sse = True`` answers wrap the
# JSON-RPC envelope in SSE ``data:`` lines so both client parsers run.
# ---------------------------------------------------------------------------

TOOL_ARGS = {
    "memory_search": ({"query"}, {"query", "kinds", "limit", "share"}),
    "memory_expand": ({"handle"}, {"handle", "share"}),
    "memory_evidence": ({"handle"}, {"handle", "raw", "limit", "share"}),
    "memory_shares": (set(), set()),
    "memory_turn_context": (
        {"session_id", "prompt", "platform"},
        {"session_id", "prompt", "platform", "turn_id", "agent_context",
         "parent_session_id"},
    ),
    "memory_import_status": (set(), {"session_id", "batch_id"}),
    "memory_remember": (
        {"operation_id", "text"}, {"operation_id", "text", "about", "durability"},
    ),
    "memory_forget": (
        {"operation_id", "handle"}, {"operation_id", "handle", "reason"},
    ),
    "memory_capture_tool": (
        {"session_id", "tool_use_id", "tool_name", "platform"},
        {"session_id", "tool_use_id", "tool_name", "tool_input",
         "tool_response", "platform", "turn_id", "agent_context",
         "parent_session_id"},
    ),
    "memory_capture_turn": (
        {"session_id", "assistant_message", "platform"},
        {"session_id", "assistant_message", "platform", "turn_id",
         "agent_context", "agent_id", "parent_session_id"},
    ),
    "memory_session_boundary": (
        {"session_id", "boundary", "platform"},
        {"session_id", "boundary", "platform", "next_session_id",
         "parent_session_id", "reason"},
    ),
    "memory_import": ({"items"}, {"items", "batch_id"}),
}


def _tool_error(message_id, category, hint="bad arguments"):
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps(
                {"contract_version": 2, "error": category, "hint": hint})}],
            "structuredContent": {
                "contract_version": 2, "error": category, "hint": hint},
            "isError": True,
        },
    }


def _tool_ok(message_id, structured, text=None):
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {
            "content": [{"type": "text", "text": text if text is not None
                         else json.dumps(structured)}],
            "structuredContent": structured,
            "isError": False,
        },
    }


class _MCPHandler(BaseHTTPRequestHandler):
    server_version = "FakeMCP/2"

    def log_message(self, *args):
        pass

    def _send_json(self, status, envelope):
        raw = json.dumps(envelope).encode()
        if getattr(self.server, "sse", False) and status == 200:
            raw = b"data: " + raw + b"\n\n"
            content_type = "text/event-stream"
        else:
            content_type = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        self.server.hits += 1  # type: ignore[attr-defined]
        if self.path != "/mcp":
            self._send_json(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, ValueError):
            body = {}
        method = body.get("method") if isinstance(body, dict) else None
        params = body.get("params") if isinstance(body, dict) else None
        message_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(params, dict):
            params = {}
        self.server.calls.append({  # type: ignore[attr-defined]
            "method": method,
            "tool": params.get("name") if method == "tools/call" else None,
            "arguments": params.get("arguments") if method == "tools/call" else None,
            "auth": self.headers.get("Authorization"),
            "accept": self.headers.get("Accept"),
        })
        if getattr(self.server, "http_status", 200) != 200:
            self._send_json(self.server.http_status, {"error": "internal"})
            return
        if getattr(self.server, "delay", 0.0):
            time.sleep(self.server.delay)
        if self.headers.get("Authorization") != f"Bearer {self.server.token}":
            self._send_json(200, {
                "jsonrpc": "2.0", "id": message_id,
                "error": {"code": -32000, "message": "unauthorized"},
            })
            return
        if method == "initialize":
            self._send_json(200, {
                "jsonrpc": "2.0", "id": message_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.server.server_name,
                                   "version": "test"},
                    "instructions": "substrate-mcp-contract/2 test memory",
                },
            })
            return
        if method == "tools/list":
            self._send_json(200, {
                "jsonrpc": "2.0", "id": message_id,
                "result": {"tools": [{"name": name} for name in self.server.tool_names]},
            })
            return
        if method != "tools/call":
            self._send_json(200, {
                "jsonrpc": "2.0", "id": message_id,
                "error": {"code": -32601, "message": "method not found"},
            })
            return
        name = params.get("name")
        arguments = params.get("arguments")
        if name not in TOOL_ARGS or not isinstance(arguments, dict):
            self._send_json(200, _tool_error(message_id, "invalid_request"))
            return
        required, allowed = TOOL_ARGS[name]
        if not required.issubset(arguments) or set(arguments) - allowed:
            self._send_json(200, _tool_error(message_id, "invalid_request"))
            return
        self._dispatch_tool(message_id, name, arguments)

    def _dispatch_tool(self, message_id, name, arguments):
        server = self.server
        if name == "memory_search":
            query = arguments.get("query")
            if not isinstance(query, str) or not query:
                self._send_json(200, _tool_error(message_id, "invalid_request"))
                return
            self._send_json(200, _tool_ok(message_id, {
                "contract_version": 2, "results": list(server.search_results)}))
            return
        if name == "memory_turn_context":
            prompt = arguments.get("prompt")
            if not isinstance(prompt, str):
                self._send_json(200, _tool_error(message_id, "invalid_request"))
                return
            self._send_json(200, _tool_ok(
                message_id,
                {
                    "contract_version": 2,
                    "session_id": arguments.get("session_id"),
                    "turn": 0,
                    "block": str(server.turn_block),
                    "handles": [],
                    "missing_turns": 0,
                    "brief_version": 0,
                    "latency_ms": 1,
                    "empty_reason": "no_candidates",
                },
                str(server.turn_block),
            ))
            return
        if name == "memory_expand":
            handle = arguments.get("handle")
            self._send_json(200, _tool_ok(message_id, {
                "contract_version": 2, "handle": handle, "kind": "fact",
                "title": "Fact", "abstract": "Summary",
                "markdown": "x" * 100 if getattr(server, "long_markdown", False) else "body"}))
            return
        if name == "memory_evidence":
            self._send_json(200, _tool_ok(message_id, {
                "contract_version": 2,
                "excerpts": [{"text": "evidence"}],
                "raw": "x" * 100_000 if getattr(server, "long_raw", False) else "raw"}))
            return
        if name == "memory_remember":
            self._send_json(200, _tool_ok(message_id, {
                "contract_version": 2, "handle": str(server.remember_handle)}))
            return
        if name == "memory_forget":
            self._send_json(200, _tool_ok(message_id, {
                "contract_version": 2, "handle": str(arguments.get("handle"))}))
            return
        if name == "memory_import":
            items = arguments.get("items")
            if not isinstance(items, list) or not items or len(items) > 64:
                self._send_json(200, _tool_error(message_id, "invalid_request"))
                return
            mode = server.import_mode
            results = []
            for index, item in enumerate(items):
                event_id = item.get("event_id") if isinstance(item, dict) else None
                if callable(mode):
                    action = mode(index, item)
                elif mode == "reject-all":
                    action = "rejected"
                else:
                    action = str(mode)
                entry = {"index": index, "action": action}
                if event_id is not None:
                    entry["event_id"] = event_id
                if action == "rejected":
                    entry["error"] = "invalid_request"
                results.append(entry)
            accepted = sum(1 for entry in results if entry["action"] != "rejected")
            self._send_json(200, _tool_ok(message_id, {
                "contract_version": 2, "batch_id": "a" * 16,
                "accepted": accepted, "rejected": len(results) - accepted,
                "results": results}))
            return
        self._send_json(200, _tool_ok(message_id, {"contract_version": 2}))


@pytest.fixture
def fake_mcp():
    """Offline MCP stub. Tune via server attrs: ``mode``/``sse``/``delay``/
    ``http_status``/``tool_names``/``server_name``/``search_results``/
    ``turn_block``/``remember_handle``/``import_mode``/``long_markdown``/
    ``long_raw``. ``server.calls`` records every decoded JSON-RPC call; for
    ``tools/call``, ``server.calls`` entries carry ``tool`` + ``arguments``.
    ``server.hits`` counts every HTTP POST (including error-status answers).
    """
    from substrate import contract as _contract

    server = ThreadingHTTPServer(("127.0.0.1", 0), _MCPHandler)
    server.daemon_threads = True
    server.token = "k"
    server.server_name = "substrate-memory"
    server.tool_names = list(_contract.MCP_TOOL_LIST)
    server.search_results = []
    server.turn_block = ""
    server.remember_handle = "m:44a1b02e"
    server.import_mode = "stored"
    server.sse = False
    server.delay = 0.0
    server.http_status = 200
    server.long_markdown = False
    server.long_raw = False
    server.calls = []
    server.hits = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5.0)
    server.server_close()


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()
