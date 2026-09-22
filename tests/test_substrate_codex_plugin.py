"""Tests for the substrate-codex plugin package (ChatGPT Work / Codex app / Codex CLI)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUG = REPO / "plugins" / "substrate-codex"
CLAUDE_SYNC = REPO / "plugins" / "substrate-claude" / "scripts" / "substrate_sync.py"
CODEX_SYNC = PLUG / "scripts" / "substrate_sync.py"
VALIDATE = Path.home() / ".codex" / "skills" / ".system" / "plugin-creator" / "scripts" / "validate_plugin.py"

SERVER = "substrate-memory"
MCP_URL = "https://app.trysubstrate.co/mcp"


def _load(name):
    return json.loads((PLUG / name).read_text(encoding="utf-8"))


def test_portable_manifest_shape():
    m = _load("plugin.json")
    assert m["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert m["name"] == "substrate-codex"
    assert m["version"]
    for key in ("description", "author", "homepage", "repository", "license", "keywords"):
        assert m[key], key
    ext = m["extensions"]["com.openai"]
    assert ext["hooks"] == "./hooks/hooks.json"
    iface = ext["interface"]
    for key in ("displayName", "shortDescription", "longDescription", "developerName", "category", "capabilities", "websiteURL", "defaultPrompt"):
        assert iface[key], key
    assert isinstance(iface["defaultPrompt"], list) and 1 <= len(iface["defaultPrompt"]) <= 3


def test_compat_manifest_agrees_and_passes_validator_shape():
    c = json.loads((PLUG / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    p = _load("plugin.json")
    for key in ("name", "version", "description", "author", "homepage", "repository", "license", "keywords"):
        assert c[key] == p[key], key
    assert c["skills"] == "./skills/"
    assert c["mcpServers"] == "./.mcp.json"
    # The bundled validator rejects a `hooks` field, so compat relies on
    # default hooks/hooks.json discovery; portable manifest carries hooks.
    assert "hooks" not in c
    assert c["interface"]["displayName"] == p["extensions"]["com.openai"]["interface"]["displayName"]
    assert c["interface"]["defaultPrompt"]


def test_mcp_configs_agree():
    mcp = _load("mcp.json")
    assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    legacy = _load(".mcp.json")
    assert legacy["mcpServers"] == mcp["mcpServers"]
    srv = mcp["mcpServers"][SERVER]
    assert srv["url"] == MCP_URL
    assert srv["type"] in ("streamable-http", "http")


def test_hooks_map_to_contract_table():
    hooks = json.loads((PLUG / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    # No SessionEnd: Codex does not support mcp_tool there; server seals idle sessions.
    assert "SessionEnd" not in hooks
    expected = {
        "UserPromptSubmit": "memory_turn_context",
        "PostToolUse": "memory_capture_tool",
        "Stop": "memory_capture_turn",
        "SubagentStop": "memory_capture_turn",
        "SessionStart": "memory_session_boundary",
        "PreCompact": "memory_session_boundary",
    }
    assert set(hooks) == set(expected)
    for event, tool in expected.items():
        for group in hooks[event]:
            for h in group["hooks"]:
                assert h["type"] == "mcp_tool", event
                assert h["server"] == SERVER, event
                assert h["tool"] == tool, event
                assert h.get("timeout", 600) <= 5, event
    turn = hooks["UserPromptSubmit"][0]["hooks"][0]["input"]
    assert turn["platform"] == "codex" and turn["turn_id"] == "${turn_id}" and turn["prompt"] == "${prompt}"
    tool_use = hooks["PostToolUse"][0]["hooks"][0]["input"]
    for key in ("session_id", "tool_use_id", "tool_name", "tool_input", "tool_response", "platform", "turn_id"):
        assert key in tool_use, key
    stop = hooks["Stop"][0]["hooks"][0]["input"]
    assert stop["assistant_message"] == "${last_assistant_message}" and stop["agent_context"] == "main"
    sub = hooks["SubagentStop"][0]["hooks"][0]["input"]
    assert sub["agent_context"] == "subagent" and sub["agent_id"] == "${agent_id}"
    assert sub["parent_session_id"] == "${session_id}"
    start = hooks["SessionStart"][0]
    assert start["matcher"] == "startup|resume|clear|compact"
    assert start["hooks"][0]["input"]["boundary"] == "${source}"
    assert hooks["PreCompact"][0]["hooks"][0]["input"]["boundary"] == "compact"


def test_marketplace_entry():
    mp = json.loads((REPO / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
    assert mp["name"] == "substrate-marketplace"
    assert mp["interface"]["displayName"] == "Substrate Memory"
    entry = next(e for e in mp["plugins"] if e["name"] == "substrate-codex")
    assert entry["source"] == {"source": "local", "path": "./plugins/substrate-codex"}
    assert entry["policy"] == {"installation": "AVAILABLE", "authentication": "ON_USE"}
    assert entry["category"] == "Productivity"


def test_sync_script_byte_identical_to_claude():
    assert CLAUDE_SYNC.is_file(), "claude worker script missing"
    a = hashlib.sha256(CLAUDE_SYNC.read_bytes()).hexdigest()
    b = hashlib.sha256(CODEX_SYNC.read_bytes()).hexdigest()
    assert a == b, "scripts/substrate_sync.py must be byte-identical to the claude copy"


def test_skills_have_frontmatter():
    for skill in ("substrate-memory", "substrate-connect", "substrate-sync", "substrate-import"):
        text = (PLUG / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n"), skill
        head = text[4:text.index("\n---", 4)]
        assert f"name: {skill}" in head, skill
        assert "description:" in head, skill


def test_bundled_validator_passes():
    assert VALIDATE.is_file(), "plugin-creator validator not found"
    r = subprocess.run(["/usr/bin/python3", str(VALIDATE), str(PLUG)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
