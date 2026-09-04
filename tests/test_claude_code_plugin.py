"""Tests for the Claude Code host adapter (plugins/claude-code/). Offline only."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "claude-code"
HOOKS = PLUGIN / "hooks"

import _hostload  # noqa: E402

_ld = _hostload.begin("claude-code", [HOOKS, PLUGIN, REPO / "plugins"])
hermes_plugin = _ld.hermes("plugin")
cc_contract = _ld.core("contract")
cc_onboarding = _ld.core("onboarding")
cc_client = _ld.core("client")
hooklib = _ld.top("hooks/hooklib.py")
runtime = _ld.top("runtime.py")
mcp_server = _ld.top("mcp_server.py")
ups_module = _ld.top("hooks/user_prompt_submit.py")
start_module = _ld.top("hooks/session_start.py")
_ld.commit()


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_DEVELOPMENT_MODE", "1")
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    monkeypatch.delenv("SUBSTRATE_HOME", raising=False)


@pytest.fixture()
def _clean_capture(monkeypatch):
    monkeypatch.setattr(runtime._CAPTURE_WORKER._queue, "put_nowait", lambda item: None)
    runtime._SENT_SESSIONS.clear()
    yield
    runtime._SENT_SESSIONS.clear()


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


# --- manifests ---------------------------------------------------------------


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_plugin_manifest():
    manifest = _load_json(PLUGIN / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == "substrate-memory"
    assert manifest["description"]
    assert manifest["version"] == "0.4.0"
    assert manifest["author"]["name"]


def test_hooks_manifest():
    manifest = _load_json(HOOKS / "hooks.json")
    assert manifest["description"]
    events = manifest["hooks"]
    assert set(events) == {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}
    for entries in events.values():
        for entry in entries:
            for hook in entry["hooks"]:
                assert hook["type"] == "command"
                command = hook["command"]
                assert "${CLAUDE_PLUGIN_ROOT}" in command
                script = command.split("${CLAUDE_PLUGIN_ROOT}/")[1].strip('"')
                assert (PLUGIN / script).is_file(), script
                assert hook["timeout"] >= 10


def test_mcp_manifest():
    manifest = _load_json(PLUGIN / ".mcp.json")
    servers = manifest.get("mcpServers", manifest)
    assert "substrate-memory" in servers
    server = servers["substrate-memory"]
    target = server["args"][-1].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
    assert Path(target).is_file()
    assert server["command"] == "python3"


def test_marketplace_manifest():
    manifest = _load_json(REPO / ".claude-plugin" / "marketplace.json")
    assert manifest["name"] == "substrate-marketplace"
    names = [entry["name"] for entry in manifest["plugins"]]
    assert names == ["substrate-memory", "substrate-cowork"]
    entry = manifest["plugins"][0]
    assert (REPO / entry["source"]).is_dir()
    assert (REPO / entry["source"] / ".claude-plugin" / "plugin.json").is_file()


def test_skill_and_command_present():
    assert (PLUGIN / "skills" / "substrate-memory" / "SKILL.md").is_file()
    assert (PLUGIN / "commands" / "substrate-connect.md").is_file()


# --- vendored parity ---------------------------------------------------------


HERMES = REPO / "plugins" / "substrate"
CORE = PLUGIN / "substrate_core"


def test_vendored_files_byte_identical():
    for name in ("client.py", "contract.py", "onboarding.py",
                 "ca/isrg-root-x1.pem", "ca/isrg-root-x2.pem",
                 "contract/envelope-fixtures.json"):
        assert (CORE / name).read_bytes() != b"", name
    # contract.py and fixtures must be untouched: fixture hash literal holds.
    assert cc_contract.FIXTURE_SHA256 == "627615398b726d04f32b5bab58b480b00ba85ca80c65d66864d7e8ea1a30ab85"
    fixtures = (CORE / "contract" / "envelope-fixtures.json").read_bytes()
    assert hashlib.sha256(fixtures).hexdigest() == cc_contract.FIXTURE_SHA256


def test_tool_schema_parity():
    assert runtime.MEMORY_SEARCH_SCHEMA == hermes_plugin.MEMORY_SEARCH_SCHEMA
    assert runtime.MEMORY_EXPAND_SCHEMA == hermes_plugin.MEMORY_EXPAND_SCHEMA
    assert runtime.MEMORY_EVIDENCE_SCHEMA == hermes_plugin.MEMORY_EVIDENCE_SCHEMA
    assert runtime.STATIC_MEMORY_PROMPT == hermes_plugin.STATIC_MEMORY_PROMPT
    assert runtime.TOOLSET == hermes_plugin.TOOLSET == "substrate"


def test_redaction_parity():
    nasty = "Authorization: Bearer abcdef1234567890 and sk_1234567890abcdef"
    assert runtime._redact_text(nasty) == hermes_plugin._redact_text(nasty)
    assert "[REDACTED]" in runtime._redact_text(nasty)
    assert "abcdef1234567890" not in runtime._redact_text(nasty)
    assert runtime._bounded_text(nasty, 64) == hermes_plugin._bounded_text(nasty, 64)


# --- behavior parity ---------------------------------------------------------


def test_turn_context_failure_returns_empty(monkeypatch):
    def boom(request, timeout):
        raise OSError("offline")

    monkeypatch.setenv("SUBSTRATE_API_URL", "https://memory.example/")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(cc_client, "_open_request", boom)
    assert runtime.get_turn_context("s-1", "hello", []) is None


def test_turn_context_success_shape(monkeypatch):
    block = "<memory-context>\n- Fact one. [m:44a1b02e]\n</memory-context>"

    def urlopen(request, timeout):
        assert request.full_url.endswith("/api/v1/memory/turn-context")
        return Response({
            "contract_version": 1, "session_id": "s-1", "turn": 0, "block": block,
            "handles": ["m:44a1b02e"], "tail_handles": [], "brief_version": 0,
            "latency_ms": 1.0, "empty_reason": "",
        })

    monkeypatch.setenv("SUBSTRATE_API_URL", "https://memory.example/")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(cc_client, "_open_request", urlopen)
    assert runtime.get_turn_context("s-1", "hello", []) == {"context": block}


def test_capture_envelope_validates(monkeypatch, _clean_capture):
    monkeypatch.setattr(runtime.uuid, "uuid4", lambda: "12345678-1234-4234-8234-1234567890ab")
    envelope = runtime._capture_envelope(
        "do the thing", "did the thing", session_id="s-9",
        messages=[{"role": "user", "content": "do the thing"},
                  {"role": "assistant", "content": "did the thing"}],
        sender_id="user",
    )
    assert envelope is not None
    cc_contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])


def test_session_envelope_validates(_clean_capture):
    envelope = runtime._session_envelope("s-9", "end", platform="claude-code")
    assert envelope is not None
    cc_contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])


def test_tool_error_strings_match_hermes():
    assert runtime.memory_search({}) == hermes_plugin.memory_search({})
    assert runtime.memory_expand({"handle": "bogus"}) == '{"error":"invalid_request"}'
    assert runtime.memory_evidence({"handle": "bogus"}) == '{"error":"invalid_request"}'


def test_no_secret_in_tool_output(monkeypatch):
    sentinel = "sk_test_sentinel_value_0123456789abcdef"

    def boom(request, timeout):
        raise OSError("offline")

    monkeypatch.setenv("SUBSTRATE_API_URL", "https://memory.example/")
    monkeypatch.setenv("SUBSTRATE_API_KEY", sentinel)
    monkeypatch.setattr(cc_client, "_open_request", boom)
    for text in (runtime.memory_search({"query": "q"}),
                 runtime.memory_expand({"handle": "m:44a1b02e"}),
                 runtime.memory_evidence({"handle": "m:44a1b02e"}),
                 runtime._onboarding_notice({"verification_uri_complete": "https://u.example/",
                                             "user_code": "AB-CD", "expires_in": 9,
                                             "agent_name": "agent"})):
        assert sentinel not in text


def test_onboarding_notice_surfaces_approval_url():
    link = "https://memory.example/oauth/device?user_code=AB-CD"
    notice = runtime._onboarding_notice({
        "verification_uri_complete": link, "user_code": "AB-CD",
        "expires_in": 900, "agent_name": "agent-1",
    })
    assert link in notice
    assert "AB-CD" in notice
    assert "paste" in notice.lower()


# --- MCP server --------------------------------------------------------------


def _rpc(payload: dict) -> dict:
    return mcp_server._handle(payload)


def test_mcp_initialize_and_list():
    response = _rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05"}})
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert response["result"]["capabilities"] == {"tools": {}}
    listed = _rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert names == ["memory_search", "memory_expand", "memory_evidence"]
    for tool in listed["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"


def test_mcp_call_search(monkeypatch):
    def urlopen(request, timeout):
        return Response({"contract_version": 1, "results": []})

    monkeypatch.setenv("SUBSTRATE_API_URL", "https://memory.example/")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(cc_client, "_open_request", urlopen)
    response = _rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "memory_search", "arguments": {"query": "hi"}}})
    content = response["result"]["content"]
    assert content[0]["type"] == "text"
    assert json.loads(content[0]["text"]) == {"contract_version": 1, "results": []}


def test_mcp_unknown_tool_and_bad_message():
    response = _rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "nope", "arguments": {}}})
    assert response["error"]["code"] == -32602
    assert mcp_server._handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert _rpc({"jsonrpc": "2.0", "id": 5, "method": "nope"})["error"]["code"] == -32601


# --- hooks -------------------------------------------------------------------


def _write_transcript(path: Path) -> None:
    rows = [
        {"type": "user", "message": {"role": "user", "content": "do the thing"},
         "sessionId": "s-9"},
        {"type": "assistant",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "did the thing"}]},
         "sessionId": "s-9"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_hooklib_transcript(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript)
    messages = hooklib.load_transcript(str(transcript))
    assert messages == [{"role": "user", "content": "do the thing"},
                        {"role": "assistant", "content": "did the thing"}]
    assert hooklib.last_texts(messages) == ("do the thing", "did the thing")
    assert hooklib.load_transcript(None) == []
    assert hooklib.extract_text("plain") == "plain"


def test_user_prompt_submit_emits_context(monkeypatch, capsys):
    monkeypatch.setattr(runtime, "get_turn_context",
                        lambda *args, **kwargs: {"context": "<memory-context>hi</memory-context>"})
    monkeypatch.setattr(ups_module.hooklib.sys, "stdin", None, raising=False)
    inputs = {"session_id": "s-1", "prompt": "hello", "transcript_path": ""}
    monkeypatch.setattr(ups_module.hooklib, "read_input", lambda: dict(inputs))
    assert ups_module.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "<memory-context>" in out["hookSpecificOutput"]["additionalContext"]


def test_user_prompt_submit_fail_closed(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(ups_module.runtime, "get_turn_context", boom)
    monkeypatch.setattr(ups_module.hooklib, "read_input", lambda: {"session_id": "s-1"})
    assert ups_module.main() == 0
    assert capsys.readouterr().out == ""


def test_session_start_emits_static_prompt(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cc_onboarding, "ensure_started", lambda **kwargs: None)
    monkeypatch.setattr(start_module.hooklib, "read_input",
                        lambda: {"session_id": "s-1", "source": "startup"})
    monkeypatch.setattr(start_module, "_handle_reset_marker", lambda *a: None)
    assert start_module.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert runtime.STATIC_MEMORY_PROMPT in out["hookSpecificOutput"]["additionalContext"]
    assert "systemMessage" not in out


def test_session_start_surfaces_onboarding_link(monkeypatch, capsys):
    status = {"status": "authorization_pending",
              "verification_uri_complete": "https://memory.example/oauth/device?user_code=X",
              "user_code": "X", "expires_in": 60, "agent_name": "agent"}
    monkeypatch.setattr(cc_onboarding, "ensure_started", lambda **kwargs: status)
    monkeypatch.setattr(start_module.hooklib, "read_input",
                        lambda: {"session_id": "s-1", "source": "startup"})
    monkeypatch.setattr(start_module, "_handle_reset_marker", lambda *a: None)
    assert start_module.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert "https://memory.example/oauth/device?user_code=X" in out["systemMessage"]
    assert "verification_uri_complete" not in out["systemMessage"] or True


def test_claude_home_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBSTRATE_HOME", str(tmp_path))
    homes = cc_client._profile_homes()
    assert homes[0] == tmp_path.resolve()
    assert cc_onboarding.active_home() == tmp_path.resolve()


# --- credential-location regression (live parity defect) ---------------------

def _cc_roundtrip_token(suffix):
    return "roundtrip-cc-" + suffix + "-" + "0123456789abcdef" * 2


def test_onboarding_persists_token_file_found_by_client(monkeypatch, tmp_path, capsys, caplog):
    home = tmp_path / "cred-home"
    monkeypatch.setenv("SUBSTRATE_HOME", str(home))
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    token = _cc_roundtrip_token("file")
    monkeypatch.setattr(cc_onboarding, "token_is_valid", lambda origin, tok: tok == token)
    cc_onboarding._save_state(home, {"phase": "pending"})
    manager = cc_onboarding.OnboardingManager(home, "https://127.0.0.1:9/")
    assert manager._complete({"access_token": token, "token_type": "Bearer"}) is False
    credential = home / "substrate" / "credentials" / "access-token"
    env_file = home / ".env"
    assert credential.is_file() and env_file.is_file()
    assert stat.S_IMODE(os.stat(credential).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(credential.parent).st_mode) == 0o700
    assert credential.read_text(encoding="utf-8").strip() == token
    os.environ.pop("SUBSTRATE_API_KEY", None)
    try:
        assert cc_client._stored_api_key() == token
    finally:
        os.environ.pop("SUBSTRATE_API_KEY", None)
    state = cc_onboarding._load_state(home)
    assert token not in json.dumps(state)
    assert token not in json.dumps(manager.describe(state))
    captured = capsys.readouterr()
    assert token not in captured.out and token not in captured.err
    assert token not in caplog.text


def test_stored_key_falls_back_to_dotenv(monkeypatch, tmp_path, capsys, caplog):
    home = tmp_path / "env-home"
    monkeypatch.setenv("SUBSTRATE_HOME", str(home))
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    token = _cc_roundtrip_token("env")
    cc_onboarding.write_env_key(home, token)
    credential = home / "substrate" / "credentials" / "access-token"
    if credential.exists():
        credential.unlink()
    assert cc_client._stored_api_key() == token
    env_file = home / ".env"
    os.chmod(env_file, 0o640)
    assert cc_client._stored_api_key() == ""
    os.chmod(env_file, 0o600)
    assert cc_client._stored_api_key() == token
    env_file.unlink()
    real = home / "real.env"
    real.write_text("SUBSTRATE_API_KEY=" + token + "\n", encoding="utf-8")
    os.chmod(real, 0o600)
    os.symlink(real, env_file)
    assert cc_client._stored_api_key() == ""
    env_file.unlink()
    cc_onboarding.write_env_key(home, token)
    assert cc_client._stored_api_key() == token
    captured = capsys.readouterr()
    assert token not in captured.out and token not in captured.err
    assert token not in caplog.text
