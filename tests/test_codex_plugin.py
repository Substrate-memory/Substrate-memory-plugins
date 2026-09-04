"""Tests for the Substrate Codex CLI plugin (offline, stdlib + pytest only)."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CODEX_DIR = REPOSITORY_ROOT / "plugins" / "codex"
HERMES_DIR = REPOSITORY_ROOT / "plugins" / "substrate"


def _load_package(alias: str, directory: Path) -> ModuleType:
    package = ModuleType(alias)
    package.__path__ = [str(directory)]
    sys.modules[alias] = package
    for name in ("contract", "client", "onboarding", "plugin", "runtime"):
        candidate = directory / f"{name}.py"
        if not candidate.exists():
            continue
        spec = importlib.util.spec_from_file_location(f"{alias}.{name}", candidate)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{alias}.{name}"] = module
        spec.loader.exec_module(module)
    return package


codex = _load_package("codex_substrate_core", CODEX_DIR / "substrate_core")
hermes = _load_package("hermes_substrate", HERMES_DIR)

codex_runtime = sys.modules["codex_substrate_core.runtime"]
codex_contract = sys.modules["codex_substrate_core.contract"]
codex_onboarding = sys.modules["codex_substrate_core.onboarding"]
codex_client = sys.modules["codex_substrate_core.client"]
hermes_plugin = sys.modules["hermes_substrate.plugin"]
hermes_onboarding = sys.modules["hermes_substrate.onboarding"]
hermes_contract = sys.modules["hermes_substrate.contract"]


def _load_hook_module(name: str, filename: str) -> ModuleType:
    """Load a hook script against the Codex runtime instance under test.

    ``hooks/*.py`` does ``from substrate_core import ...``; seeding those
    top-level names with the already-loaded Codex modules keeps the hook on
    the same runtime object the parity tests use (and the same one these
    tests monkeypatch).
    """
    sys.modules["substrate_core"] = codex
    sys.modules["substrate_core.runtime"] = codex_runtime
    sys.modules["substrate_core.contract"] = codex_contract
    sys.modules["substrate_core.client"] = codex_client
    sys.modules["substrate_core.onboarding"] = codex_onboarding
    spec = importlib.util.spec_from_file_location(name, CODEX_DIR / "hooks" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


substrate_hook = _load_hook_module("codex_substrate_hook", "substrate_hook.py")
send_spooled = _load_hook_module("codex_send_spooled", "_send_spooled.py")


# Real Codex CLI wire payloads (shapes from codex-cli 0.144.5; see README).
REAL_PRE_PAYLOAD = {
    "hook_event_name": "UserPromptSubmit",
    "session_id": "sess-1",
    "prompt": "do the thing",
    "transcript_path": None,
    "turn_id": "turn-9",
    "cwd": "/tmp",
    "model": "gpt-5.6-sol",
    "permission_mode": "default",
}
REAL_POST_PAYLOAD = {
    "hook_event_name": "PostToolUse",
    "session_id": "sess-1",
    "tool_name": "shell",
    "tool_input": {"command": "ls"},
    "tool_response": "ok",
    "tool_use_id": "tu-1",
    "transcript_path": None,
    "turn_id": "turn-9",
    "cwd": "/tmp",
    "model": "gpt-5.6-sol",
    "permission_mode": "default",
}
REAL_SESSION_PAYLOAD = {
    "hook_event_name": "SessionStart",
    "session_id": "sess-1",
    "source": "startup",
    "transcript_path": None,
    "cwd": "/tmp",
    "model": "gpt-5.6-sol",
    "permission_mode": "default",
}


def _write_transcript(path: Path) -> None:
    rows = [
        {
            "timestamp": "2026-09-04T04:50:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "do the thing"}],
            },
        },
        {
            "timestamp": "2026-09-04T04:50:03.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


# ---------------------------------------------------------------------------
# Manifests and native layout
# ---------------------------------------------------------------------------


def test_plugin_manifest_is_valid() -> None:
    manifest = json.loads((CODEX_DIR / ".codex-plugin" / "plugin.json").read_text())
    assert isinstance(manifest, dict)
    for key in ("name", "version", "description", "skills", "mcpServers", "interface"):
        assert manifest[key], key
    assert manifest["name"] == "substrate"
    assert manifest["version"] == "0.4.0"
    assert manifest["skills"] == "./skills/"
    assert "hooks" not in manifest  # rejected by Codex validation; hooks/ is discovered
    assert manifest["mcpServers"] == "./.mcp.json"
    interface = manifest["interface"]
    for key in ("displayName", "shortDescription", "longDescription", "developerName",
                "category", "capabilities", "defaultPrompt"):
        assert interface[key], key


def test_mcp_manifest_points_at_server() -> None:
    payload = json.loads((CODEX_DIR / ".mcp.json").read_text())
    assert set(payload) == {"mcpServers"}
    servers = payload["mcpServers"]
    assert set(servers) == {"substrate"}
    assert (CODEX_DIR / "server.py").exists()


def test_hooks_manifest_references_real_scripts() -> None:
    hooks = json.loads((CODEX_DIR / "hooks" / "hooks.json").read_text())
    assert isinstance(hooks.get("hooks"), dict)
    seen = set()
    for event, entries in hooks["hooks"].items():
        assert isinstance(entries, list) and entries
        for entry in entries:
            command = entry["command"]
            assert "substrate_hook.py" in command
            seen.add(event)
    assert {"UserPromptSubmit", "PostToolUse", "SessionStart"} <= seen
    assert (CODEX_DIR / "hooks" / "substrate_hook.py").exists()
    assert (CODEX_DIR / "hooks" / "_send_spooled.py").exists()


def test_skill_carries_static_prompt() -> None:
    text = (CODEX_DIR / "skills" / "substrate-memory" / "SKILL.md").read_text()
    assert text.startswith("---\n")
    assert "substrate-memory" in text.split("---")[1]
    assert codex_runtime.STATIC_MEMORY_PROMPT in text


# ---------------------------------------------------------------------------
# Parity with the Hermes reference plugin
# ---------------------------------------------------------------------------


def test_static_prompt_parity() -> None:
    assert codex_runtime.STATIC_MEMORY_PROMPT == hermes_plugin.STATIC_MEMORY_PROMPT


def test_tool_schema_parity() -> None:
    assert codex_runtime.MEMORY_SEARCH_SCHEMA == hermes_plugin.MEMORY_SEARCH_SCHEMA
    assert codex_runtime.MEMORY_EXPAND_SCHEMA == hermes_plugin.MEMORY_EXPAND_SCHEMA
    assert codex_runtime.MEMORY_EVIDENCE_SCHEMA == hermes_plugin.MEMORY_EVIDENCE_SCHEMA
    assert codex_runtime.TOOLSET == hermes_plugin.TOOLSET == "substrate"


def test_tool_names_and_error_shapes() -> None:
    assert codex_runtime.memory_search({}) == hermes_plugin.memory_search({})
    assert codex_runtime.memory_expand({"handle": "bogus"}) == hermes_plugin.memory_expand(
        {"handle": "bogus"}
    )
    assert codex_runtime.memory_evidence({"handle": "bogus"}) == hermes_plugin.memory_evidence(
        {"handle": "bogus"}
    )


def test_vendored_core_matches_reference() -> None:
    for name in (
        "contract.py",
        "ca/isrg-root-x1.pem",
        "ca/isrg-root-x2.pem",
        "contract/envelope-fixtures.json",
    ):
        assert (CODEX_DIR / "substrate_core" / name).read_bytes() == (
            HERMES_DIR / name
        ).read_bytes(), name
    for name in ("client.py", "onboarding.py"):
        text = (CODEX_DIR / "substrate_core" / name).read_text()
        assert "HOST-HOME PATCH" in text, name


# ---------------------------------------------------------------------------
# Behavior: redaction, envelopes, fail-closed retrieval, no secrets
# ---------------------------------------------------------------------------


def test_redaction_matches_reference() -> None:
    samples = [
        "api_key: hunter2-value",
        "Authorization: Bearer abcdefgh",
        "token=sk_abcdefgh12345678",
        "password: s3cret!",
        "nothing sensitive here",
    ]
    for sample in samples:
        assert codex_runtime._redact_text(sample) == hermes_plugin._redact_text(sample)
    assert "[REDACTED]" in codex_runtime._redact_text("api_key: hunter2-value")
    assert "hunter2-value" not in codex_runtime._redact_text("api_key: hunter2-value")
    assert "abcdefgh12345678" not in codex_runtime._redact_text("token=sk_abcdefgh12345678")


def test_capture_envelope_validates() -> None:
    envelope = codex_runtime._capture_envelope(
        "do the thing",
        "done",
        session_id="session-1",
        messages=[
            {"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": "done"},
        ],
    )
    assert envelope is not None
    codex_contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    assert hermes_plugin._capture_envelope(
        "do the thing",
        "done",
        session_id="session-1",
        messages=[
            {"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": "done"},
        ],
    ) is not None


def test_session_envelope_validates() -> None:
    envelope = codex_runtime._session_envelope("session-1", "end")
    assert envelope is not None
    codex_contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])


def test_retrieval_failure_returns_empty_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from codex_substrate_core.client import ClientError

    def _boom(self: object, *args: object, **kwargs: object) -> object:
        raise ClientError("transport_error")

    monkeypatch.setattr(
        sys.modules["codex_substrate_core.client"].SubstrateClient, "post_json", _boom
    )
    # Stay hermetic: never let the failure path touch the network.
    monkeypatch.setattr(
        sys.modules["codex_substrate_core.onboarding"],
        "ensure_started",
        lambda *args, **kwargs: {"status": "failed", "error_class": "transport_error"},
    )
    assert codex_runtime.pre_llm_call(session_id="s", user_message="hi") is None
    assert codex_runtime.codex_pre_turn_text(session_id="s", user_message="hi") is None


def test_no_secret_ever_appears_in_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "unit-test-sentinel-secret-value-0123456789"
    monkeypatch.setenv("SUBSTRATE_API_KEY", sentinel)
    from codex_substrate_core.client import ClientError

    def _boom(self: object, *args: object, **kwargs: object) -> object:
        raise ClientError("timeout")

    monkeypatch.setattr(
        sys.modules["codex_substrate_core.client"].SubstrateClient, "post_json", _boom
    )
    result = codex_runtime.memory_search({"query": "hello"})
    assert sentinel not in result
    assert json.loads(result) == {"error": "timeout"}
    assert codex_runtime.pre_llm_call(session_id="s", user_message="hi") is None
    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def test_codex_home_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from codex_substrate_core import hosthome

    monkeypatch.setenv("SUBSTRATE_HOME", str(tmp_path))
    assert hosthome.codex_home() == tmp_path.resolve()
    assert hosthome.credential_path().name == "access-token"


# ---------------------------------------------------------------------------
# MCP stdio server (offline protocol check)
# ---------------------------------------------------------------------------


def _mcp_exchange(requests: list[dict[str, object]]) -> list[dict[str, object]]:
    payload = "\n".join(json.dumps(item) for item in requests) + "\n"
    completed = subprocess.run(
        [sys.executable, str(CODEX_DIR / "server.py")],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(CODEX_DIR),
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "sk_" not in completed.stderr
    return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]


def test_mcp_server_lists_parity_tools() -> None:
    responses = _mcp_exchange(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
    )
    assert responses[0]["result"]["serverInfo"]["name"] == "substrate-memory"
    tools = {tool["name"]: tool for tool in responses[1]["result"]["tools"]}
    assert set(tools) == {"memory_search", "memory_expand", "memory_evidence"}
    for name in tools:
        schema = getattr(hermes_plugin, f"{name.upper()}_SCHEMA")
        assert tools[name]["description"] == schema["description"]
        assert tools[name]["inputSchema"] == schema["parameters"]


def test_mcp_server_rejects_bad_handle_offline() -> None:
    (response,) = _mcp_exchange(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "memory_expand", "arguments": {"handle": "bogus"}},
            }
        ]
    )
    text = response["result"]["content"][0]["text"]
    assert json.loads(text) == {"error": "invalid_request"}


# ---------------------------------------------------------------------------
# Hook wire contract (real Codex payloads, exact response shape)
# ---------------------------------------------------------------------------


def test_pre_emits_enveloped_context_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pre hook must emit hookSpecificOutput.hookEventName, never bare."""
    monkeypatch.setattr(
        codex_runtime, "codex_pre_turn_text", lambda **kwargs: "<memory-context>x</memory-context>"
    )
    assert substrate_hook.run_pre(dict(REAL_PRE_PAYLOAD)) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert set(payload) == {"hookSpecificOutput"}
    assert payload == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "<memory-context>x</memory-context>",
        }
    }


def test_pre_forwards_real_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    def _fake(**kwargs: object) -> str | None:
        seen.update(kwargs)
        return None

    monkeypatch.setattr(codex_runtime, "codex_pre_turn_text", _fake)
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(transcript)
    payload = dict(REAL_PRE_PAYLOAD, transcript_path=str(transcript))
    assert substrate_hook.run_pre(payload) == 0
    assert capsys.readouterr().out == ""  # nothing to inject -> silent, exit 0
    assert seen["session_id"] == "sess-1"
    assert seen["turn_id"] == "turn-9"
    assert seen["user_message"] == "do the thing"
    history = seen["conversation_history"]
    assert isinstance(history, list) and len(history) == 2
    assert history[0] == {"role": "user", "content": "do the thing"}
    assert history[1] == {"role": "assistant", "content": "done"}


def test_post_spool_is_stable_private_and_valid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repeated PostToolUse fires for one turn share one Idempotency-Key."""
    monkeypatch.setattr(codex_onboarding, "active_home", lambda: tmp_path)
    monkeypatch.setattr(substrate_hook.subprocess, "Popen", lambda *a, **k: None)
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(transcript)
    payload = dict(REAL_POST_PAYLOAD, transcript_path=str(transcript))
    assert substrate_hook.run_post(payload) == 0
    spool = tmp_path / "substrate" / "spool-codex"
    assert spool.is_dir()
    assert (spool.stat().st_mode & 0o777) == 0o700
    assert substrate_hook.run_post(payload) == 0  # duplicate fire
    files = sorted(spool.glob("*.json"))
    assert len(files) == 1  # stable filename per event id
    assert (files[0].stat().st_mode & 0o777) == 0o600
    envelope = json.loads(files[0].read_text())
    codex_contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    assert envelope["kind"] == "capture_turn"
    assert envelope["session_id"] == "sess-1"
    assert envelope["payload"]["turn_id"] == "turn-9"
    assert envelope["payload"]["messages"] == [
        {"index": 0, "role": "user", "content": "do the thing"},
        {"index": 1, "role": "assistant", "content": "done"},
    ]
    first_id = envelope["event_id"]
    assert substrate_hook._stable_event_id("capture_turn", "sess-1", "turn-9") == first_id


def test_post_empty_payload_spools_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(codex_onboarding, "active_home", lambda: tmp_path)
    monkeypatch.setattr(substrate_hook.subprocess, "Popen", lambda *a, **k: None)
    assert substrate_hook.run_post({}) == 0
    assert substrate_hook.run_post(None) == 0
    spool = tmp_path / "substrate" / "spool-codex"
    assert not spool.exists() or list(spool.glob("*.json")) == []


@pytest.mark.parametrize(
    ("source", "boundary"),
    [
        ("startup", "switch"),
        ("resume", "switch"),
        ("clear", "reset"),
        ("compact", "compress"),
        ("bogus", "switch"),
        (None, "switch"),
    ],
)
def test_session_source_maps_to_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: object, boundary: str
) -> None:
    monkeypatch.setattr(codex_onboarding, "active_home", lambda: tmp_path)
    monkeypatch.setattr(substrate_hook.subprocess, "Popen", lambda *a, **k: None)
    payload = dict(REAL_SESSION_PAYLOAD, source=source)
    assert substrate_hook.run_session(payload, "switch") == 0
    (path,) = (tmp_path / "substrate" / "spool-codex").glob("*.json")
    envelope = json.loads(path.read_text())
    codex_contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    assert envelope["kind"] == "capture_session"
    assert envelope["payload"]["boundary"] == boundary
    path.unlink()


def test_transcript_reader_is_best_effort(tmp_path: Path) -> None:
    assert substrate_hook._read_transcript_messages(str(tmp_path / "missing.jsonl")) == []
    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("not json\n{}\n")
    assert substrate_hook._read_transcript_messages(str(garbage)) == []
    assert substrate_hook._read_transcript_messages("") == []
    assert substrate_hook._read_transcript_messages(None) == []  # type: ignore[arg-type]


def test_sender_sweeps_stale_spool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The detached sender delivers aged files, expires old ones, drops junk."""
    import time as _time

    monkeypatch.setattr(codex_onboarding, "active_home", lambda: tmp_path)
    spool = tmp_path / "substrate" / "spool-codex"
    spool.mkdir(parents=True, mode=0o700)
    sent: list[tuple[dict[str, object], str]] = []

    class _Client:
        @classmethod
        def from_env(cls) -> "_Client":
            return cls()

        def post_json(
            self, path: str, body: object, **kwargs: object
        ) -> dict[str, object]:
            assert kwargs.get("idempotency_key") == body["event_id"]  # type: ignore[index]
            sent.append((body, path))  # type: ignore[arg-type]
            return {}

    monkeypatch.setattr(send_spooled, "SubstrateClient", _Client)
    good = codex_runtime._capture_envelope(
        "do the thing",
        "done",
        session_id="sess-1",
        messages=[{"role": "user", "content": "do the thing"}],
        turn_id="turn-9",
    )
    assert good is not None
    fresh = spool / "fresh.json"
    fresh.write_text(json.dumps(good))
    stale = spool / "stale.json"
    stale.write_text(json.dumps(good))
    old = _time.time() - (8 * 24 * 3600)
    import os as _os

    _os.utime(stale, (old, old))
    junk = spool / "junk.json"
    junk.write_text(json.dumps({"no": "event_id"}))
    assert send_spooled.main(["_send_spooled.py"]) == 0
    assert not (spool / "fresh.json").exists()  # sent, then unlinked
    assert not stale.exists()  # expired without sending
    assert not junk.exists()  # invalid id dropped
    assert sent, "expected at least one delivery"
    assert all(isinstance(body, dict) for body, _ in sent)


def test_hooks_manifest_shape_is_cli_safe() -> None:
    """hooks.json must parse under the CLI rules: no $schema, real events."""
    hooks = json.loads((CODEX_DIR / "hooks" / "hooks.json").read_text())
    assert set(hooks) == {"hooks"}  # Codex rejects top-level "$schema"
    assert {"UserPromptSubmit", "PostToolUse", "SessionStart"} <= set(hooks["hooks"])
    for event, entries in hooks["hooks"].items():
        assert isinstance(entries, list) and entries
        for entry in entries:
            assert "substrate_hook.py" in entry["command"]
    session_cmd = hooks["hooks"]["SessionStart"][0]["command"]
    assert session_cmd.rstrip().endswith("session")  # source comes from payload


def test_security_constants_match_hermes() -> None:
    for name in ("CLIENT_ID", "SCOPES", "DEVICE_GRANT", "ENV_KEY", "DEFAULT_ORIGIN"):
        assert getattr(codex_onboarding, name) == getattr(hermes_onboarding, name), name
    assert codex_contract.LIMITS == hermes_contract.LIMITS


# --- credential-location regression (live parity defect) ---------------------

def _codex_roundtrip_token(suffix):
    return "roundtrip-cx-" + suffix + "-" + "0123456789abcdef" * 2


def test_onboarding_persists_token_file_found_by_client(monkeypatch, tmp_path, capsys, caplog):
    home = tmp_path / "cred-home"
    monkeypatch.setenv("SUBSTRATE_HOME", str(home))
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    token = _codex_roundtrip_token("file")
    monkeypatch.setattr(codex_onboarding, "token_is_valid", lambda origin, tok: tok == token)
    codex_onboarding._save_state(home, {"phase": "pending"})
    manager = codex_onboarding.OnboardingManager(home, "https://127.0.0.1:9/")
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
        assert codex_client._stored_api_key() == token
    finally:
        os.environ.pop("SUBSTRATE_API_KEY", None)
    state = codex_onboarding._load_state(home)
    assert token not in json.dumps(state)
    assert token not in json.dumps(manager.describe(state))
    captured = capsys.readouterr()
    assert token not in captured.out and token not in captured.err
    assert token not in caplog.text


def test_stored_key_falls_back_to_dotenv(monkeypatch, tmp_path, capsys, caplog):
    home = tmp_path / "env-home"
    monkeypatch.setenv("SUBSTRATE_HOME", str(home))
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    token = _codex_roundtrip_token("env")
    codex_onboarding.write_env_key(home, token)
    credential = home / "substrate" / "credentials" / "access-token"
    if credential.exists():
        credential.unlink()
    assert codex_client._stored_api_key() == token
    env_file = home / ".env"
    os.chmod(env_file, 0o640)
    assert codex_client._stored_api_key() == ""
    os.chmod(env_file, 0o600)
    assert codex_client._stored_api_key() == token
    env_file.unlink()
    real = home / "real.env"
    real.write_text("SUBSTRATE_API_KEY=" + token + "\n", encoding="utf-8")
    os.chmod(real, 0o600)
    os.symlink(real, env_file)
    assert codex_client._stored_api_key() == ""
    env_file.unlink()
    codex_onboarding.write_env_key(home, token)
    assert codex_client._stored_api_key() == token
    captured = capsys.readouterr()
    assert token not in captured.out and token not in captured.err
    assert token not in caplog.text
