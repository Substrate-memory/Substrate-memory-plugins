"""Behavior tests for the thin native Hermes plugin."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
import time
import urllib.request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from substrate import contract
from substrate import plugin


class Response:
    def __init__(self, value, status=200):
        self.raw = json.dumps(value).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.raw if size < 0 else self.raw[:size]


def test_pre_llm_call_posts_exact_contract_request(monkeypatch):
    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.method
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout
        seen["auth"] = request.get_header("Authorization")
        return Response(
            {
                "contract_version": 1,
                "session_id": "session-1",
                "turn": 1,
                "block": "<memory-context>\n- Keep it private. [m:44a1b02e]\n</memory-context>",
                "handles": ["m:44a1b02e"],
                "tail_handles": [],
                "brief_version": 2,
                "latency_ms": 12.5,
                "empty_reason": "",
                "ignored_backend_debug": "drop me",
            }
        )

    monkeypatch.setenv("SUBSTRATE_API_URL", "https://memory.example/")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "secret")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current question"},
    ]
    result = plugin.pre_llm_call(
        "session-1",
        "current question",
        history,
        turn_id="turn-2",
        platform="cli",
        chat_type="direct",
        sender_id="person-1",
        agent_identity="Hermes",
        agent_context="default",
        parent_session_id="parent-1",
        injected_handles=["m:12345678"],
        cited_handles=["p:abcdef12"],
    )
    assert result == {"context": "<memory-context>\n- Keep it private. [m:44a1b02e]\n</memory-context>"}
    assert seen == {
        "url": "https://memory.example/api/v1/memory/turn-context",
        "method": "POST",
        "body": {
            "contract_version": 1,
            "session_id": "session-1",
            "turn_id": "turn-2",
            "turn": 1,
            "platform": "cli",
            "chat_type": "direct",
            "sender_id": "person-1",
            "agent_identity": "Hermes",
            "agent_context": "default",
            "parent_session_id": "parent-1",
            "message": "current question",
            "recent_turns": [{"user": "old question", "assistant": "old answer"}],
            "injected_handles": ["m:12345678"],
            "cited_handles": ["p:abcdef12"],
            "deadline_ms": 500,
        },
        "timeout": 0.5,
        "auth": "Bearer secret",
    }


def test_pre_llm_rejects_validator_failure_and_every_error(monkeypatch):
    valid = {
        "contract_version": 1,
        "session_id": "s",
        "turn": 0,
        "block": "unsafe",
        "handles": [],
        "tail_handles": [],
        "brief_version": 0,
        "latency_ms": 1,
        "empty_reason": "",
    }
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.setattr(plugin.SubstrateClient, "post_json", lambda *a, **k: valid)
    monkeypatch.setattr(
        contract,
        "validate_turn_context",
        lambda value: (_ for _ in ()).throw(contract.ContractError("invalid_response")),
    )
    assert plugin.pre_llm_call("s", "q", [], turn_id="t") is None

    monkeypatch.setattr(
        plugin.SubstrateClient,
        "post_json",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("backend detail must not escape")),
    )
    assert plugin.pre_llm_call("s", "q", [], turn_id="t") is None


def test_capture_envelope_has_full_completed_messages_and_validates():
    history = [
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "terminal", "arguments": '{"token":"hide"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": "token=hide"},
        {"role": "assistant", "content": "old visible answer"},
    ]
    envelope = plugin._capture_envelope(
        "first",
        "final visible answer",
        session_id="session",
        messages=history,
        turn_id="turn",
        sender_id="owner",
    )
    assert envelope is not None
    messages = envelope["payload"]["messages"]
    assert [row["role"] for row in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[-1]["content"] == "final visible answer"
    assert messages[1]["tool_calls"][0]["args"]["token"] == "[REDACTED]"
    assert "hide" not in messages[2]["content"]
    contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])


def test_sync_turn_is_nonblocking_and_uses_one_daemon_worker(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    captured = []

    def blocked_post(self, path, body, **kwargs):
        captured.append((path, body, kwargs))
        started.set()
        release.wait(1)
        return {}

    worker = plugin._CaptureWorker()
    monkeypatch.setattr(plugin, "_CAPTURE_WORKER", worker)
    monkeypatch.setattr(plugin.SubstrateClient, "post_json", blocked_post)
    before = time.monotonic()
    plugin.sync_turn(
        "hello",
        "world",
        session_id="s",
        messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}],
        turn_id="t",
    )
    assert time.monotonic() - before < 0.1
    assert started.wait(0.5)
    assert worker._thread is not None and worker._thread.daemon
    first_thread = worker._thread
    plugin.sync_turn("again", "done", session_id="s", messages=[], turn_id="t2")
    assert worker._thread is first_thread
    release.set()
    assert captured[0][0] == "/api/v1/ledger/events"
    assert captured[0][2]["idempotency_key"] == captured[0][1]["event_id"]


def test_three_tools_validate_defaults_shape_and_bound(monkeypatch):
    calls = []

    def post(self, path, body, **kwargs):
        calls.append((path, body))
        if path.endswith("search"):
            return {
                "contract_version": 1,
                "results": [
                    {"handle": "m:12345678", "text": "fact", "score": 0.8, "kind": "fact", "markers": []},
                    {"handle": "bad", "text": "drop"},
                ],
                "debug": "drop",
            }
        if path.endswith("expand"):
            return {
                "contract_version": 1,
                "handle": "p:abcdef12",
                "kind": "page",
                "title": "Page",
                "abstract": "Summary",
                "markdown": "body",
                "debug": "drop",
            }
        return {"contract_version": 1, "excerpts": [{"text": "evidence"}], "raw": "x" * 100_000}

    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.setattr(plugin.SubstrateClient, "post_json", post)
    search = json.loads(plugin.memory_search({"query": "planned delete"}))
    expand = json.loads(plugin.memory_expand({"handle": "p:abcdef12"}))
    evidence_text = plugin.memory_evidence({"handle": "m:12345678"})
    evidence = json.loads(evidence_text)
    assert search == {
        "contract_version": 1,
        "results": [{"handle": "m:12345678", "kind": "fact", "markers": [], "score": 0.8, "text": "fact"}],
    }
    assert expand["markdown"] == "body" and "debug" not in expand
    assert evidence["excerpts"] == [{"text": "evidence"}]
    assert len(evidence_text.encode()) <= contract.LIMITS["max_tool_result_bytes"]
    assert calls == [
        ("/api/v1/memory/search", {"query": "planned delete", "limit": 8}),
        ("/api/v1/memory/expand", {"handle": "p:abcdef12"}),
        ("/api/v1/memory/evidence", {"handle": "m:12345678", "raw": False, "limit": 5}),
    ]


@pytest.mark.parametrize(
    "callback,args",
    [
        (plugin.memory_search, {"query": "", "limit": 8}),
        (plugin.memory_search, {"query": "q", "limit": 21}),
        (plugin.memory_expand, {"handle": "M:12345678"}),
        (plugin.memory_evidence, {"handle": "m:12345678", "raw": "yes"}),
    ],
)
def test_tools_fail_closed_on_invalid_input(callback, args):
    assert json.loads(callback(args)) == {"error": "invalid_request"}


def test_tool_backend_failure_has_no_free_detail(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.setattr(
        plugin.SubstrateClient,
        "post_json",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret backend stack")),
    )
    result = plugin.memory_search({"query": "q"})
    assert json.loads(result) == {"error": "transport_error"}
    assert "secret" not in result and "stack" not in result


def test_register_matches_native_hermes_context_and_has_no_side_effect(monkeypatch):
    class Context:
        def __init__(self):
            self.hooks = {}
            self.tools = {}
            self.prompts = {}

        def register_hook(self, name, callback):
            self.hooks[name] = callback

        def register_tool(self, *, name, toolset, schema, handler, **kwargs):
            self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

        def register_system_prompt_section(self, id, content, *, position, max_chars):
            self.prompts[id] = (content, position, max_chars)

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("register performed network I/O")),
    )
    ctx = Context()
    thread_before = plugin._CAPTURE_WORKER._thread
    plugin.register(ctx)
    assert set(ctx.hooks) == {"pre_llm_call", "post_llm_call"}
    assert set(ctx.tools) == {"memory_search", "memory_expand", "memory_evidence"}
    assert ctx.prompts == {
        "substrate.memory": (plugin.STATIC_MEMORY_PROMPT, "after_memory", 2000)
    }
    assert plugin._CAPTURE_WORKER._thread is thread_before
