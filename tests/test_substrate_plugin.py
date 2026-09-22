"""Behavior tests for the thin native Hermes plugin."""

from __future__ import annotations

import json
import time
import urllib.request

import pytest

from substrate import contract
from substrate import plugin
from substrate.spool import PRIORITY_EXPLICIT, PRIORITY_LIVE


FORGET_SENTENCE = (
    "marks a memory as no longer true; it stays in the record, keeps its evidence, "
    "and can be revived by later information."
)


def _mcp_call_double(monkeypatch, handler):
    """Stub SubstrateClient.call_tool with handler(tool, args, kwargs)."""
    from substrate.client import SubstrateClient

    def call(self, name, arguments, **kwargs):
        return handler(name, arguments, kwargs)

    monkeypatch.setattr(SubstrateClient, "call_tool", call)


def test_pre_llm_call_posts_exact_contract_request(monkeypatch):
    seen = {}

    def handler(name, arguments, kwargs):
        seen["tool"] = name
        seen["body"] = arguments
        seen["timeout"] = kwargs.get("timeout")
        return (
            {
                "contract_version": 2,
                "session_id": "session-1",
                "turn": 0,
                "block": "<memory-context>\n- Keep it private. [m:44a1b02e]\n</memory-context>",
                "handles": ["m:44a1b02e"],
                "missing_turns": 0,
                "brief_version": 2,
                "latency_ms": 12.5,
                "empty_reason": "",
                "ignored_backend_debug": "drop me",
            },
            "<memory-context>\n- Keep it private. [m:44a1b02e]\n</memory-context>",
        )

    monkeypatch.setenv("SUBSTRATE_API_URL", "https://memory.example/")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "secret")
    _mcp_call_double(monkeypatch, handler)
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
    # The v2 contract takes only session/prompt/platform (+ optional ids):
    # no history, turn number, or identity fields travel on the wire.
    assert seen == {
        "tool": "memory_turn_context",
        "body": {
            "session_id": "session-1",
            "prompt": "current question",
            "platform": "cli",
            "turn_id": "turn-2",
            "parent_session_id": "parent-1",
        },
        "timeout": 0.5,
    }


def test_pre_llm_call_injects_block_only_and_ignores_sync_line(monkeypatch):
    """The [substrate] sync line is for spool-less MCP hosts; Hermes ignores it."""
    structured = {
        "contract_version": 2,
        "session_id": "s",
        "turn": 3,
        "block": "<memory-context>\n- fact\n</memory-context>",
        "handles": [],
        "missing_turns": 2,
        "brief_version": 0,
        "latency_ms": 1,
        "empty_reason": "",
    }
    text_with_sync = (
        "<memory-context>\n- fact\n</memory-context>\n"
        "[substrate] 2 earlier turn(s) of this session are not saved yet. "
        "Run the Substrate sync command."
    )
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    _mcp_call_double(monkeypatch, lambda *a: (structured, text_with_sync))
    assert plugin.pre_llm_call("s", "q", [], turn_id="t") == {
        "context": "<memory-context>\n- fact\n</memory-context>"
    }
    _mcp_call_double(monkeypatch, lambda *a: ({**structured, "block": ""}, ""))
    assert plugin.pre_llm_call("s", "q", [], turn_id="t") is None


def test_pre_llm_rejects_validator_failure_and_every_error(monkeypatch):
    valid = {
        "contract_version": 2,
        "session_id": "s",
        "turn": 0,
        "block": "unsafe",
        "handles": [],
        "missing_turns": 0,
        "brief_version": 0,
        "latency_ms": 1,
        "empty_reason": "",
    }
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    _mcp_call_double(monkeypatch, lambda *a: (valid, "unsafe"))
    original = contract.validate_mcp_turn_context
    monkeypatch.setattr(
        contract,
        "validate_mcp_turn_context",
        lambda value: (_ for _ in ()).throw(contract.ContractError("invalid_response")),
    )
    assert plugin.pre_llm_call("s", "q", [], turn_id="t") is None
    monkeypatch.setattr(contract, "validate_mcp_turn_context", original)

    _mcp_call_double(
        monkeypatch,
        lambda *a: (_ for _ in ()).throw(RuntimeError("backend detail must not escape")),
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


def test_sync_turn_is_nonblocking_and_enqueues_live(fake_spool, clean_session_state):
    before = time.monotonic()
    plugin.sync_turn(
        "hello",
        "world",
        session_id="s",
        messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}],
        turn_id="t",
    )
    assert time.monotonic() - before < 0.5
    assert len(fake_spool.items) == 1
    item = fake_spool.items[0]
    assert item["priority"] == PRIORITY_LIVE
    assert item["kind"] == "capture_turn"
    assert item["capture_origin"] == "live"
    assert item["envelope"]["kind"] == "capture_turn"
    assert item["envelope"]["session_id"] == "s"
    contract.validate_envelope(item["envelope"], idempotency_key=item["envelope"]["event_id"])
    # Session binding and high-water mark are tracked for boundary events.
    assert plugin._SESSION_STATE["active"] == "s"
    assert plugin._SESSION_STATE["high_water"]["s"] == 2


def test_sync_turn_never_raises(fake_spool, monkeypatch):
    monkeypatch.setattr(
        plugin, "get_spool", lambda: (_ for _ in ()).throw(RuntimeError("spool down"))
    )
    plugin.sync_turn("hello", "world", session_id="s", messages=[])
    plugin.sync_turn("hello", "world", session_id="", messages=[])
    assert fake_spool.items == []


def test_post_llm_call_forwards_host_turn_to_sync_turn(fake_spool, clean_session_state):
    # Host dispatch shape: agent/turn_finalizer.py post_llm_call kwargs.
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    assert plugin.post_llm_call(
        session_id="s",
        task_id="task-1",
        turn_id="turn-9",
        user_message="hello",
        assistant_response="world",
        conversation_history=history,
        model="m",
        platform="cli",
    ) is None
    assert len(fake_spool.items) == 1
    item = fake_spool.items[0]
    assert item["priority"] == PRIORITY_LIVE
    assert item["kind"] == "capture_turn"
    assert item["capture_origin"] == "live"
    envelope = item["envelope"]
    assert envelope["session_id"] == "s"
    assert envelope["payload"]["turn_id"] == "turn-9"
    assert envelope["payload"]["messages"][-1]["content"] == "world"
    contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])


def test_post_llm_call_never_raises(fake_spool, monkeypatch):
    monkeypatch.setattr(
        plugin, "get_spool", lambda: (_ for _ in ()).throw(RuntimeError("spool down"))
    )
    assert plugin.post_llm_call("s", "hi", "there", []) is None


def test_subagent_turns_capture_under_parent(fake_spool, clean_session_state):
    plugin.subagent_start(parent_session_id="parent-1", child_session_id="child-9")
    plugin.post_llm_call(
        "child-9", "hi", "there", [{"role": "user", "content": "hi"}]
    )
    assert fake_spool.items[0]["envelope"]["session_id"] == "parent-1"
    plugin.subagent_stop(parent_session_id="parent-1", child_session_id="child-9")
    plugin.post_llm_call(
        "child-9", "hi", "there", [{"role": "user", "content": "hi"}]
    )
    assert fake_spool.items[1]["envelope"]["session_id"] == "child-9"


def test_subagent_map_is_bounded_and_never_raises(clean_session_state):
    for n in range(plugin._MAX_SUBAGENT_PARENTS + 50):
        plugin.subagent_start(
            parent_session_id="parent", child_session_id=f"child-{n}"
        )
    assert len(plugin._SUBAGENT_PARENTS) <= plugin._MAX_SUBAGENT_PARENTS
    plugin.subagent_start(parent_session_id="", child_session_id="")
    plugin.subagent_stop()


def test_prefetch_returns_empty_and_does_not_capture(fake_spool):
    assert plugin.prefetch(session_id="s") == ""
    assert fake_spool.items == []


def test_on_session_end_is_per_turn_noop(fake_spool, clean_session_state):
    # The host fires on_session_end at the end of every turn, so it must
    # not emit a session_complete boundary here.
    plugin.sync_turn(
        "hello",
        "world",
        session_id="s",
        messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}],
        turn_id="t",
    )
    assert plugin.on_session_end("s", platform="cli") is None
    assert len(fake_spool.items) == 1
    assert plugin._SESSION_STATE["active"] == "s"


def test_on_session_finalize_emits_content_free_boundary(fake_spool, clean_session_state):
    plugin.sync_turn(
        "hello",
        "world",
        session_id="s",
        messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}],
        turn_id="t",
    )
    plugin.on_session_finalize("s", platform="cli", reason="shutdown")
    assert len(fake_spool.items) == 2
    item = fake_spool.items[1]
    assert item["priority"] == PRIORITY_LIVE
    assert item["kind"] == "capture_session"
    assert item["capture_origin"] == "live"
    envelope = item["envelope"]
    assert envelope["kind"] == "capture_session"
    assert envelope["session_id"] == "s"
    assert envelope["payload"]["boundary"] == "end"
    assert envelope["payload"]["session_complete"] is True
    assert envelope["payload"]["message_high_water"] == 2
    assert "messages" not in envelope["payload"]
    assert "text" not in envelope["payload"]
    contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    # Stale session state is invalidated.
    assert plugin._SESSION_STATE["active"] is None
    assert "s" not in plugin._SESSION_STATE["high_water"]


def test_on_session_switch_emits_old_rebinds_and_clears_stale(fake_spool, clean_session_state):
    plugin.sync_turn("hi", "there", session_id="old", messages=[], turn_id="t")
    plugin.on_session_switch("old", "new", platform="cli", chat_type="direct")
    assert len(fake_spool.items) == 2
    envelope = fake_spool.items[1]["envelope"]
    assert envelope["kind"] == "capture_session"
    assert envelope["session_id"] == "old"
    assert envelope["payload"]["boundary"] == "switch"
    assert envelope["payload"]["session_complete"] is True
    assert envelope["payload"]["next_session_id"] == "new"
    contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    assert plugin._SESSION_STATE["active"] == "new"
    assert "old" not in plugin._SESSION_STATE["high_water"]


def test_session_hooks_ignore_empty_session(fake_spool, clean_session_state):
    plugin.on_session_end("")
    plugin.on_session_switch("", "")
    plugin.on_session_finalize("")
    plugin.on_session_reset("")
    assert fake_spool.items == []


def test_on_session_reset_emits_switch_for_tracked_old(fake_spool, clean_session_state):
    plugin.sync_turn("hi", "there", session_id="old", messages=[], turn_id="t")
    # Host fires on_session_reset with the NEW id after rotation.
    plugin.on_session_reset("new", platform="cli")
    assert len(fake_spool.items) == 2
    envelope = fake_spool.items[1]["envelope"]
    assert envelope["kind"] == "capture_session"
    assert envelope["session_id"] == "old"
    assert envelope["payload"]["boundary"] == "switch"
    assert envelope["payload"]["session_complete"] is True
    assert envelope["payload"]["next_session_id"] == "new"
    contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    assert plugin._SESSION_STATE["active"] == "new"


def test_on_session_reset_without_tracked_old_just_binds(fake_spool, clean_session_state):
    plugin.on_session_reset("fresh", platform="cli")
    assert fake_spool.items == []
    assert plugin._SESSION_STATE["active"] == "fresh"


def test_three_tools_validate_defaults_shape_and_bound(monkeypatch):
    calls = []

    def handler(name, arguments, kwargs):
        calls.append((name, arguments))
        if name == "memory_search":
            return (
                {
                    "contract_version": 2,
                    "results": [
                        {"handle": "m:12345678", "text": "fact", "score": 0.8, "kind": "fact", "markers": []},
                        {"handle": "bad", "text": "drop"},
                    ],
                    "debug": "drop",
                },
                "",
            )
        if name == "memory_expand":
            return (
                {
                    "contract_version": 2,
                    "handle": "p:abcdef12",
                    "kind": "page",
                    "title": "Page",
                    "abstract": "Summary",
                    "markdown": "body",
                    "debug": "drop",
                },
                "",
            )
        return (
            {"contract_version": 2, "excerpts": [{"text": "evidence"}], "raw": "x" * 100_000},
            "",
        )

    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    _mcp_call_double(monkeypatch, handler)
    search = json.loads(plugin.memory_search({"query": "planned remove"}))
    expand = json.loads(plugin.memory_expand({"handle": "p:abcdef12"}))
    evidence_text = plugin.memory_evidence({"handle": "m:12345678"})
    evidence = json.loads(evidence_text)
    assert search == {
        "contract_version": 2,
        "results": [{"handle": "m:12345678", "kind": "fact", "markers": [], "score": 0.8, "text": "fact"}],
    }
    assert expand["markdown"] == "body" and "debug" not in expand
    assert evidence["excerpts"] == [{"text": "evidence"}]
    assert len(evidence_text.encode()) <= contract.LIMITS["max_tool_result_bytes"]
    assert calls == [
        ("memory_search", {"query": "planned remove", "limit": 8}),
        ("memory_expand", {"handle": "p:abcdef12"}),
        ("memory_evidence", {"handle": "m:12345678", "raw": False, "limit": 5}),
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
    _mcp_call_double(
        monkeypatch,
        lambda *a: (_ for _ in ()).throw(RuntimeError("secret backend stack")),
    )
    result = plugin.memory_search({"query": "q"})
    assert json.loads(result) == {"error": "transport_error"}
    assert "secret" not in result and "stack" not in result


def _mcp_write(calls, handle="m:44a1b02e"):
    """Stub MCP write tools: record (tool, args), answer the m: handle."""

    def handler(name, arguments, kwargs):
        calls.append((name, arguments, kwargs))
        assert name in ("memory_remember", "memory_forget")
        assert arguments["operation_id"]
        return ({"contract_version": 2, "handle": handle}, "")

    return handler


def test_memory_remember_posts_write_and_returns_handle(monkeypatch, fake_spool):
    calls = []
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    _mcp_call_double(monkeypatch, _mcp_write(calls))
    result = json.loads(
        plugin.memory_remember(
            {"text": "the user prefers tea", "durability": "durable"},
            session_id="s",
            sender_id="owner-1",
        )
    )
    assert result == {"handle": "m:44a1b02e"}
    # One synchronous MCP call; the same operation is spooled durably.
    assert len(calls) == 1
    tool, tool_args, _kwargs = calls[0]
    assert tool == "memory_remember"
    assert tool_args == {
        "operation_id": tool_args["operation_id"],
        "text": "the user prefers tea",
        "durability": "durable",
    }
    assert len(fake_spool.items) == 1
    assert fake_spool.items[0]["priority"] == PRIORITY_EXPLICIT
    assert fake_spool.items[0]["kind"] == "memory_write"
    assert fake_spool.items[0]["capture_origin"] == "live"
    envelope = fake_spool.items[0]["envelope"]
    assert envelope["kind"] == "memory_write"
    assert envelope["payload"] == {
        "text": "the user prefers tea",
        "durability": "durable",
        "source": "memory_remember",
    }
    contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    # The spooled envelope and the sync call share the idempotency key, and
    # the spooled envelope converts losslessly to a memory_import item.
    assert tool_args["operation_id"] == envelope["event_id"]
    item = contract.to_import_item(envelope)
    assert item["event_id"] == envelope["event_id"]
    assert "schema_version" not in item and "contract_version" not in item


def test_memory_remember_redacts_before_send(monkeypatch, fake_spool):
    calls = []
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    _mcp_call_double(monkeypatch, _mcp_write(calls))
    plugin.memory_remember({"text": "api_key=sk_live_should_not_travel", "durability": "transient"})
    sent = calls[0][1]["text"]
    assert "sk_live_should_not_travel" not in sent
    assert "sk_live_should_not_travel" not in json.dumps(fake_spool.items[0]["envelope"])


@pytest.mark.parametrize(
    "args",
    [
        {"text": "", "durability": "durable"},
        {"text": "   ", "durability": "durable"},
        {"text": "fact", "durability": "forever"},
        {"text": "fact"},
        {"durability": "durable"},
        {"text": "fact", "durability": "durable", "unknown": 1},
        {"text": 42, "durability": "durable"},
        {"text": "fact", "durability": ["durable"]},
        "not-a-dict",
    ],
)
def test_memory_remember_rejects_bad_input(args):
    assert json.loads(plugin.memory_remember(args)) == {"error": "invalid_request"}


def test_memory_forget_marks_atom_and_returns_handle(monkeypatch, fake_spool):
    calls = []
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    _mcp_call_double(monkeypatch, _mcp_write(calls))
    result = json.loads(
        plugin.memory_forget(
            {"handle": "m:44a1b02e", "reason": "the user corrected this fact"},
            session_id="s",
        )
    )
    assert result == {"handle": "m:44a1b02e"}
    # Exactly one envelope, one MCP call: no fan-out to related atoms.
    assert len(calls) == 1
    assert len(fake_spool.items) == 1
    tool, tool_args, _kwargs = calls[0]
    assert tool == "memory_forget"
    assert tool_args["handle"] == "m:44a1b02e"
    envelope = fake_spool.items[0]["envelope"]
    assert tool_args["operation_id"] == envelope["event_id"]
    assert envelope["kind"] == "memory_forget"
    assert envelope["payload"] == {
        "handle": "m:44a1b02e",
        "reason": "the user corrected this fact",
    }
    assert fake_spool.items[0]["priority"] == PRIORITY_EXPLICIT
    contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])


@pytest.mark.parametrize(
    "args",
    [
        {"handle": ["m:44a1b02e", "m:55c2d03f"], "reason": "no lists"},
        {"handle": "m:44a1b02e"},
        {"handle": "m:44a1b02e", "reason": ""},
        {"handle": "m:44a1b02e", "reason": "   "},
        {"handle": "M:44A1B02E", "reason": "case matters"},
        {"handle": "m:xyz", "reason": "bad hex"},
        {"handle": "m:44a1b02e", "reason": 42},
        {"handle": 42, "reason": "wrong type"},
        {"handle": "m:44a1b02e", "reason": "ok", "extra": 1},
    ],
)
def test_memory_forget_refuses_bad_input(args):
    assert json.loads(plugin.memory_forget(args)) == {"error": "invalid_request"}


def test_memory_forget_says_invalidation_not_removal():
    for text in (
        plugin.memory_forget.__doc__,
        plugin.MEMORY_FORGET_SCHEMA["description"],
        plugin.MEMORY_FORGET_SCHEMA["parameters"]["properties"]["handle"]["description"],
        plugin.MEMORY_FORGET_SCHEMA["parameters"]["properties"]["reason"]["description"],
    ):
        assert FORGET_SENTENCE in text
        lowered = text.lower()
        assert "delete" not in lowered
        assert "erase" not in lowered
        assert "permanently" not in lowered


@pytest.mark.parametrize("callback", [plugin.memory_remember, plugin.memory_forget])
@pytest.mark.parametrize(
    "structured,expected",
    [
        ({"contract_version": 1, "handle": "m:44a1b02e"}, "invalid_response"),
        ({"contract_version": 2, "handle": "p:44a1b02e"}, "invalid_response"),
        ({"contract_version": 2}, "invalid_response"),
        ({"contract_version": 2, "handle": "m:xyz"}, "invalid_response"),
        ({"contract_version": 2, "handle": "m:44a1b02e", "extra": "ok"}, None),
    ],
)
def test_explicit_tools_reject_bad_result(monkeypatch, fake_spool, callback, structured, expected):
    _mcp_call_double(monkeypatch, lambda *a: (dict(structured), ""))
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    if callback is plugin.memory_remember:
        args = {"text": "fact", "durability": "durable"}
    else:
        args = {"handle": "m:44a1b02e", "reason": "no longer true"}
    if expected is None:
        assert json.loads(callback(args)) == {"handle": "m:44a1b02e"}
    else:
        assert json.loads(callback(args)) == {"error": expected}


@pytest.mark.parametrize(
    "raised,expected",
    [("timeout", "timeout"), ("rate_limited", "transport_error")],
)
@pytest.mark.parametrize("callback", [plugin.memory_remember, plugin.memory_forget])
def test_explicit_tools_surface_tool_error_category(
    monkeypatch, fake_spool, callback, raised, expected
):
    """A failed MCP tool surfaces its bounded error category."""
    from substrate.client import ClientError

    def handler(*a):
        raise ClientError(raised)

    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    _mcp_call_double(monkeypatch, handler)
    if callback is plugin.memory_remember:
        args = {"text": "fact", "durability": "durable"}
    else:
        args = {"handle": "m:44a1b02e", "reason": "no longer true"}
    assert json.loads(callback(args)) == {"error": expected}


@pytest.mark.parametrize("callback", [plugin.memory_remember, plugin.memory_forget])
def test_explicit_tools_hide_backend_detail(monkeypatch, fake_spool, callback):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    _mcp_call_double(
        monkeypatch,
        lambda *a: (_ for _ in ()).throw(RuntimeError("secret backend stack")),
    )
    if callback is plugin.memory_remember:
        args = {"text": "fact", "durability": "durable"}
    else:
        args = {"handle": "m:44a1b02e", "reason": "no longer true"}
    result = callback(args)
    assert json.loads(result) == {"error": "transport_error"}
    assert "secret" not in result and "stack" not in result


def test_register_matches_native_hermes_context_and_has_no_side_effect(
    monkeypatch, fake_spool
):
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
    plugin.register(ctx)
    # Only host-dispatched hooks are registered. "sync_turn", "prefetch",
    # and "on_session_switch" are directly callable helpers: the host
    # dispatches no hook under those names.
    assert set(ctx.hooks) == {
        "pre_llm_call",
        "post_llm_call",
        "on_session_end",
        "on_session_finalize",
        "on_session_reset",
        "subagent_start",
        "subagent_stop",
    }
    assert ctx.hooks["post_llm_call"] is plugin.post_llm_call
    assert ctx.hooks["on_session_finalize"] is plugin.on_session_finalize
    assert ctx.hooks["on_session_reset"] is plugin.on_session_reset
    assert set(ctx.tools) == {
        "memory_search",
        "memory_expand",
        "memory_evidence",
        "memory_remember",
        "memory_forget",
    }
    for name, entry in ctx.tools.items():
        assert entry["toolset"] == plugin.TOOLSET
        assert entry["schema"]["name"] == name
        assert entry["handler"] is getattr(plugin, name)
    assert ctx.prompts == {
        "substrate.memory": (plugin.STATIC_MEMORY_PROMPT, "after_memory", 2000)
    }
    # register() is side-effect free: no spool, no network.
    assert fake_spool.items == []
    assert fake_spool.start_calls == []
    assert plugin.prefetch() == ""
    assert plugin.sync_turn is not None and plugin.on_session_switch is not None
# ---------------------------------------------------------------------------
# Real-spool integration (temp dir + offline fake MCP server; no live API/DB)
# ---------------------------------------------------------------------------


def _wait_for(predicate, timeout=10.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _mcp_env(monkeypatch, fake_mcp):
    monkeypatch.setenv(
        "SUBSTRATE_API_URL", f"http://127.0.0.1:{fake_mcp.server_address[1]}"
    )
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.delenv("SUBSTRATE_SPOOL_DIR", raising=False)


def _dead_env(monkeypatch):
    """Point the client at a refused port: background sender retries quietly."""
    monkeypatch.setenv("SUBSTRATE_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.delenv("SUBSTRATE_SPOOL_DIR", raising=False)


def _tool_calls(fake_mcp, tool=None):
    calls = [call for call in fake_mcp.calls if call["method"] == "tools/call"]
    if tool is not None:
        calls = [call for call in calls if call["tool"] == tool]
    return calls


def test_real_spool_enqueue_and_counters(real_spool, clean_session_state, monkeypatch):
    _dead_env(monkeypatch)
    plugin.sync_turn(
        "hello",
        "world",
        session_id="s",
        messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}],
        turn_id="t",
    )
    assert real_spool.pending() == 1
    counters = real_spool.counters()
    assert any(
        key.startswith("capture_turn|live|") and value["item_count"] >= 1
        for key, value in counters.items()
    )


def test_real_spool_lazy_start_delivers_and_retires(
    real_spool, fake_mcp, clean_session_state, monkeypatch
):
    _mcp_env(monkeypatch, fake_mcp)
    plugin.sync_turn(
        "hello",
        "world",
        session_id="s",
        messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}],
        turn_id="t",
    )
    assert _wait_for(lambda: len(_tool_calls(fake_mcp, "memory_import")) >= 1)
    delivered = _tool_calls(fake_mcp, "memory_import")[0]["arguments"]["items"]
    assert len(delivered) == 1
    assert delivered[0]["kind"] == "capture_turn"
    # Import items carry the spool event_id but no envelope versions.
    assert delivered[0]["event_id"]
    assert "schema_version" not in delivered[0] and "contract_version" not in delivered[0]
    # The stored per-item action retires the spooled envelope.
    assert _wait_for(lambda: real_spool.pending() == 0)


def test_spool_batching_limits(fake_mcp, clean_session_state, monkeypatch, tmp_path):
    """Batches hold <=64 items and <=240 KiB of import items."""
    import substrate.spool as spool_module

    _mcp_env(monkeypatch, fake_mcp)
    root = tmp_path / "batch-spool"
    spool = spool_module.Spool(root)
    try:
        for _ in range(70):
            plugin.sync_turn(
                "hello",
                "world",
                session_id="s",
                messages=[{"role": "user", "content": "hello"}],
                turn_id="t",
            )
        # Drain through the fake server one batch at a time.
        for _ in range(200):
            batch = spool.claim_batch()
            if not batch:
                break
            assert len(batch) <= 64
            raw = __import__("json").dumps(
                [__import__("substrate.contract", fromlist=["to_import_item"]).to_import_item(
                    claimed["envelope"]) for claimed in batch]
            ).encode()
            assert len(raw) <= 240 * 1024
            for claimed in batch:
                spool.retire(claimed["item_id"])
        assert spool.pending() == 0
    finally:
        spool.close()


def test_tool_success_against_fake_mcp(
    real_spool, fake_mcp, monkeypatch, clean_session_state
):
    _mcp_env(monkeypatch, fake_mcp)
    result = json.loads(
        plugin.memory_remember({"text": "prefers tea", "durability": "durable"})
    )
    assert result == {"handle": "m:44a1b02e"}
    calls = _tool_calls(fake_mcp, "memory_remember")
    assert len(calls) == 1
    assert calls[0]["arguments"]["text"] == "prefers tea"
    assert calls[0]["arguments"]["operation_id"]
    assert calls[0]["auth"] == "Bearer k"
    assert calls[0]["accept"] == "application/json, text/event-stream"
    # Explicit tools enqueue durably; the tool path never starts the sender.
    assert real_spool.pending() == 1


def test_tool_timeout_against_slow_stub(
    real_spool, fake_mcp, monkeypatch, clean_session_state
):
    _mcp_env(monkeypatch, fake_mcp)
    fake_mcp.delay = 4.0  # longer than the 3 s tool deadline
    result = json.loads(
        plugin.memory_remember({"text": "fact", "durability": "durable"})
    )
    assert result == {"error": "timeout"}
    assert fake_mcp.hits == 1


def test_tool_404_carries_permanent_signal(
    real_spool, fake_mcp, monkeypatch, clean_session_state
):
    # Server 404 (unknown handle) must not retry forever: the client marks
    # it permanent (transient False) so the spool sender quarantines instead
    # of releasing. The tool itself still returns the bounded
    # transport_error shape.
    _mcp_env(monkeypatch, fake_mcp)
    fake_mcp.http_status = 404
    result = json.loads(
        plugin.memory_forget({"handle": "m:44a1b02e", "reason": "no longer true"})
    )
    assert result == {"error": "transport_error"}
    assert fake_mcp.hits == 1
    assert real_spool.pending() == 1
    from substrate.client import ClientError, SubstrateClient

    try:
        SubstrateClient.from_env().call_tool(
            "memory_search", {"query": "probe"}, timeout=3.0
        )
        raise AssertionError("stub should answer 404")
    except ClientError as exc:
        assert exc.category == "not_found"
        assert exc.transient is False


@pytest.mark.disk
def test_spool_reopen_durability_on_disk(tmp_path, clean_session_state, monkeypatch):
    """On-disk durability: queued items and counters survive a reopen."""
    import substrate.spool as spool_module

    _dead_env(monkeypatch)
    root = tmp_path / "disk-spool"
    spool_module.configure_spool(root)
    try:
        from substrate import plugin as _plugin

        _plugin.sync_turn(
            "hello",
            "world",
            session_id="s",
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
            ],
            turn_id="t",
        )
        assert spool_module.get_spool().pending() == 1
    finally:
        spool_module.reset_spool()
    reopened = spool_module.Spool(root)
    try:
        assert reopened.pending() == 1
        assert any(
            key.startswith("capture_turn|live|") and value["item_count"] >= 1
            for key, value in reopened.counters().items()
        )
    finally:
        reopened.close()


def test_sender_quarantines_404_without_retry(
    real_spool, fake_mcp, monkeypatch, clean_session_state
):
    """End to end: tool enqueue + sender batch to a 404 stub quarantines.

    The tool's sync call fails bounded; the sender's batch gets the same
    404, sees the permanent signal, and quarantines with a durable counter
    instead of retrying.
    """
    import time

    _mcp_env(monkeypatch, fake_mcp)
    fake_mcp.http_status = 404
    from substrate.client import SubstrateClient

    assert json.loads(
        plugin.memory_forget({"handle": "m:44a1b02e", "reason": "no longer true"})
    ) == {"error": "transport_error"}
    assert fake_mcp.hits == 1  # tool sync call only; sender not started
    assert real_spool.pending() == 1
    real_spool.start(SubstrateClient.from_env())
    assert _wait_for(lambda: real_spool.pending() == 0, timeout=15.0)
    assert fake_mcp.hits == 2  # exactly one sender batch, never retried
    counters = real_spool.counters()
    assert any(
        key.endswith("|quarantined") and value["item_count"] >= 1
        for key, value in counters.items()
    ), counters
    time.sleep(1.0)
    assert fake_mcp.hits == 2


def test_tool_makes_single_attempt_on_500(
    real_spool, fake_mcp, monkeypatch, clean_session_state
):
    # No client-side retry: redelivery is the spool sender's job.
    _mcp_env(monkeypatch, fake_mcp)
    fake_mcp.http_status = 500
    assert json.loads(plugin.memory_remember({"text": "f", "durability": "durable"})) == {
        "error": "transport_error"
    }
    assert fake_mcp.hits == 1


def test_rejected_items_quarantine_while_rest_retire(
    real_spool, fake_mcp, monkeypatch, clean_session_state
):
    """Per-item semantics: rejected quarantines, stored/duplicate retire."""
    from substrate.client import SubstrateClient

    _mcp_env(monkeypatch, fake_mcp)
    fake_mcp.import_mode = lambda index, item: "stored" if index == 0 else "rejected"
    plugin.sync_turn(
        "hello", "world", session_id="s",
        messages=[{"role": "user", "content": "hello"}],
        turn_id="t",
    )
    plugin.sync_turn(
        "bye", "moon", session_id="s",
        messages=[{"role": "user", "content": "bye"}],
        turn_id="t2",
    )
    def _outcome(kind: str) -> bool:
        return any(
            key.endswith(f"|{kind}") and value["item_count"] >= 1
            for key, value in real_spool.counters().items()
        )

    assert _wait_for(lambda: real_spool.pending() == 0, timeout=15.0)
    # Counters are written by the sender thread after the item leaves the
    # queue, so wait for the outcomes rather than sampling once.
    assert _wait_for(lambda: _outcome("delivered") and _outcome("quarantined"), timeout=15.0), real_spool.counters()
    assert SubstrateClient is not None
