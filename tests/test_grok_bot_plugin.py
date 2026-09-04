"""Behavior tests for the Substrate Grok Bot plugin (offline, no network)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "grok-bot"

import _hostload  # noqa: E402

_ld = _hostload.begin("grok-bot", [PLUGIN_DIR, _hostload.REPO / "plugins"])
contract = _ld.core("contract")
runtime = _ld.core("runtime")
hermes_plugin = _ld.hermes("plugin")
bridge = _ld.top("bridge.py")
server = _ld.top("server.py")
_ld.commit()


@pytest.fixture(autouse=True)
def _allow_test_origins(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_DEVELOPMENT_MODE", "1")


def test_manifests_parse_and_advertise_three_tools():
    manifest_text = (PLUGIN_DIR / "grok-bot.yaml").read_text()
    for tool in ("memory_search", "memory_expand", "memory_evidence"):
        assert tool in manifest_text
    assert "transport: stdio" in manifest_text
    plugin_json = json.loads((PLUGIN_DIR / "plugin.json").read_text())
    assert plugin_json["name"] == "substrate-memory"
    mcp_json = json.loads((PLUGIN_DIR / ".mcp.json").read_text())
    assert "substrate-memory" in mcp_json["mcpServers"]
    hooks = json.loads((PLUGIN_DIR / "hooks" / "hooks.json").read_text())
    assert {hook["event"] for hook in hooks["hooks"]} >= {"pre_turn", "post_turn", "session_end"}
    tools_manifest = json.loads((PLUGIN_DIR / "grok-tools.json").read_text())
    assert [entry["function"]["name"] for entry in tools_manifest] == [
        "memory_search",
        "memory_expand",
        "memory_evidence",
    ]
    assert (PLUGIN_DIR / "skills" / "substrate-memory" / "SKILL.md").exists()
    assert (PLUGIN_DIR / "instructions.md").exists()


def test_tool_schema_parity_with_hermes():
    assert runtime.MEMORY_SEARCH_SCHEMA == hermes_plugin.MEMORY_SEARCH_SCHEMA
    assert runtime.MEMORY_EXPAND_SCHEMA == hermes_plugin.MEMORY_EXPAND_SCHEMA
    assert runtime.MEMORY_EVIDENCE_SCHEMA == hermes_plugin.MEMORY_EVIDENCE_SCHEMA
    assert runtime.STATIC_MEMORY_PROMPT == hermes_plugin.STATIC_MEMORY_PROMPT
    assert runtime.TOOLSET == hermes_plugin.TOOLSET


def test_tool_handlers_match_hermes(monkeypatch):
    calls = []

    def post(self, path, body, **kwargs):
        calls.append((path, body))
        if path.endswith("search"):
            return {"contract_version": 1, "results": [
                {"handle": "m:12345678", "text": "fact", "score": 0.5, "kind": "fact", "markers": []},
            ]}
        if path.endswith("expand"):
            return {"contract_version": 1, "handle": "p:abcdef12", "kind": "page", "title": "T"}
        return {"contract_version": 1, "excerpts": [{"text": "proof"}]}

    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.setattr(runtime.SubstrateClient, "post_json", post)
    assert json.loads(runtime.memory_search({"query": "q"}))["results"][0]["handle"] == "m:12345678"
    assert json.loads(hermes_plugin.memory_search({"query": "q"}))
    assert "handle" in runtime.memory_expand({"handle": "p:abcdef12"})
    assert "excerpts" in runtime.memory_evidence({"handle": "m:12345678"})
    assert json.loads(runtime.memory_search({"query": "bad", "limit": 99})) == {"error": "invalid_request"}


def test_redaction_and_envelope_validation():
    assert "hide" not in runtime._redact_text("api_key=hide")
    assert runtime._safe_value({"token": "hide"}) == {"token": "[REDACTED]"}
    envelope = runtime._capture_envelope(
        "hello", "world", session_id="s",
        messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}],
        turn_id="t",
    )
    assert envelope is not None
    contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    session = runtime._session_envelope("s", "end")
    assert session is not None
    contract.validate_envelope(session, idempotency_key=session["event_id"])


def test_retrieval_failure_returns_empty_context(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("backend detail must not escape")

    monkeypatch.setattr(runtime.SubstrateClient, "post_json", boom)
    assert runtime.pre_llm_call("s", "q", [], turn_id="t") is None
    assert bridge.pre_turn_context("s", "q", [], turn_id="t") == ""


def test_no_secret_in_outputs(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-token-value")
    monkeypatch.setenv("SUBSTRATE_HOME", str(tmp_path))
    outputs = [
        runtime._redact_text("Authorization: Bearer test-token-value"),
        runtime.memory_search({"query": "", "limit": 8}),
        bridge.pre_turn_context("", "", []),
        json.dumps(bridge.tool_manifest()),
    ]
    for output in outputs:
        assert "test-token-value" not in output


def test_mcp_stdio_round_trip(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.setattr(
        runtime.SubstrateClient, "post_json",
        lambda *a, **k: {"contract_version": 1, "results": []},
    )
    assert server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]["serverInfo"]["name"] == "substrate-memory"
    tools = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert [entry["name"] for entry in tools] == ["memory_search", "memory_expand", "memory_evidence"]
    call = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "memory_search", "arguments": {"query": "q"}}})
    assert json.loads(call["result"]["content"][0]["text"]) == {"contract_version": 1, "results": []}
    assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    err = server.handle_message({"jsonrpc": "2.0", "id": 4, "method": "nope"})
    assert err["error"]["code"] == -32601
    # full serve() loop over one initialize line
    stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n")
    stdout = io.StringIO()
    assert server.serve(stdin, stdout) == 0
    assert json.loads(stdout.getvalue())["id"] == 7


def test_bridge_capture_never_blocks_or_raises(monkeypatch):
    monkeypatch.setattr(runtime._CAPTURE_WORKER, "enqueue", lambda envelope: (_ for _ in ()).throw(RuntimeError("x")))
    bridge.capture_turn("hi", "there", session_id="s", conversation_history=[])
    bridge.session_end(session_id="s")
    bridge.session_reset(session_id="old", new_session_id="new")
    assert bridge.STATIC_MEMORY_PROMPT == hermes_plugin.STATIC_MEMORY_PROMPT
