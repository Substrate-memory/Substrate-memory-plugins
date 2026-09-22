"""Tests for the substrate-claude plugin (Cowork + Claude Code, one package)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUG = REPO / "plugins" / "substrate-claude"
SCRIPT = PLUG / "scripts" / "substrate_sync.py"
FIX = REPO / "tests" / "fixtures" / "substrate-claude"

HERMES_SRC = str(REPO / "plugins" / "substrate-hermes" / "src")
if HERMES_SRC not in sys.path:
    sys.path.insert(0, HERMES_SRC)

from substrate import contract as c  # noqa: E402

SERVER = "plugin:substrate-claude:substrate-memory"

# event -> (tool, required input keys with ${} placeholders or literals)
CONTRACT_TABLE = {
    "UserPromptSubmit": ("memory_turn_context", {"session_id": "${session_id}",
                                                 "prompt": "${prompt}",
                                                 "platform": "claude"}),
    "PostToolUse": ("memory_capture_tool", {"session_id": "${session_id}",
                                            "tool_use_id": "${tool_use_id}",
                                            "tool_name": "${tool_name}",
                                            "tool_input": "${tool_input}",
                                            "tool_response": "${tool_response}",
                                            "platform": "claude"}),
    "Stop": ("memory_capture_turn", {"session_id": "${session_id}",
                                     "assistant_message": "${last_assistant_message}",
                                     "agent_context": "main",
                                     "platform": "claude"}),
    "SubagentStop": ("memory_capture_turn", {"session_id": "${session_id}",
                                             "assistant_message": "${last_assistant_message}",
                                             "agent_context": "subagent",
                                             "agent_id": "${agent_id}",
                                             "parent_session_id": "${session_id}",
                                             "platform": "claude"}),
}


def _load_sync():
    spec = importlib.util.spec_from_file_location("substrate_sync", str(SCRIPT))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_shape() -> None:
    manifest = json.loads((PLUG / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "substrate-claude"
    assert manifest["version"] == "0.7.0"
    assert manifest["author"]["name"] == "Sightline Technologies Inc"
    assert manifest["description"].strip()
    assert manifest["homepage"].startswith("https://")


def test_mcp_shape() -> None:
    mcp = json.loads((PLUG / ".mcp.json").read_text())
    servers = mcp["mcpServers"]
    assert set(servers) == {"substrate-memory"}
    server = servers["substrate-memory"]
    assert server["type"] == "http"
    assert server["url"] == "https://app.trysubstrate.co/mcp"


def _hook_entries():
    hooks = json.loads((PLUG / "hooks" / "hooks.json").read_text())
    assert set(hooks) == {"hooks"}
    return hooks["hooks"]


def test_every_hook_maps_to_contract_table() -> None:
    entries = _hook_entries()
    for event, (tool, args) in CONTRACT_TABLE.items():
        assert event in entries, event
        found = False
        for group in entries[event]:
            for hook in group["hooks"]:
                if hook.get("tool") != tool:
                    continue
                assert hook["type"] == "mcp_tool"
                assert hook["server"] == SERVER
                for key, value in args.items():
                    assert hook["input"].get(key) == value, (event, key)
                found = True
        assert found, event


def test_post_tool_use_has_no_matcher() -> None:
    entries = _hook_entries()
    for group in entries["PostToolUse"]:
        assert group.get("matcher", "") in ("", "*"), group


def test_session_boundaries() -> None:
    entries = _hook_entries()
    by_matcher = {}
    for group in entries["SessionStart"]:
        by_matcher[group["matcher"]] = group["hooks"][0]
    assert set(by_matcher) == {"startup", "resume", "clear", "compact"}
    for matcher, hook in by_matcher.items():
        assert hook["tool"] == "memory_session_boundary"
        assert hook["server"] == SERVER
        assert hook["input"]["boundary"] == "${source}"
        assert hook["input"]["session_id"] == "${session_id}"
        assert hook["input"]["platform"] == "claude"
    pre = entries["PreCompact"][0]["hooks"][0]
    assert pre["tool"] == "memory_session_boundary"
    assert pre["input"]["boundary"] == "compact"
    end = entries["SessionEnd"][0]["hooks"][0]
    assert end["tool"] == "memory_session_boundary"
    assert end["input"]["boundary"] == "end"
    assert end["input"]["reason"] == "${reason}"
    assert end["timeout"] == 3


def test_hook_timeouts() -> None:
    entries = _hook_entries()
    for event in ("UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop"):
        for group in entries[event]:
            for hook in group["hooks"]:
                assert hook["timeout"] == 5, event


def _run_script(host: str, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(FIX / "claude-home")
    env["CODEX_HOME"] = str(FIX / "codex-home")
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(SCRIPT), "--host", host, *args],
                          capture_output=True, text=True, timeout=120, env=env)


@pytest.fixture(scope="module")
def fixture_homes(tmp_path_factory: pytest.TempPathFactory):
    # Build sandbox homes from the checked-in fixtures (read-only source).
    import shutil
    tmp = tmp_path_factory.mktemp("sync-homes")
    claude_projects = tmp / "claude-home" / "projects" / "proj-test"
    codex_sessions = tmp / "codex-home" / "sessions"
    claude_projects.mkdir(parents=True)
    codex_sessions.mkdir(parents=True)
    shutil.copy(str(FIX / "claude-projects" / "proj-test" / "test-claude-session-01.jsonl"),
                str(claude_projects / "test-claude-session-01.jsonl"))
    shutil.copy(str(FIX / "codex-sessions" / "rollout-test-codex-session-01.jsonl"),
                str(codex_sessions / "rollout-test-codex-session-01.jsonl"))
    return tmp


def _run_at(host: str, base: Path, *args: str):
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(base / "claude-home")
    env["CODEX_HOME"] = str(base / "codex-home")
    return subprocess.run([sys.executable, str(SCRIPT), "--host", host, *args],
                          capture_output=True, text=True, timeout=120, env=env)


def _validate_items(items: list) -> None:
    assert items, "expected at least one import item"
    for item in items:
        assert "schema_version" not in item and "contract_version" not in item
        env = dict(item)
        env["schema_version"] = 3
        env["contract_version"] = 1
        env["event_id"] = str(uuid.uuid4())
        c.validate_envelope(env)


def test_script_claude_fixture_validates(fixture_homes: Path) -> None:
    proc = _run_at("claude", fixture_homes, "--session", "test-claude-session-01",
                   "--origin", "catchup")
    assert proc.returncode == 0, proc.stderr
    batches = [json.loads(line) for line in proc.stdout.splitlines()]
    items = [item for batch in batches for item in batch["items"]]
    assert len(items) == 2
    first = items[0]
    assert first["kind"] == "capture_turn"
    assert first["session_id"] == "test-claude-session-01"
    assert first["capture_origin"] == "catchup"
    assert first["offset"] == {"start": 0, "end": 3}
    roles = [m["role"] for m in first["payload"]["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assistant = first["payload"]["messages"][1]
    assert assistant["tool_calls"][0]["id"] == "toolu_01abc"
    tool_msg = first["payload"]["messages"][2]
    assert tool_msg["tool_call_id"] == "toolu_01abc"
    # Secrets redacted: prompt api_key, tool arg token, sk_ value in reply.
    assert "[REDACTED]" in first["payload"]["messages"][0]["content"]
    assert "abc123def456" not in json.dumps(first)
    assert "sk_live_1234567890abcdef" not in json.dumps(first)
    assert "should-be-redacted" not in json.dumps(first)
    _validate_items(items)


def test_script_codex_fixture_validates(fixture_homes: Path) -> None:
    proc = _run_at("codex", fixture_homes, "--session", "test-codex-session-01",
                   "--origin", "history_replay")
    assert proc.returncode == 0, proc.stderr
    batches = [json.loads(line) for line in proc.stdout.splitlines()]
    items = [item for batch in batches for item in batch["items"]]
    assert len(items) == 1
    item = items[0]
    assert item["capture_origin"] == "history_replay"
    assert "hunter2" not in json.dumps(item)
    _validate_items(items)


def test_script_after_index_and_deterministic_ids(fixture_homes: Path) -> None:
    full = _run_at("claude", fixture_homes, "--session", "test-claude-session-01",
                   "--origin", "catchup")
    again = _run_at("claude", fixture_homes, "--session", "test-claude-session-01",
                    "--origin", "catchup")
    assert full.stdout == again.stdout, "script output must be deterministic"
    tail = _run_at("claude", fixture_homes, "--session", "test-claude-session-01",
                   "--after-index", "3", "--origin", "catchup")
    assert tail.returncode == 0, tail.stderr
    items = [item for batch in (json.loads(line) for line in tail.stdout.splitlines())
             for item in batch["items"]]
    assert len(items) == 1
    assert items[0]["payload"]["turn_id"] == "t00001"
    assert items[0]["offset"] == {"start": 3, "end": 5}


def test_script_batch_limits(fixture_homes: Path) -> None:
    for host, session in (("claude", "test-claude-session-01"),
                          ("codex", "test-codex-session-01")):
        proc = _run_at(host, fixture_homes, "--session", session, "--origin", "catchup")
        assert proc.returncode == 0, proc.stderr
        for line in proc.stdout.splitlines():
            batch = json.loads(line)
            assert len(batch["items"]) <= 64
            assert len(c.canonical_bytes(batch)) <= 240 * 1024


def test_script_list(fixture_homes: Path) -> None:
    proc = _run_at("claude", fixture_homes, "--list")
    assert proc.returncode == 0, proc.stderr
    rows = [json.loads(line) for line in proc.stdout.splitlines()]
    assert {row["session_id"] for row in rows} == {"test-claude-session-01"}


def test_redaction_fixture_passes() -> None:
    module = _load_sync()
    fixture = json.loads((REPO / "contract" / "redaction-fixtures.json").read_text())
    for case in fixture["text"]:
        assert module._redact_text(case["in"]) == case["out"], case["in"]
    for case in fixture["objects"]:
        assert module._safe_value(case["in"]) == case["out"], case["in"]
