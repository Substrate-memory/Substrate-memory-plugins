"""Session-start update line, Claude Code permission helper, and the stable Codex hook entry point.

Nothing here touches the network or a real ~/.claude or ~/.codex: every path is a temp dir.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUG_CLAUDE = REPO / "plugins" / "substrate-claude"
PLUG_CODEX = REPO / "plugins" / "substrate-codex"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sync = _load("substrate_sync_update_perm", PLUG_CLAUDE / "scripts" / "substrate_sync.py")
U = _load("codex_user_hooks_shim", PLUG_CODEX / "scripts" / "codex_user_hooks.py")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated CLAUDE_CONFIG_DIR, CODEX_HOME and data dir; the update check is on, the network off."""
    claude = tmp_path / "claude"
    claude.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.delenv("SUBSTRATE_UPDATE_CHECK", raising=False)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(sync, "_fetch_latest_version", lambda: None)
    return {"claude": claude, "data": data, "tmp": tmp_path}


# ---------------------------------------------------------------------------
# Update line (SessionStart, at most once a day)
# ---------------------------------------------------------------------------

def test_no_line_when_the_fetch_fails(home) -> None:
    assert sync._update_notice("claude", str(home["data"])) == ""


def test_newer_version_gives_one_line_once_a_day(monkeypatch, home) -> None:
    monkeypatch.setattr(sync, "_fetch_latest_version", lambda: "99.0.0")
    line = sync._update_notice("claude", str(home["data"]))
    assert "99.0.0" in line and "/plugin marketplace update substrate-marketplace" in line
    assert sync._update_notice("claude", str(home["data"])) == ""  # shown already today


def test_cache_within_a_day_means_no_second_request(monkeypatch, home) -> None:
    calls = []
    monkeypatch.setattr(sync, "_fetch_latest_version", lambda: calls.append(1) or "99.0.0")
    sync._update_notice("claude", str(home["data"]))
    sync._update_notice("claude", str(home["data"]))
    assert len(calls) == 1


def test_older_or_equal_version_is_silent(monkeypatch, home) -> None:
    monkeypatch.setattr(sync, "_fetch_latest_version", lambda: sync.PLUGIN_VERSION)
    assert sync._update_notice("claude", str(home["data"])) == ""
    monkeypatch.setattr(sync, "_fetch_latest_version", lambda: "0.0.1")
    assert sync._update_notice("claude", str(home["data"])) == ""


def test_opt_out_never_fetches(monkeypatch, home) -> None:
    monkeypatch.setenv("SUBSTRATE_UPDATE_CHECK", "0")
    monkeypatch.setattr(sync, "_fetch_latest_version", lambda: pytest.fail("network used"))
    assert sync._update_notice("claude", str(home["data"])) == ""


def test_invalid_response_is_ignored(monkeypatch) -> None:
    class _Bad(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sync.urllib.request, "urlopen", lambda *a, **k: _Bad(b"<html>not a version</html>"))
    assert sync._fetch_latest_version() is None


def test_unreachable_server_fails_fast_and_silently(monkeypatch) -> None:
    monkeypatch.setattr(sync, "UPDATE_URL", "http://127.0.0.1:9/VERSION")
    assert sync._fetch_latest_version() is None


def test_claude_hook_prints_a_system_line_only(monkeypatch, home, capsys) -> None:
    monkeypatch.setattr(sync, "_fetch_latest_version", lambda: "99.0.0")
    assert sync._cmd_update_check("claude", str(home["data"])) == 0
    assert "systemMessage" in json.loads(capsys.readouterr().out)
    assert sync._cmd_update_check("codex", str(home["data"])) == 0
    assert capsys.readouterr().out == ""


def test_codex_session_start_adds_the_line_to_its_context(monkeypatch, home, capsys) -> None:
    monkeypatch.setattr(sync, "_codex_mcp_signed_in", lambda: False)
    monkeypatch.setattr(sync, "_fetch_latest_version", lambda: "99.0.0")
    sync._cmd_session_start("codex", str(home["data"]))
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "Substrate 99.0.0 is available" in context and "Say it once" in context


def test_session_start_hook_for_claude_is_a_command_only() -> None:
    hooks = json.loads((PLUG_CLAUDE / "hooks" / "hooks.json").read_text())["hooks"]
    handlers = [h for g in hooks["SessionStart"] for h in g["hooks"]]
    assert [h["type"] for h in handlers] == ["command"]
    assert "--update-check" in handlers[0]["command"] and handlers[0]["timeout"] <= 5


# ---------------------------------------------------------------------------
# Claude Code permissions: every memory tool except memory_forget
# ---------------------------------------------------------------------------

def _run_perm(home, *args: str) -> dict:
    import os

    os.environ["CLAUDE_CONFIG_DIR"] = str(home["claude"])
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = sync._cmd_permissions("claude", str(home["data"]), allow="--permissions-allow" in args,
                                     decline="--decline" in args)
    finally:
        sys.stdout = old
    out = json.loads(buf.getvalue() or "{}")
    out["_code"] = code
    return out


def _settings(home) -> dict:
    return json.loads((home["claude"] / "settings.json").read_text())


def test_memory_tools_exclude_forget_and_are_listed_one_by_one() -> None:
    assert "memory_forget" not in sync.MEMORY_TOOLS
    assert len(sync.MEMORY_TOOLS) == len(set(sync.MEMORY_TOOLS)) == 12
    assert sync.CLAUDE_TOOL_PREFIX == "mcp__plugin_substrate-claude_substrate-memory__"


def test_check_reports_missing_rules_and_writes_nothing(home) -> None:
    out = _run_perm(home)
    assert out["missing"] == 12 and out["allowed"] == 0 and out["offered"] is False
    assert not (home["claude"] / "settings.json").exists()


def test_allow_adds_exactly_the_rules_and_keeps_other_settings(home) -> None:
    (home["claude"] / "settings.json").write_text(json.dumps({
        "theme": "dark", "permissions": {"allow": ["Bash(git status)"], "deny": ["Read(./.env)"]},
    }))
    out = _run_perm(home, "--permissions-allow")
    assert out["_code"] == 0 and out["changed"] is True and out["added"] == 12
    doc = _settings(home)
    assert doc["theme"] == "dark" and doc["permissions"]["deny"] == ["Read(./.env)"]
    allow = doc["permissions"]["allow"]
    assert allow[0] == "Bash(git status)"
    assert allow[1:] == [sync.CLAUDE_TOOL_PREFIX + name for name in sync.MEMORY_TOOLS]
    assert not any(rule.endswith("memory_forget") for rule in allow)
    assert (home["claude"] / "settings.json.substrate-backup").exists()


def test_allow_is_idempotent(home) -> None:
    _run_perm(home, "--permissions-allow")
    again = _run_perm(home, "--permissions-allow")
    assert again["_code"] == 0 and again["changed"] is False
    assert _run_perm(home)["missing"] == 0


def test_invalid_settings_are_left_untouched(home) -> None:
    path = home["claude"] / "settings.json"
    path.write_text("{not json")
    out = _run_perm(home, "--permissions-allow")
    assert out["_code"] == 1 and out["ok"] is False
    assert path.read_text() == "{not json"


def test_decline_records_the_answer_and_writes_no_rules(home) -> None:
    out = _run_perm(home, "--permissions-allow", "--decline")
    assert out["declined"] is True and not (home["claude"] / "settings.json").exists()
    assert _run_perm(home)["offered"] is True


def test_cowork_host_is_refused_by_the_helper(home) -> None:
    out = sync._cmd_permissions("codex", str(home["data"]), allow=True, decline=False)
    assert out == 1


# ---------------------------------------------------------------------------
# Codex stable hook entry point: a plugin update does not change the hook command
# ---------------------------------------------------------------------------

def _fake_plugin(root: Path, version: str) -> Path:
    """A copy of the Codex plugin's hooks and scripts at one version."""
    for rel in ("hooks", "scripts", ".codex-plugin"):
        shutil.copytree(PLUG_CODEX / rel, root / rel, dirs_exist_ok=True)
    manifest = root / ".codex-plugin" / "plugin.json"
    doc = json.loads(manifest.read_text())
    doc["version"] = version
    manifest.write_text(json.dumps(doc))
    return root


def test_hooks_file_is_byte_identical_across_plugin_versions(tmp_path, monkeypatch) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    v1 = _fake_plugin(tmp_path / "plugin-0.10.3", "0.10.3")
    v2 = _fake_plugin(tmp_path / "plugin-0.10.4", "0.10.4")
    U.install(home, v1, check_plugin=False)
    first = (home / "hooks.json").read_bytes()
    U.install(home, v2, check_plugin=False)
    assert (home / "hooks.json").read_bytes() == first
    assert str(v1) not in first.decode() and str(v2) not in first.decode()
    assert "substrate/substrate_hook.py" in first.decode()


def test_install_twice_writes_nothing_and_remove_takes_the_shim(tmp_path, monkeypatch) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    root = _fake_plugin(tmp_path / "plugin", "0.10.3")
    assert U.install(home, root, check_plugin=False)["changed"] is True
    assert U.install(home, root, check_plugin=False)["changed"] is False
    assert (home / "substrate" / "substrate_hook.py").is_file()
    assert U.remove(home)["changed"] is True
    assert not (home / "substrate").exists()


def test_shim_runs_the_newest_cached_version_with_the_same_arguments(tmp_path, monkeypatch) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    shim_dir = home / "substrate"
    shim_dir.mkdir()
    (shim_dir / "substrate_hook.py").write_text(U.SHIM_SOURCE)
    for version in ("0.10.3", "0.10.10"):
        root = home / "plugins" / "cache" / "substrate-marketplace" / "substrate-codex" / version
        (root / "scripts").mkdir(parents=True)
        (root / ".codex-plugin").mkdir()
        (root / ".codex-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
        (root / "scripts" / "substrate_sync.py").write_text(
            "import os, sys\n"
            "version = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
            "print('RAN', version, sys.argv[1:])\n")
    out = subprocess.run([sys.executable, str(shim_dir / "substrate_hook.py"), "--host", "codex", "--offer-check"],
                         capture_output=True, text=True, cwd=str(tmp_path), timeout=30)
    # The newest version wins by number (0.10.10 over 0.10.3), and the arguments pass through.
    assert out.returncode == 0 and out.stdout.strip() == "RAN 0.10.10 ['--host', 'codex', '--offer-check']"


def test_shim_exits_silently_when_no_plugin_is_installed(tmp_path, monkeypatch) -> None:
    home = tmp_path / "codex-home"
    (home / "substrate").mkdir(parents=True)
    (home / "substrate" / "substrate_hook.py").write_text(U.SHIM_SOURCE)
    out = subprocess.run([sys.executable, str(home / "substrate" / "substrate_hook.py"), "--host", "codex"],
                         capture_output=True, text=True, cwd=str(tmp_path), timeout=30)
    assert out.returncode == 0 and out.stdout == ""
