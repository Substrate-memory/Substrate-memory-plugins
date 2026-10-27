"""Tests for the Codex user hook installer (CDX-04): plugins/substrate-codex/scripts/codex_user_hooks.py.

Every test uses its own temporary CODEX_HOME; nothing touches ~/.codex.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUG = REPO / "plugins" / "substrate-codex"
MODULE_PATH = PLUG / "scripts" / "codex_user_hooks.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("codex_user_hooks", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


U = _load_module()


@pytest.fixture(autouse=True)
def _no_codex_spawn(monkeypatch):
    """Unit tests never start the codex binary; the live check has its own test."""
    monkeypatch.setattr(U, "_plugin_hooks_active", lambda timeout=20.0: None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return codex_home


def _hooks_file(home: Path) -> Path:
    return home / "hooks.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _foreign_doc() -> dict:
    return {
        "model": "keep-me",
        "hooks": {
            "SessionStart": [
                {"matcher": "startup", "hooks": [
                    {"type": "command", "command": "echo mine", "timeout": 9}]},
            ],
            "Stop": [
                {"hooks": [{"type": "command", "command": "notify-send done", "timeout": 3}]},
            ],
        },
    }


def _foreign_handlers(doc: dict) -> list:
    out = []
    for event, groups in doc.get("hooks", {}).items():
        for group in groups:
            for handler in group.get("hooks", []):
                if not U._is_ours(handler):
                    out.append((event, group.get("matcher"), handler))
    return out


# --------------------------------------------------------------------------

def test_desired_table_mirrors_plugin_hooks_with_tag_and_absolute_script():
    desired = U.desired_hooks(PLUG)
    plugin = json.loads((PLUG / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(desired) == set(plugin) == {
        "UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop", "SessionStart", "PreCompact",
    }
    for event in plugin:
        assert len(desired[event]) == len(plugin[event])
        for dg, pg in zip(desired[event], plugin[event]):
            assert dg.get("matcher") == pg.get("matcher")
            for dh, ph in zip(dg["hooks"], pg["hooks"]):
                assert dh.pop(U.TAG_KEY) == U.TAG_VALUE
                if ph["type"] == "command":
                    assert "${" not in dh["command"]
                    assert str(PLUG / "scripts" / "substrate_sync.py") in dh["command"]
                    assert ("--session-start" if event == "SessionStart" else "--offer-check") in dh["command"]
                    assert dh["timeout"] == ph["timeout"]
                    dh.pop("command")
                    ph = {k: v for k, v in ph.items() if k != "command"}
                    dh = {k: v for k, v in dh.items() if k != "command"}
                assert dh == ph


def test_install_writes_tagged_entries_for_every_plugin_event(home):
    result = U.install(home, PLUG, check_plugin=False)
    assert result["changed"] is True
    assert result["events"] == {
        "UserPromptSubmit": 2, "PostToolUse": 1, "Stop": 1,
        "SubagentStop": 1, "SessionStart": 2, "PreCompact": 1,
    }
    doc = _read(_hooks_file(home))
    for groups in doc["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                assert handler[U.TAG_KEY] == U.TAG_VALUE
    assert "CLAUDE_PLUGIN_ROOT" not in _hooks_file(home).read_text(encoding="utf-8")
    assert U.BACKUP_FILE not in os.listdir(home)  # nothing to back up on a fresh home


def test_install_is_idempotent_and_writes_nothing_second_time(home):
    U.install(home, PLUG, check_plugin=False)
    first = _hooks_file(home).read_bytes()
    again = U.install(home, PLUG, check_plugin=False)
    assert again["changed"] is False
    assert _hooks_file(home).read_bytes() == first


def test_install_preserves_foreign_entries_and_their_order(home):
    original = _foreign_doc()
    _hooks_file(home).write_text(json.dumps(original, indent=2), encoding="utf-8")
    U.install(home, PLUG, check_plugin=False)
    doc = _read(_hooks_file(home))
    assert doc["model"] == "keep-me"
    # Our group is first in SessionStart; the user's own group follows, untouched.
    session = doc["hooks"]["SessionStart"]
    assert U._is_ours(session[0]["hooks"][0])
    assert session[-1] == original["hooks"]["SessionStart"][0]
    assert doc["hooks"]["Stop"][-1] == original["hooks"]["Stop"][0]
    assert _foreign_handlers(doc) == _foreign_handlers(original)


def test_install_twice_on_existing_file_is_byte_stable(home):
    _hooks_file(home).write_text(json.dumps(_foreign_doc(), indent=2), encoding="utf-8")
    U.install(home, PLUG, check_plugin=False)
    first = _hooks_file(home).read_bytes()
    U.install(home, PLUG, check_plugin=False)
    assert _hooks_file(home).read_bytes() == first


def test_backup_is_one_time_and_holds_the_original_bytes(home):
    original = json.dumps(_foreign_doc(), indent=2).encode("utf-8")
    _hooks_file(home).write_bytes(original)
    U.install(home, PLUG, check_plugin=False)
    backup = home / U.BACKUP_FILE
    assert backup.read_bytes() == original
    # The user edits the file; a later install must not overwrite the backup.
    doc = _read(_hooks_file(home))
    doc["hooks"]["PostToolUse"] = [{"hooks": [{"type": "command", "command": "later", "timeout": 1}]}]
    _hooks_file(home).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    U.install(home, PLUG, check_plugin=False)
    assert backup.read_bytes() == original


def test_remove_deletes_exactly_our_entries(home):
    original = _foreign_doc()
    _hooks_file(home).write_text(json.dumps(original, indent=2), encoding="utf-8")
    U.install(home, PLUG, check_plugin=False)
    result = U.remove(home)
    assert result["changed"] is True and result["removed_entries"] == 8
    doc = _read(_hooks_file(home))
    assert doc == original
    again = U.remove(home)
    assert again["changed"] is False


def test_remove_deletes_file_that_install_created(home):
    U.install(home, PLUG, check_plugin=False)
    U.remove(home)
    assert not _hooks_file(home).exists()
    assert not (home / U.BACKUP_FILE).exists()


def test_remove_keeps_user_file_with_only_empty_hooks_when_it_existed(home):
    _hooks_file(home).write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    U.install(home, PLUG, check_plugin=False)
    U.remove(home)
    assert _read(_hooks_file(home)) == {"hooks": {}}


def test_malformed_file_is_reported_and_left_untouched(home):
    bad = b'{"hooks": {"Stop": [ oops'
    _hooks_file(home).write_bytes(bad)
    with pytest.raises(U.HookConfigError):
        U.install(home, PLUG, check_plugin=False)
    assert _hooks_file(home).read_bytes() == bad
    assert U.main(["install", "--codex-home", str(home)]) == 1
    assert _hooks_file(home).read_bytes() == bad
    assert sorted(os.listdir(home)) == ["hooks.json"]


def test_unexpected_shape_is_refused(home):
    _hooks_file(home).write_text(json.dumps({"hooks": {"Stop": {"not": "a list"}}}), encoding="utf-8")
    with pytest.raises(U.HookConfigError):
        U.install(home, PLUG, check_plugin=False)


def test_failed_replace_keeps_old_file_and_leaves_no_temp_files(home, monkeypatch):
    original = json.dumps(_foreign_doc(), indent=2).encode("utf-8")
    _hooks_file(home).write_bytes(original)

    def boom(src, dst):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(U.os, "replace", boom)
    with pytest.raises(OSError):
        U.install(home, PLUG, check_plugin=False)
    assert _hooks_file(home).read_bytes() == original
    assert not [p for p in os.listdir(home) if p.endswith(".tmp")]


def test_trust_state_is_cleared_only_for_our_keys(home):
    _hooks_file(home).write_text(json.dumps(_foreign_doc(), indent=2), encoding="utf-8")
    U.install(home, PLUG, check_plugin=False)
    # Foreign Stop group sits after ours in the file: its index is 1.
    doc = _read(_hooks_file(home))
    path = _hooks_file(home).resolve()
    our_keys = U.owned_state_keys(doc, path)
    assert len(our_keys) == 8
    foreign_key = f"{path}:stop:1:0"
    config = home / "config.toml"
    blocks = [f'[hooks.state."{k}"]\ntrusted_hash = "sha256:{i:064d}"\n' for i, k in enumerate(our_keys)]
    blocks.append(f'[hooks.state."{foreign_key}"]\ntrusted_hash = "sha256:{"f" * 64}"\n')
    config.write_text(
        '[model_providers.p]\nname = "x"\n\n' + "\n".join(blocks) + "\n[features]\nhooks = true\n",
        encoding="utf-8",
    )
    result = U.remove(home)
    assert result["trust_state_cleared"] is True
    text = config.read_text(encoding="utf-8")
    for key in our_keys:
        assert f'[hooks.state."{key}"]' not in text
    assert f'[hooks.state."{foreign_key}"]' in text
    assert "[model_providers.p]" in text and "[features]\nhooks = true" in text
    assert (home / U.CONFIG_BACKUP_FILE).exists()


def test_plugin_hooks_already_active_means_no_user_copy(home, monkeypatch):
    U.install(home, PLUG, check_plugin=False)
    assert _foreign_handlers(_read(_hooks_file(home))) == []
    monkeypatch.setattr(U, "_plugin_hooks_active", lambda timeout=20.0: True)
    result = U.install(home, PLUG, check_plugin=True)
    assert result["skipped"] == "plugin hooks already active in Codex"
    assert not _hooks_file(home).exists()  # our file held only our entries


def test_unknown_plugin_state_still_installs(home, monkeypatch):
    monkeypatch.setattr(U, "_plugin_hooks_active", lambda timeout=20.0: None)
    result = U.install(home, PLUG, check_plugin=True)
    assert result["changed"] is True and "skipped" not in result


def test_status_reports_counts_without_writing(home):
    before = U.status(home)
    assert before["present"] is False and before["events"] == {}
    U.install(home, PLUG, check_plugin=False)
    after = U.status(home)
    assert sum(after["events"].values()) == 8


def test_main_cli_round_trip(home, capsys, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert U.main(["install", "--no-plugin-check"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is True
    assert U.main(["remove"]) == 0
    assert not _hooks_file(home).exists()


def test_live_codex_reports_no_plugin_hooks_for_isolated_home(home, monkeypatch):
    """Real app-server call (skipped when codex is not installed)."""
    if not shutil.which("codex"):
        pytest.skip("codex CLI not installed")
    monkeypatch.setattr(U, "_plugin_hooks_active", _ORIGINAL_CHECK)
    monkeypatch.chdir(home)
    assert U._plugin_hooks_active(timeout=60.0) is False


_ORIGINAL_CHECK = U._plugin_hooks_active


def test_skill_and_docs_describe_the_install_and_the_one_approval():
    skill = (PLUG / "skills" / "substrate-connect" / "SKILL.md").read_text(encoding="utf-8")
    assert "codex_user_hooks.py\" install" in skill or "codex_user_hooks.py install" in skill
    assert "/hooks" in skill and "approve" in skill.lower()
    assert "Open this link and choose Approve connection" in skill and "/new" in skill
    assert "codex mcp add substrate-memory --url" in skill and "<the url in this plugin's mcp.json>" in skill
    for doc in ("INSTALL.md", "README.md"):
        text = (PLUG / doc).read_text(encoding="utf-8")
        assert "codex_user_hooks.py" in text, doc
        assert "/hooks" in text, doc
