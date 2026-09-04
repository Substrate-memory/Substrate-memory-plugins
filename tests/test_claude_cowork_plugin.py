"""Tests for the Claude Cowork Substrate plugin (offline, stdlib + pytest)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "plugins" / "claude-cowork"

import _hostload  # noqa: E402

_ld = _hostload.begin("claude-cowork", [PLUGIN_DIR, REPO / "plugins"])
hermes_client = _ld.hermes("client")
hermes_contract = _ld.hermes("contract")
hermes_plugin = _ld.hermes("plugin")
cowork_contract = _ld.core("contract")
hosthome = _ld.core("hosthome")
cowork_client = _ld.core("client")
cowork_onboarding = _ld.core("onboarding")
runtime = _ld.core("runtime")
transcript = _ld.core("transcript")
mcp_server = _ld.core("mcp_server")
_ld.commit()

FAKE_KEY = "sk_sub_testkey0123456789abcdef012345"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBSTRATE_DEVELOPMENT_MODE", "1")
    monkeypatch.setenv("SUBSTRATE_HOME", str(tmp_path / "cowork-home"))
    monkeypatch.setenv("SUBSTRATE_API_URL", "https://127.0.0.1:9/")
    monkeypatch.setenv("SUBSTRATE_API_KEY", FAKE_KEY)
    for var in ("HERMES_HOME", "CLAUDE_CONFIG_DIR", "SUBSTRATE_AGENT_NAME",
                "SUBSTRATE_WIKI_ORIGIN"):
        monkeypatch.delenv(var, raising=False)


class _Response:
    def __init__(self, value, status=200):
        self.raw = json.dumps(value).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.raw if size < 0 else self.raw[:size]


def _refused(monkeypatch):
    def urlopen(request, timeout):
        raise OSError("connection refused")
    monkeypatch.setattr(cowork_client, "_open_request", urlopen)
    monkeypatch.setattr(hermes_client, "_open_request", urlopen)


# --- manifests --------------------------------------------------------

def test_manifests_parse_and_agree():
    plugin_manifest = json.loads((PLUGIN_DIR / ".claude-plugin" / "plugin.json").read_text())
    assert plugin_manifest["name"] == "substrate-cowork"
    assert plugin_manifest["version"] == "0.3.0"
    assert len(plugin_manifest["description"]) >= 20
    entry = json.loads((PLUGIN_DIR / ".claude-plugin" / "marketplace-entry.json").read_text())
    assert entry["name"] == plugin_manifest["name"]
    assert entry["version"] == plugin_manifest["version"]
    assert entry["source"] == "./plugins/claude-cowork"
    hooks = json.loads((PLUGIN_DIR / "hooks" / "hooks.json").read_text())
    assert set(hooks["hooks"]) == {
        "UserPromptSubmit", "SessionStart", "Stop", "SubagentStop", "SessionEnd",
    }
    for event, groups in hooks["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                assert hook["type"] == "command"
                assert "${CLAUDE_PLUGIN_ROOT}/hooks/" in hook["command"]
                script = hook["command"].split("${CLAUDE_PLUGIN_ROOT}/hooks/")[1].rstrip('"')
                assert (PLUGIN_DIR / "hooks" / script).is_file(), event
    mcp = json.loads((PLUGIN_DIR / ".mcp.json").read_text())
    assert set(mcp) == {"mcpServers"}
    assert "substrate-cowork-memory" in mcp["mcpServers"]
    server = PLUGIN_DIR / "substrate_core" / "mcp_server.py"
    assert mcp["mcpServers"]["substrate-cowork-memory"]["args"][0].endswith(
        "substrate_core/mcp_server.py")
    assert server.is_file()
    skill = PLUGIN_DIR / "skills" / "substrate-cowork-memory" / "SKILL.md"
    assert skill.is_file()


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_plugin_validate_passes():
    result = subprocess.run(
        ["claude", "plugin", "validate", str(PLUGIN_DIR)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- parity with the Hermes reference -----------------------------------

def test_tool_schema_parity():
    pairs = [
        (hermes_plugin.MEMORY_SEARCH_SCHEMA, runtime.MEMORY_SEARCH_SCHEMA),
        (hermes_plugin.MEMORY_EXPAND_SCHEMA, runtime.MEMORY_EXPAND_SCHEMA),
        (hermes_plugin.MEMORY_EVIDENCE_SCHEMA, runtime.MEMORY_EVIDENCE_SCHEMA),
    ]
    for hermes_schema, cowork_schema in pairs:
        assert cowork_schema == hermes_schema
    assert [t["name"] for t in mcp_server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})["result"]["tools"]] == [
        "memory_search", "memory_expand", "memory_evidence",
    ]
    for tool in mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})["result"]["tools"]:
        hermes_schema = next(
            s for s, _ in pairs if s["name"] == tool["name"])
        assert tool["inputSchema"] == hermes_schema["parameters"]
        assert tool["description"] == hermes_schema["description"]


def test_static_prompt_parity():
    assert runtime.STATIC_MEMORY_PROMPT == hermes_plugin.STATIC_MEMORY_PROMPT
    assert runtime.static_prompt() == hermes_plugin.STATIC_MEMORY_PROMPT
    skill = (PLUGIN_DIR / "skills" / "substrate-cowork-memory" / "SKILL.md").read_text()
    assert hermes_plugin.STATIC_MEMORY_PROMPT in skill


def test_core_constants_parity():
    assert cowork_client.SubstrateClient is not None
    assert cowork_onboarding.CLIENT_ID == "substrate-hermes"
    assert cowork_onboarding.SCOPES == "capture retrieve"
    assert cowork_onboarding.DEVICE_GRANT == "urn:ietf:params:oauth:grant-type:device_code"
    assert cowork_onboarding.ENV_KEY == "SUBSTRATE_API_KEY"
    assert cowork_onboarding.DEFAULT_ORIGIN == "https://vm-substrate-ar-01.taile961d2.ts.net:10000"
    assert cowork_contract.CONTRACT_VERSION == hermes_contract.CONTRACT_VERSION
    assert cowork_contract.SCHEMA_VERSION == hermes_contract.SCHEMA_VERSION
    assert cowork_contract.LIMITS == hermes_contract.LIMITS
    assert cowork_contract.FIXTURE_SHA256 == hermes_contract.FIXTURE_SHA256


def test_vendored_files_byte_identical():
    reference = REPO / "plugins" / "substrate"
    for name in ("contract.py", "ca/isrg-root-x1.pem", "ca/isrg-root-x2.pem",
                 "contract/envelope-fixtures.json"):
        assert (PLUGIN_DIR / "substrate_core" / name).read_bytes() == (
            reference / name).read_bytes()


def test_host_home_is_cowork_home(monkeypatch, tmp_path):
    home = hosthome.active_home()
    assert home == Path(os.environ["SUBSTRATE_HOME"]).resolve()
    monkeypatch.setenv("SUBSTRATE_HOME", str(tmp_path / "custom"))
    assert hosthome.active_home() == (tmp_path / "custom").resolve()
    assert cowork_client._profile_homes()[0] == (tmp_path / "custom").resolve()


def test_tool_behavior_parity_on_invalid_input():
    assert json.loads(runtime.memory_search({"query": ""})) == {"error": "invalid_request"}
    assert json.loads(runtime.memory_expand({"handle": "nope"})) == {"error": "invalid_request"}
    assert json.loads(
        runtime.memory_evidence({"handle": "m:12345678", "raw": "yes"})) == {
        "error": "invalid_request"}
    assert json.loads(hermes_plugin.memory_search({"query": ""})) == {"error": "invalid_request"}


def test_tool_positive_path_parity_with_mocked_client(monkeypatch):
    """Mocked post_json: Hermes vs Cowork shaped output must be byte-identical."""
    cv = cowork_contract.CONTRACT_VERSION
    raw = {
        "/api/v1/memory/search": {
            "contract_version": cv,
            "results": [{
                "handle": "m:12345678", "text": "hello world",
                "score": 0.5, "kind": "note", "markers": ["mk"],
            }],
            "junk": "dropped",
        },
        "/api/v1/memory/expand": {
            "contract_version": cv, "handle": "m:12345678",
            "kind": "note", "title": "Title", "abstract": "Abstract",
            "markdown": "Body",
        },
        "/api/v1/memory/evidence": {
            "contract_version": cv,
            "excerpts": [{"handle": "p:abcdef12", "text": "quote"}],
            "raw": "raw body",
        },
    }

    def fake_post_json(self, path, request, timeout=3.0):
        return dict(raw[path])

    monkeypatch.setattr(cowork_client.SubstrateClient, "post_json", fake_post_json)
    monkeypatch.setattr(hermes_client.SubstrateClient, "post_json", fake_post_json)
    cases = [
        (runtime.memory_search, hermes_plugin.memory_search, {"query": "hello"}),
        (runtime.memory_expand, hermes_plugin.memory_expand, {"handle": "m:12345678"}),
        (runtime.memory_evidence, hermes_plugin.memory_evidence,
         {"handle": "p:abcdef12"}),
    ]
    max_bytes = cowork_contract.LIMITS["max_tool_result_bytes"]
    for cowork_fn, hermes_fn, args in cases:
        cowork_out, hermes_out = cowork_fn(dict(args)), hermes_fn(dict(args))
        assert cowork_out == hermes_out, cowork_fn.__name__
        assert len(cowork_out.encode("utf-8")) <= max_bytes
        parsed = json.loads(cowork_out)
        assert "error" not in parsed, cowork_out
        assert parsed["contract_version"] == cv


def test_transcript_bridge_nonempty_fixture(tmp_path):
    """Real JSONL fixture: tool_use->tool_calls, tool_result->role:tool, envelope valid."""
    lines = [
        {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"content": [{"type": "text", "text": "what is X?"}]}},
        {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z",
         "message": {"content": [
             {"type": "text", "text": "let me check"},
             {"type": "tool_use", "id": "call-1", "name": "memory_search",
              "input": {"query": "X"}},
         ]}},
        {"type": "user", "timestamp": "2026-01-01T00:00:02Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "call-1",
                                  "content": "result text"}]}},
    ]
    fixture = tmp_path / "session.jsonl"
    fixture.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    history = transcript.history_for_context(str(fixture))
    assert len(history) >= 2
    assert history[0] == {"role": "user", "content": "what is X?"}
    messages = transcript.messages_for_capture(str(fixture))
    calls = [m["tool_calls"] for m in messages if m.get("tool_calls")]
    assert calls and calls[0][0]["id"] == "call-1"
    assert calls[0][0]["tool_name"] == "memory_search"
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "result text"
    assert tool_msgs[0]["tool_call_id"] == "call-1"
    envelope = runtime._capture_envelope(
        "what is X?", "let me check", session_id="s-9", messages=messages,
    )
    assert envelope is not None
    cowork_contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])


# --- redaction -----------------------------------------------------------

def test_redaction_matches_hermes():
    samples = [
        "Authorization: Bearer supersecretvalue",
        "api_key=supersecretvalue",
        "call with sk_abcdefghijklmnop inside",
        "password: hunter2-hunter2",
    ]
    for sample in samples:
        assert runtime._redact_text(sample) == hermes_plugin._redact_text(sample)
        assert "supersecretvalue" not in runtime._redact_text(sample)
        assert "hunter2" not in runtime._redact_text(sample)
    assert "[REDACTED]" in runtime._redact_text("token=abc123xyz")


def test_capture_redacts_sensitive_tool_args():
    envelope = runtime._capture_envelope(
        "do it", "done", session_id="s-1",
        messages=[{"role": "assistant", "content": "x",
                   "tool_calls": [{"id": "c1", "tool_name": "t",
                                   "args": {"api_key": "supersecretvalue"}}]}],
    )
    assert envelope is not None
    text = json.dumps(envelope)
    assert "supersecretvalue" not in text
    cowork_contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])


# --- capture envelope ----------------------------------------------------

def test_capture_envelope_validates():
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current question"},
    ]
    envelope = runtime._capture_envelope(
        "current question", "current answer", session_id="session-1", messages=history,
    )
    assert envelope is not None
    assert envelope["kind"] == "capture_turn"
    cowork_contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    session_envelope = runtime._session_envelope("session-1", "end", platform="cowork")
    assert session_envelope is not None
    cowork_contract.validate_envelope(
        session_envelope, idempotency_key=session_envelope["event_id"])


def test_transcript_bridge_shapes():
    path = PLUGIN_DIR / "skills" / "substrate-cowork-memory" / "SKILL.md"
    assert transcript.history_for_context(str(path)) == []
    assert transcript.messages_for_capture(str(path)) == []
    assert transcript.history_for_context("/nonexistent.jsonl") == []


# --- fail closed ----------------------------------------------------------

def test_retrieval_failure_returns_empty_context(monkeypatch):
    _refused(monkeypatch)
    assert runtime.get_memory_context("s-1", "hello", []) is None
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                               "params": {"name": "memory_search",
                                          "arguments": {"query": "hello"}}})
    assert json.loads(reply["result"]["content"][0]["text"]) == {"error": "transport_error"}


def test_hooks_fail_closed_and_emit_json(tmp_path):
    env = dict(os.environ)
    expected_prompt = {"hookSpecificOutput": {"additionalContext": runtime.static_prompt()}}
    for script, payload, expected in (
        ("session_start.py", b"{}", expected_prompt),
        ("pre_turn.py", b'{"session_id": "s-1", "prompt": "hi"}', {}),
        ("pre_turn.py", b"not json\n", {}),
        ("stop_capture.py", b'{"session_id": "s-1"}', {}),
        ("session_end.py", b'{"session_id": "s-1"}', {}),
    ):
        result = subprocess.run(
            [sys.executable, str(PLUGIN_DIR / "hooks" / script)],
            input=payload, capture_output=True, timeout=60, env=env,
        )
        assert result.returncode == 0, script
        assert json.loads(result.stdout.decode()) == expected, script
        assert FAKE_KEY not in result.stdout.decode() + result.stderr.decode(), script


def test_no_secret_ever_appears_in_output(monkeypatch):
    _refused(monkeypatch)
    outputs = [
        runtime.memory_search({"query": "hello"}),
        runtime.memory_expand({"handle": "m:12345678"}),
        runtime.memory_evidence({"handle": "p:abcdef12"}),
        json.dumps(runtime.get_memory_context("s-1", "hello", []) or {}),
        json.dumps(runtime._onboarding_notice({
            "verification_uri_complete": "https://x.example/oauth/device?user_code=AB",
            "user_code": "AB", "expires_in": 60, "agent_name": "cowork"})),
        json.dumps(mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                      "params": {}})),
    ]
    for output in outputs:
        assert FAKE_KEY not in output
    notice = runtime._onboarding_notice({
        "verification_uri_complete": "https://x.example/oauth/device?user_code=AB-CD",
        "user_code": "AB-CD", "expires_in": 60, "agent_name": "cowork"})
    assert "AB-CD" in notice
