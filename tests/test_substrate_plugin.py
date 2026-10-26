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
        "timeout": 2.5,
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
    (envelope,) = plugin._capture_envelopes(
        "first",
        "final visible answer",
        session_id="session",
        messages=history,
        turn_id="turn",
        sender_id="owner",
    )
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
        ({"contract_version": 3, "handle": "m:44a1b02e"}, "queued"),  # 1 and 2 are valid
        ({"contract_version": 2, "handle": "p:44a1b02e"}, "queued"),
        ({"contract_version": 2}, "queued"),
        ({"contract_version": 2, "handle": "m:xyz"}, "queued"),
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
    elif expected == "queued":
        result = json.loads(callback(args))
        assert result["status"] == "queued"
        assert "handle" not in result and "error" not in result
    else:
        assert json.loads(callback(args)) == {"error": expected}


@pytest.mark.parametrize(
    "raised,expected",
    [("timeout", "queued"), ("rate_limited", "queued"), ("invalid_request", "invalid_request")],
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
    result = json.loads(callback(args))
    if expected == "queued":
        # Transient: the spooled copy is delivered later, so it is not a failure.
        assert result["status"] == "queued"
    else:
        assert result == {"error": expected}


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
    # Enqueued before the call failed: queued, and no backend detail leaks.
    assert json.loads(result)["status"] == "queued"
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
        "substrate_import",
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
    # The write is also spooled durably. The sender now starts with the tool, so
    # it delivers the same event id via memory_import; the server dedupes it.
    operation_id = calls[0]["arguments"]["operation_id"]
    assert _wait_for(lambda: real_spool.pending() == 0, timeout=15.0)
    imports = _tool_calls(fake_mcp, "memory_import")
    assert any(
        item.get("event_id") == operation_id
        for call in imports for item in call["arguments"]["items"]
    ), imports


def test_tool_timeout_against_slow_stub(
    real_spool, fake_mcp, monkeypatch, clean_session_state
):
    _mcp_env(monkeypatch, fake_mcp)
    fake_mcp.delay = 4.0  # longer than the 3 s tool deadline
    result = json.loads(
        plugin.memory_remember({"text": "fact", "durability": "durable"})
    )
    assert result["status"] == "queued"
    assert "handle" not in result and "error" not in result
    assert fake_mcp.hits >= 1


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
    # No hit count here: the 404 answers the initialize POST, and the spool
    # sender may post its own memory_import concurrently. Permanence is shown
    # by the quarantine counter and transient=False below.
    # The write is spooled and the sender is started, so the permanent refusal
    # is settled by the sender (quarantined with a counter), not retried.
    assert _wait_for(lambda: real_spool.pending() == 0, timeout=15.0)
    assert any(
        key.endswith("|quarantined") and value["item_count"] >= 1
        for key, value in real_spool.counters().items()
    ), real_spool.counters()
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

    assert json.loads(
        plugin.memory_forget({"handle": "m:44a1b02e", "reason": "no longer true"})
    ) == {"error": "transport_error"}
    # The tool started the sender, which settles the spooled copy: one tool call
    # plus exactly one sender batch, never retried.
    assert _wait_for(lambda: real_spool.pending() == 0, timeout=15.0)
    assert fake_mcp.hits == 2
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
    result = json.loads(plugin.memory_remember({"text": "f", "durability": "durable"}))
    assert result["status"] == "queued"
    # No retry inside the tool: the started sender may deliver the spooled copy later.


def test_rejected_items_quarantine_while_rest_retire(
    real_spool, fake_mcp, monkeypatch, clean_session_state
):
    """Per-item semantics: an irreducible rejection quarantines; the rest retire.

    A refused two-message turn is split first (HER-08); its refused single
    message then quarantines with a counter, while the accepted half retires.
    """
    from substrate.client import SubstrateClient

    _mcp_env(monkeypatch, fake_mcp)
    fake_mcp.import_mode = (
        lambda index, item: "rejected" if "poison" in json.dumps(item) else "stored"
    )
    plugin.sync_turn(
        "hello", "world", session_id="s",
        messages=[{"role": "user", "content": "hello"}],
        turn_id="t",
    )
    plugin.sync_turn(
        "poison", "moon", session_id="s",
        messages=[{"role": "user", "content": "poison"}],
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
    assert _wait_for(
        lambda: _outcome("delivered") and _outcome("split") and _outcome("quarantined"),
        timeout=15.0,
    ), real_spool.counters()
    assert SubstrateClient is not None


def test_remember_while_offline_is_queued_not_failed(real_spool, monkeypatch, clean_session_state):
    """HER-07: the write is spooled; the tool must say so, not ask for a retry."""
    _dead_env(monkeypatch)
    result = json.loads(
        plugin.memory_remember(
            {"text": "the user prefers tea", "durability": "durable"},
            session_id="s",
            sender_id="owner",
        )
    )
    assert result["status"] == "queued"
    assert "handle" not in result and "error" not in result
    assert "syncs to Substrate automatically" in result["message"]
    assert "Do not call memory_remember again" in result["message"]
    assert any(
        key.startswith("memory_write|") and key.endswith("|enqueued")
        for key in real_spool.counters()
    ), real_spool.counters()


def test_forget_while_offline_is_queued_not_failed(real_spool, monkeypatch, clean_session_state):
    _dead_env(monkeypatch)
    result = json.loads(
        plugin.memory_forget({"handle": "m:44a1b02e", "reason": "no longer true"})
    )
    assert result["status"] == "queued"
    assert "error" not in result
    assert "Do not call memory_forget again" in result["message"]
    assert any(
        key.startswith("memory_forget|") and key.endswith("|enqueued")
        for key in real_spool.counters()
    ), real_spool.counters()


def test_write_that_was_neither_sent_nor_queued_still_fails(monkeypatch, clean_session_state):
    from substrate import spool as spool_module

    class Full:
        def enqueue(self, *a, **k):
            raise spool_module.SpoolFull("full")

    monkeypatch.setattr(plugin, "get_spool", lambda: Full())
    _dead_env(monkeypatch)
    assert json.loads(
        plugin.memory_remember({"text": "fact", "durability": "durable"})
    ) == {"error": "transport_error"}


def test_import_is_routed_to_the_import_tool_not_remember():
    """HER-02: a direct import request must reach substrate_import, never remember."""
    remember = plugin.MEMORY_REMEMBER_SCHEMA["description"]
    assert "substrate_import" in remember and "Not for importing" in remember
    import_description = plugin.SUBSTRATE_IMPORT_SCHEMA["description"]
    assert "Use this tool for any request to import" in import_description
    assert "Never use memory_remember" in import_description
    assert "never say conversations were imported" in import_description
    assert "substrate_import" in plugin.STATIC_MEMORY_PROMPT
    assert "never memory_remember" in plugin.STATIC_MEMORY_PROMPT
    assert len(plugin.STATIC_MEMORY_PROMPT) < 2000


def test_long_session_capture_stays_under_the_limits_for_150_turns(fake_spool, clean_session_state):
    """HER-08: captures are incremental and chunked; nothing grows past the bounds."""
    history: list[dict] = []
    for turn in range(150):
        user = f"question {turn} " + "u" * 300
        answer = f"answer {turn} " + "a" * 900
        history.append({"role": "user", "content": user})
        history.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"c{turn}", "function": {
                "name": "terminal", "arguments": '{"command":"ls"}'}}],
        })
        history.append({"role": "tool", "tool_call_id": f"c{turn}",
                        "name": "terminal", "content": "out " * 2000})
        history.append({"role": "assistant", "content": answer})
        plugin.sync_turn(user, answer, session_id="long", messages=list(history), turn_id=f"t{turn}")
    envelopes = [item["envelope"] for item in fake_spool.items]
    assert envelopes
    indices: list[int] = []
    for envelope in envelopes:
        contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
        messages = envelope["payload"]["messages"]
        assert len(messages) <= 128
        assert len(contract.canonical_bytes(envelope)) <= contract.ENVELOPE_BUDGET_BYTES
        indices.extend(message["index"] for message in messages)
    # Every message of the 600-message session is sent once, in order (no replay).
    assert indices == list(range(len(history)))
    total = sum(len(contract.canonical_bytes(envelope)) for envelope in envelopes)
    assert total < 4 * len(history) * 1024 * 3  # linear in the session, not quadratic


def test_oversized_turn_is_split_not_quarantined(real_spool, fake_mcp, monkeypatch,
                                                  clean_session_state):
    """HER-08: a turn the server refuses for size is split and delivered, not quarantined."""
    _mcp_env(monkeypatch, fake_mcp)
    fake_mcp.import_mode = (
        lambda index, item: "rejected" if len(item["payload"]["messages"]) > 2 else "stored"
    )
    messages = []
    for turn in range(3):
        messages.append({"role": "user", "content": f"q{turn}"})
        messages.append({"role": "assistant", "content": f"a{turn}"})
    plugin.sync_turn("q", "a", session_id="split", messages=messages, turn_id="big")
    assert _wait_for(lambda: real_spool.pending() == 0, timeout=15.0)
    counters = real_spool.counters()
    assert any(k.endswith("|split") and v["item_count"] >= 1 for k, v in counters.items()), counters
    assert not any(k.endswith("|quarantined") for k in counters), counters
    delivered = sum(
        v["item_count"] for k, v in counters.items() if k.endswith("|delivered")
    )
    assert delivered >= 2


def test_client_refuses_an_oversized_request_as_payload_too_large(monkeypatch):
    from substrate.client import ClientError, SubstrateClient

    client = SubstrateClient("http://127.0.0.1:9", "k")
    with pytest.raises(ClientError) as refused:
        client._rpc("tools/call", {"name": "memory_import",
                                   "arguments": {"items": ["x" * 300_000]}}, timeout=1.0)
    assert refused.value.category == "payload_too_large"
    assert refused.value.transient is False


# ---------------------------------------------------------------------------
# 0.9.1 (customer test 2026-10-11): explicit retrieval budget, busy result,
# server-supplied memory_search description, version note at connect only,
# and the sender's per-item rejection reason.
# ---------------------------------------------------------------------------


def _scripted_call_tool(monkeypatch, outcomes):
    """Each SubstrateClient.call_tool pops one outcome: an exception to raise,
    or a structuredContent dict to return. Returns the list of timeouts seen."""
    from substrate.client import SubstrateClient

    seen = []

    def call(self, name, arguments, **kwargs):
        seen.append(kwargs.get("timeout"))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, ""

    monkeypatch.setattr(SubstrateClient, "call_tool", call)
    monkeypatch.setenv("SUBSTRATE_API_URL", "https://memory.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "secret")
    monkeypatch.delenv(plugin.TOOL_TIMEOUT_ENV, raising=False)
    return seen


def _search_ok():
    return {"contract_version": contract.MCP_CONTRACT_VERSION, "results": []}


def test_turn_context_budget_keeps_headroom_over_server_deadline():
    # The server answers at its own 1500 ms deadline; the plugin must still be
    # waiting then, with about a second left for TLS, auth and transport.
    assert plugin.TURN_CONTEXT_TIMEOUT == 2.5
    assert plugin.TURN_CONTEXT_TIMEOUT - 1.5 >= 1.0


def test_explicit_search_retries_once_after_a_timeout(monkeypatch):
    from substrate.client import ClientError

    seen = _scripted_call_tool(monkeypatch, [ClientError("timeout"), _search_ok()])
    result = json.loads(plugin.memory_search({"query": "whatsapp"}))
    assert result == {"contract_version": contract.MCP_CONTRACT_VERSION, "results": []}
    assert len(seen) == 2
    assert seen[0] == plugin.EXPLICIT_CALL_ATTEMPT_SECONDS
    assert plugin.EXPLICIT_CALL_ATTEMPT_SECONDS < seen[1] <= plugin.EXPLICIT_CALL_BUDGET_SECONDS


@pytest.mark.parametrize("category", ["timeout", "transport_error", "server_error"])
def test_explicit_call_gives_up_after_one_retry_with_a_busy_message(monkeypatch, category):
    from substrate.client import ClientError

    seen = _scripted_call_tool(
        monkeypatch, [ClientError(category), ClientError(category)]
    )
    result = json.loads(plugin.memory_search({"query": "whatsapp"}))
    assert len(seen) == 2
    assert result["error"] == "timeout"
    assert "busy or slow right now" in result["message"]
    assert "try again in a moment" in result["message"].lower()
    assert "Say that the memory lookup is busy." in result["message"]
    assert "lack access" not in result["message"]


@pytest.mark.parametrize(
    "category, status",
    [("invalid_request", 400), ("not_found", 404), ("rate_limited", 429),
     ("payload_too_large", 413)],
)
def test_client_4xx_answers_are_not_retried(monkeypatch, category, status):
    from substrate.client import ClientError

    seen = _scripted_call_tool(
        monkeypatch, [ClientError(category, status=status, transient=False)]
    )
    result = json.loads(plugin.memory_expand({"handle": "m:44a1b02e"}))
    assert len(seen) == 1
    # A refusal is an answer, not a busy server: no busy text, no second attempt.
    assert "busy" not in json.dumps(result)
    assert "message" not in result


def test_evidence_uses_the_same_budget(monkeypatch):
    from substrate.client import ClientError

    seen = _scripted_call_tool(monkeypatch, [ClientError("timeout"), ClientError("timeout")])
    json.loads(plugin.memory_evidence({"handle": "m:44a1b02e"}))
    assert len(seen) == 2 and seen[0] == plugin.EXPLICIT_CALL_ATTEMPT_SECONDS


@pytest.mark.parametrize(
    "raw, expected",
    [("", 15.0), ("30", 30.0), ("1", 3.0), ("999", 60.0), ("bogus", 15.0), ("nan", 15.0)],
)
def test_explicit_budget_is_configurable_and_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv(plugin.TOOL_TIMEOUT_ENV, raw)
    assert plugin._explicit_budget() == expected


def test_unknown_hermes_builds_get_no_version_note():
    for version in ("0.0.0", "0.0", "vgit.5473cec", "dev", ""):
        assert plugin.host_version_notice(version) == "", version
    assert "0.20.3" in plugin.host_version_notice("0.20.3")


def test_version_note_is_announced_once_at_connect_only(monkeypatch):
    from substrate import onboarding

    monkeypatch.setattr(plugin, "_version_notice_shown", False)
    monkeypatch.setattr(plugin, "running_hermes_version", lambda: "0.20.3")
    monkeypatch.setattr(
        onboarding, "take_connected_announcement",
        lambda: "Connected to Substrate as you@example.com.",
    )
    first = plugin._connected_announcement()
    assert "Connected to Substrate as you@example.com." in first
    assert "0.20.3" in first and "Then tell the user once" in first
    monkeypatch.setattr(onboarding, "take_connected_announcement", lambda: "Connected again.")
    assert "0.20.3" not in plugin._connected_announcement()


def test_search_description_is_fetched_once_in_the_background(monkeypatch):
    calls = []
    monkeypatch.setitem(plugin._SEARCH_DESCRIPTION_STATE, "done", False)
    monkeypatch.setitem(plugin._SEARCH_DESCRIPTION_STATE, "next_at", 0.0)
    monkeypatch.setattr(
        plugin, "_refresh_search_description", lambda client: calls.append(client) or True
    )
    client = object()
    plugin._maybe_refresh_search_description(client)
    assert _wait_for(lambda: plugin._SEARCH_DESCRIPTION_STATE["done"], timeout=5.0)
    plugin._maybe_refresh_search_description(client)
    assert calls == [client]


def test_normal_turns_never_carry_the_version_note(monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "_host_version_notice_once", lambda: calls.append(1) or "x")
    monkeypatch.setenv("SUBSTRATE_API_URL", "https://memory.example/")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "secret")

    def handler(name, arguments, kwargs):
        return (
            {"contract_version": 2, "session_id": "s", "turn": 0, "block": "",
             "handles": [], "missing_turns": 0, "brief_version": 0,
             "latency_ms": 1.0, "empty_reason": "no_candidates"},
            "",
        )

    _mcp_call_double(monkeypatch, handler)
    plugin.pre_llm_call(session_id="s", user_message="hello")
    assert calls == []


def test_server_search_description_replaces_the_fallback(monkeypatch, fake_mcp):
    _mcp_env(monkeypatch, fake_mcp)
    from substrate.client import SubstrateClient

    fallback = plugin.MEMORY_SEARCH_SCHEMA["description"]
    monkeypatch.setitem(plugin.MEMORY_SEARCH_SCHEMA, "description", fallback)
    fake_mcp.search_description = (
        "Search Substrate memory. This memory includes the user's connected Gmail and Notion."
    )
    assert plugin._refresh_search_description(SubstrateClient.from_env()) is True
    assert plugin.MEMORY_SEARCH_SCHEMA["description"] == fake_mcp.search_description


def test_missing_or_oversized_server_description_keeps_the_fallback(monkeypatch, fake_mcp):
    _mcp_env(monkeypatch, fake_mcp)
    from substrate.client import SubstrateClient

    fallback = plugin.MEMORY_SEARCH_SCHEMA["description"]
    monkeypatch.setitem(plugin.MEMORY_SEARCH_SCHEMA, "description", fallback)
    client = SubstrateClient.from_env()
    fake_mcp.search_description = None
    assert plugin._refresh_search_description(client) is False
    fake_mcp.search_description = "x" * (plugin.MAX_SERVER_DESCRIPTION_CHARS + 1)
    assert plugin._refresh_search_description(client) is False
    assert plugin.MEMORY_SEARCH_SCHEMA["description"] == fallback


def test_server_description_is_one_clean_line():
    assert plugin._clean_server_description("Search\n  memory.\tNow\x00") == "Search memory. Now"
    assert plugin._clean_server_description("") == ""
    assert plugin._clean_server_description(3) == ""


def test_fallback_search_description_is_direct():
    text = plugin.MEMORY_SEARCH_SCHEMA["description"]
    assert text.startswith("Search Substrate memory directly")
    assert "Search before saying a source is unavailable" in text
    assert "lack access" not in text


def test_payload_too_large_tool_error_is_permanent(monkeypatch):
    from substrate.client import ClientError, SubstrateClient

    client = SubstrateClient("https://memory.example", "secret")
    monkeypatch.setattr(client, "ensure_initialized", lambda **kwargs: {})
    monkeypatch.setattr(client, "_rpc", lambda *args, **kwargs: {
        "isError": True,
        "structuredContent": {"contract_version": 2, "error": "payload_too_large"},
    })
    with pytest.raises(ClientError) as info:
        client.call_tool("memory_capture_turn", {"session_id": "s"}, timeout=1.0)
    assert info.value.category == "payload_too_large"
    assert info.value.transient is False


def test_rejected_import_item_logs_the_server_reason(real_spool, fake_mcp, caplog):
    from substrate.spool import PRIORITY_LIVE

    envelope = {
        "schema_version": 3, "contract_version": 1, "event_id": "evt-reject-1",
        "kind": "capture_turn", "session_id": "session-1", "offset": {"start": 0, "end": 1},
        "capture_origin": "live", "batch_id": "",
        "speaker": {"id": "user", "role": "owner", "display": ""},
        "created_at": "2026-09-04T00:00:00Z",
        "payload": {"turn_id": "turn-1", "messages": [
            {"index": 0, "role": "user", "content": "hello"},
        ]},
    }
    real_spool.enqueue(envelope, priority=PRIORITY_LIVE, kind="capture_turn", capture_origin="live")
    batch = real_spool.claim_batch()
    structured = {
        "contract_version": 2, "batch_id": "b" * 16, "accepted": 0, "rejected": 1,
        "results": [{
            "index": 0, "action": "rejected", "error": "payload_too_large",
            "hint": "max_messages: 128; split the turn into chunks of at most 128 messages",
        }],
    }
    with caplog.at_level("WARNING", logger="substrate.spool"):
        real_spool._settle_batch(batch, structured)
    messages = [record.getMessage() for record in caplog.records if record.name == "substrate.spool"]
    assert any(
        "rejected by Substrate" in text and "reason=payload_too_large: max_messages: 128" in text
        and "event_id=evt-reject-1" in text
        for text in messages
    ), messages
