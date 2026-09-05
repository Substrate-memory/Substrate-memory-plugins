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
    search = json.loads(plugin.memory_search({"query": "planned remove"}))
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
        ("/api/v1/memory/search", {"query": "planned remove", "limit": 8}),
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


def _ledger_ack(posts, handle="m:44a1b02e", action="stored", stored=True):
    def post(self, path, body, **kwargs):
        posts.append((path, body, kwargs))
        assert path == "/api/v1/ledger/events"
        assert kwargs.get("idempotency_key") == body["event_id"]
        return {
            "event_id": body["event_id"],
            "accepted": True,
            "stored": stored,
            "status": "accepted",
            "action": action,
            "handle": handle,
        }

    return post


def test_memory_remember_posts_write_and_returns_handle(monkeypatch, fake_spool):
    posts = []
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.setattr(plugin.SubstrateClient, "post_json", _ledger_ack(posts))
    result = json.loads(
        plugin.memory_remember(
            {"text": "the user prefers tea", "durability": "durable"},
            session_id="s",
            sender_id="owner-1",
        )
    )
    assert result == {"handle": "m:44a1b02e"}
    assert len(posts) == 1
    envelope = posts[0][1]
    assert envelope["kind"] == "memory_write"
    assert envelope["payload"] == {
        "text": "the user prefers tea",
        "durability": "durable",
        "source": "memory_remember",
    }
    contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    assert len(fake_spool.items) == 1
    assert fake_spool.items[0]["priority"] == PRIORITY_EXPLICIT
    assert fake_spool.items[0]["kind"] == "memory_write"
    assert fake_spool.items[0]["capture_origin"] == "live"


def test_memory_remember_redacts_before_send(monkeypatch, fake_spool):
    posts = []
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.setattr(plugin.SubstrateClient, "post_json", _ledger_ack(posts))
    plugin.memory_remember({"text": "api_key=sk_live_should_not_travel", "durability": "transient"})
    sent = posts[0][1]["payload"]["text"]
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
    posts = []
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.setattr(plugin.SubstrateClient, "post_json", _ledger_ack(posts))
    result = json.loads(
        plugin.memory_forget(
            {"handle": "m:44a1b02e", "reason": "the user corrected this fact"},
            session_id="s",
        )
    )
    assert result == {"handle": "m:44a1b02e"}
    # Exactly one envelope, one POST: no fan-out to related atoms.
    assert len(posts) == 1
    assert len(fake_spool.items) == 1
    envelope = posts[0][1]
    assert fake_spool.items[0]["envelope"]["event_id"] == envelope["event_id"]
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
    "ack,expected",
    [
        ({"stored": False, "action": "stored", "handle": "m:44a1b02e"}, "invalid_response"),
        ({"stored": True, "action": "mystery", "handle": "m:44a1b02e"}, "invalid_response"),
        ({"stored": True, "action": "stored", "handle": "p:44a1b02e"}, "invalid_response"),
        ({"stored": True, "action": "stored"}, "invalid_response"),
    ],
)
def test_explicit_tools_reject_bad_ack(monkeypatch, fake_spool, callback, ack, expected):
    def post(self, path, body, **kwargs):
        response = {"event_id": body["event_id"], "accepted": True, "status": "accepted"}
        response.update(ack)
        return response

    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.setattr(plugin.SubstrateClient, "post_json", post)
    if callback is plugin.memory_remember:
        args = {"text": "fact", "durability": "durable"}
    else:
        args = {"handle": "m:44a1b02e", "reason": "no longer true"}
    assert json.loads(callback(args)) == {"error": expected}


def test_explicit_tools_reject_event_id_mismatch(monkeypatch, fake_spool):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.setattr(
        plugin.SubstrateClient,
        "post_json",
        lambda *a, **k: {
            "event_id": "00000000-0000-4000-8000-000000000000",
            "accepted": True,
            "stored": True,
            "status": "accepted",
            "action": "stored",
            "handle": "m:44a1b02e",
        },
    )
    assert json.loads(plugin.memory_remember({"text": "f", "durability": "durable"})) == {
        "error": "invalid_response"
    }
    assert json.loads(plugin.memory_forget({"handle": "m:44a1b02e", "reason": "r"})) == {
        "error": "invalid_response"
    }


@pytest.mark.parametrize("callback", [plugin.memory_remember, plugin.memory_forget])
def test_explicit_tools_hide_backend_detail(monkeypatch, fake_spool, callback):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.setattr(
        plugin.SubstrateClient,
        "post_json",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret backend stack")),
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
# Real-spool integration (temp dir + local stub ledger; no live API/DB)
# ---------------------------------------------------------------------------


def _wait_for(predicate, timeout=10.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _stub_env(monkeypatch, stub_ledger):
    monkeypatch.setenv(
        "SUBSTRATE_API_URL", f"http://127.0.0.1:{stub_ledger.server_address[1]}"
    )
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.delenv("SUBSTRATE_SPOOL_DIR", raising=False)


def _dead_env(monkeypatch):
    """Point the client at a refused port: background sender retries quietly."""
    monkeypatch.setenv("SUBSTRATE_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.delenv("SUBSTRATE_SPOOL_DIR", raising=False)


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
    real_spool, stub_ledger, clean_session_state, monkeypatch
):
    _stub_env(monkeypatch, stub_ledger)
    plugin.sync_turn(
        "hello",
        "world",
        session_id="s",
        messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}],
        turn_id="t",
    )
    assert _wait_for(lambda: len(stub_ledger.posts) >= 1)
    post = stub_ledger.posts[0]
    assert post["path"] == "/api/v1/ledger/events"
    assert post["idempotency_key"] == post["body"]["event_id"]
    assert post["body"]["kind"] == "capture_turn"
    # The stub ACK (stored + matching event_id + known action) retires it.
    assert _wait_for(lambda: real_spool.pending() == 0)


def test_tool_success_against_stub_ledger(
    real_spool, stub_ledger, monkeypatch, clean_session_state
):
    _stub_env(monkeypatch, stub_ledger)
    result = json.loads(
        plugin.memory_remember({"text": "prefers tea", "durability": "durable"})
    )
    assert result == {"handle": "m:44a1b02e"}
    assert len(stub_ledger.posts) == 1
    assert stub_ledger.posts[0]["body"]["kind"] == "memory_write"
    # Explicit tools enqueue durably; the tool path never starts the sender.
    assert real_spool.pending() == 1


def test_tool_timeout_against_slow_stub(
    real_spool, stub_ledger, monkeypatch, clean_session_state
):
    _stub_env(monkeypatch, stub_ledger)
    stub_ledger.delay = 4.0  # longer than the 3 s tool POST deadline
    result = json.loads(
        plugin.memory_remember({"text": "fact", "durability": "durable"})
    )
    assert result == {"error": "timeout"}
    assert len(stub_ledger.posts) == 1


def test_tool_404_carries_permanent_signal(
    real_spool, stub_ledger, monkeypatch, clean_session_state
):
    # Server 404 (unknown handle) must not retry forever: the client marks
    # it permanent (status 404, transient False) so the spool sender
    # quarantines instead of releasing. The tool itself still returns the
    # bounded transport_error shape.
    _stub_env(monkeypatch, stub_ledger)
    stub_ledger.mode = "error"
    stub_ledger.status = 404
    result = json.loads(
        plugin.memory_forget({"handle": "m:44a1b02e", "reason": "no longer true"})
    )
    assert result == {"error": "transport_error"}
    assert len(stub_ledger.posts) == 1
    assert real_spool.pending() == 1
    from substrate.client import ClientError, SubstrateClient

    try:
        SubstrateClient.from_env().post_json(
            "/api/v1/ledger/events", {"probe": True}, timeout=3.0
        )
        raise AssertionError("stub should answer 404")
    except ClientError as exc:
        assert exc.status == 404
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
    real_spool, stub_ledger, monkeypatch, clean_session_state
):
    """End to end: tool enqueue + sender POST to a 404 stub quarantines.

    The tool's sync POST fails bounded; the sender's retry of the queued
    item gets the same 404, sees the permanent signal, and quarantines
    with a durable counter instead of retrying (spool 8b88008).
    """
    import time

    _stub_env(monkeypatch, stub_ledger)
    stub_ledger.mode = "error"
    stub_ledger.status = 404
    from substrate.client import SubstrateClient

    assert json.loads(
        plugin.memory_forget({"handle": "m:44a1b02e", "reason": "no longer true"})
    ) == {"error": "transport_error"}
    assert len(stub_ledger.posts) == 1  # tool sync POST only; sender not started
    assert real_spool.pending() == 1
    real_spool.start(SubstrateClient.from_env())
    assert _wait_for(lambda: real_spool.pending() == 0, timeout=15.0)
    assert len(stub_ledger.posts) == 2  # exactly one sender attempt, never retried
    counters = real_spool.counters()
    assert any(
        key.endswith("|quarantined") and value["item_count"] >= 1
        for key, value in counters.items()
    ), counters
    time.sleep(1.0)
    assert len(stub_ledger.posts) == 2


def test_tool_makes_single_attempt_on_500(
    real_spool, stub_ledger, monkeypatch, clean_session_state
):
    # No client-side retry: redelivery is the spool sender's job.
    _stub_env(monkeypatch, stub_ledger)
    stub_ledger.mode = "error"
    stub_ledger.status = 500
    assert json.loads(plugin.memory_remember({"text": "f", "durability": "durable"})) == {
        "error": "transport_error"
    }
    assert len(stub_ledger.posts) == 1
