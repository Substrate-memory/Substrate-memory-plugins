"""The Manual capture variant (plugins/substrate-claude-manual) and the capture-mode commands.

Manual is generated from substrate-claude by scripts/build_manual_variant.py. It keeps recall and every
local hook, and ships none of the capture mcp_tool hooks.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AUTO = REPO / "plugins" / "substrate-claude"
MANUAL = REPO / "plugins" / "substrate-claude-manual"
CAPTURE_TOOLS = {"memory_capture_tool", "memory_capture_turn", "memory_session_boundary"}
MANUAL_TOOL = "mcp__plugin_substrate-claude-manual_substrate-memory__"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_manual_variant", str(REPO / "scripts" / "build_manual_variant.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hooks(plugin: Path) -> dict:
    return json.loads((plugin / "hooks" / "hooks.json").read_text())["hooks"]


def _all_hooks(entries: dict):
    for event, groups in entries.items():
        for group in groups:
            for hook in group["hooks"]:
                yield event, group, hook


def test_generated_variant_is_current() -> None:
    assert _load_builder().stale() == [], "run: python3 scripts/build_manual_variant.py"


def test_manual_has_no_capture_hooks_and_keeps_recall() -> None:
    entries = _hooks(MANUAL)
    tools = {hook.get("tool") for _event, _group, hook in _all_hooks(entries) if hook["type"] == "mcp_tool"}
    assert tools == {"memory_turn_context"}
    assert "PreCompact" not in entries
    (recall,) = [hook for _e, _g, hook in _all_hooks(entries) if hook.get("tool") == "memory_turn_context"]
    assert recall["server"] == "plugin:substrate-claude-manual:substrate-memory"
    # capture_mode "manual": the server opens no pending turn, so the prompt is never sealed into memory.
    assert recall["input"] == {"session_id": "${session_id}", "prompt": "${prompt}", "platform": "claude",
                               "capture_mode": "manual"}


def test_manual_keeps_every_local_hook() -> None:
    def local(plugin: Path):
        return sorted((event, group.get("matcher", "").replace("substrate-claude-manual_", "substrate-claude_"),
                       hook["command"]) for event, group, hook in _all_hooks(_hooks(plugin)) if hook["type"] == "command")
    assert local(MANUAL) == local(AUTO)
    (pre,) = _hooks(MANUAL)["PreToolUse"]
    assert pre["matcher"] == MANUAL_TOOL + ".*"
    assert pre["hooks"][0]["onFailure"] == "block"


def test_automatic_still_captures() -> None:
    tools = {hook.get("tool") for _e, _g, hook in _all_hooks(_hooks(AUTO))}
    assert CAPTURE_TOOLS <= tools


def test_manual_scripts_are_byte_identical() -> None:
    for name in ("substrate_sync.py", "session_id_hook.py"):
        assert (MANUAL / "scripts" / name).read_bytes() == (AUTO / "scripts" / name).read_bytes(), name


def test_manual_manifest_and_marketplace() -> None:
    manifest = json.loads((MANUAL / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "substrate-claude-manual"
    assert manifest["version"] == (REPO / "VERSION").read_text().strip()
    marketplace = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    entries = {entry["name"]: entry for entry in marketplace["plugins"]}
    assert entries["substrate-claude-manual"]["source"] == "./plugins/substrate-claude-manual"
    assert "substrate-claude" in entries


def test_manual_text_names_only_its_own_plugin() -> None:
    for path in [*MANUAL.joinpath("commands").rglob("*.md"), *MANUAL.joinpath("skills").rglob("*.md"),
                 MANUAL / "hooks" / "hooks.json"]:
        text = path.read_text()
        # Tool prefixes, skill/command namespaces and the server name all follow the plugin name.
        assert "substrate-claude_" not in text and "substrate-claude:" not in text, path


def _sync(plugin: Path, tmp_path: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    environment = dict(os.environ, CLAUDE_CONFIG_DIR=str(tmp_path / "claude-home"),
                       SUBSTRATE_DATA_DIR=str(tmp_path / "data"), SUBSTRATE_MCP_URL="http://127.0.0.1:9/mcp")
    return subprocess.run([sys.executable, str(plugin / "scripts" / "substrate_sync.py"), "--host", "claude", *args],
                          input=stdin, capture_output=True, text=True, timeout=60, env=environment)


def _installed(tmp_path: Path, *names: str) -> None:
    path = tmp_path / "claude-home" / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "plugins": {n + "@substrate-marketplace": [{}] for n in names}}))


@pytest.mark.parametrize("plugin, running", [(AUTO, "automatic"), (MANUAL, "manual")])
def test_mode_check_knows_which_variant_runs(tmp_path: Path, plugin: Path, running: str) -> None:
    _installed(tmp_path, plugin.name)
    report = json.loads(_sync(plugin, tmp_path, "--mode-check").stdout)
    assert report == {"mode": None, "running": running, "plugin": plugin.name,
                      "installed": [plugin.name], "switch": None}


def test_record_manual_from_automatic_prints_the_switch(tmp_path: Path) -> None:
    _installed(tmp_path, "substrate-claude")
    report = json.loads(_sync(AUTO, tmp_path, "--record-mode", "manual").stdout)
    assert report["mode"] == "manual"
    assert report["switch"] == ["claude plugin install substrate-claude-manual@substrate-marketplace",
                                "claude plugin uninstall substrate-claude@substrate-marketplace"]
    assert report["restart"] is True
    # Recorded in the shared data dir: the Manual copy reads the same mode, and after the switch has nothing to do.
    _installed(tmp_path, "substrate-claude-manual")
    after = json.loads(_sync(MANUAL, tmp_path, "--mode-check").stdout)
    assert after["mode"] == "manual" and after["switch"] is None


def test_record_mode_matching_the_variant_needs_no_switch(tmp_path: Path) -> None:
    report = json.loads(_sync(MANUAL, tmp_path, "--record-mode", "manual").stdout)
    assert report["switch"] is None
    state = json.loads((tmp_path / "data" / "import-offer.json").read_text())
    assert state["mode"] == "manual"


def test_both_variants_installed_warns_once(tmp_path: Path) -> None:
    _installed(tmp_path, "substrate-claude", "substrate-claude-manual")
    payload = json.dumps({"session_id": "s1", "prompt": "hello"})
    first = _sync(MANUAL, tmp_path, "--offer-check", stdin=payload)
    assert first.returncode == 0
    context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Both Substrate plugins are installed" in context
    second = _sync(MANUAL, tmp_path, "--offer-check", stdin=payload)
    assert "Both Substrate plugins" not in second.stdout
    report = json.loads(_sync(MANUAL, tmp_path, "--mode-check").stdout)
    assert report["switch"] == ["claude plugin uninstall substrate-claude@substrate-marketplace"]


def test_manual_setup_text_names_the_manual_skills(tmp_path: Path) -> None:
    done = _sync(MANUAL, tmp_path, "--offer-check", stdin=json.dumps({"session_id": "s1", "prompt": "hi"}))
    context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "`substrate-claude-manual:substrate-connect`" in context
    assert MANUAL_TOOL + "memory_search" in context
    assert "plugin:substrate-claude:" not in context


def test_manual_permissions_use_the_manual_prefix(tmp_path: Path) -> None:
    done = _sync(MANUAL, tmp_path, "--permissions-allow")
    assert json.loads(done.stdout)["ok"] is True
    rules = json.loads((tmp_path / "claude-home" / "settings.json").read_text())["permissions"]["allow"]
    assert rules and all(rule.startswith(MANUAL_TOOL) for rule in rules)


def test_mode_commands_are_claude_only(tmp_path: Path) -> None:
    environment = dict(os.environ, SUBSTRATE_DATA_DIR=str(tmp_path / "data"))
    done = subprocess.run([sys.executable, str(AUTO / "scripts" / "substrate_sync.py"), "--host", "codex", "--mode-check"],
                          capture_output=True, text=True, timeout=60, env=environment)
    assert done.returncode == 1


def test_manual_session_hook_sets_the_session(tmp_path: Path) -> None:
    payload = {"session_id": "sess-1", "tool_name": MANUAL_TOOL + "memory_search", "tool_input": {"query": "q"}}
    done = subprocess.run([sys.executable, str(MANUAL / "scripts" / "session_id_hook.py")], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0
    assert json.loads(done.stdout)["hookSpecificOutput"]["updatedInput"]["session_id"] == "sess-1"


def test_connect_flow_asks_for_the_mode() -> None:
    for plugin in (AUTO, MANUAL):
        for path in (plugin / "commands" / "substrate-connect.md", plugin / "skills" / "substrate-connect" / "SKILL.md"):
            text = path.read_text()
            assert "SYNC --mode-check" in text and "--record-mode" in text, path
