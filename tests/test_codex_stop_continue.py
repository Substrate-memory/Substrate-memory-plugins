"""Codex Stop hook keeps the turn going while the browser sign-in is pending.

Codex 0.154 applies {"decision": "block", "reason": ...} from a Stop hook by
continuing the turn with the reason as a prompt. The hook waits (bounded) for
`codex mcp login substrate-memory` to finish, then tells the agent to check
memory and say "Connected". Fail open everywhere else. No live processes: the
process table and `codex mcp list` are stubbed.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CODEX_SCRIPT = REPO / "plugins" / "substrate-codex" / "scripts" / "substrate_sync.py"
HOOKS = REPO / "plugins" / "substrate-codex" / "hooks" / "hooks.json"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("substrate_sync_stop_%d" % id(path), str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sync = _load(CODEX_SCRIPT)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "_sleep", lambda seconds: None)
    monkeypatch.setattr(sync, "CODEX_STOP_WAIT", 0.0)  # no real waiting in unit tests
    return str(tmp_path)


def _running(sequence):
    """Process-table answers in order; the last answer repeats."""
    items = list(sequence)

    def fake():
        return items.pop(0) if len(items) > 1 else items[0]

    return fake


def _state(data_dir):
    path = Path(data_dir) / sync.CODEX_STOP_FILE
    return json.loads(path.read_text()) if path.exists() else None


def test_no_sign_in_running_means_no_output(monkeypatch, data_dir, capsys):
    monkeypatch.setattr(sync, "_codex_login_running", lambda: False)
    assert sync._cmd_stop_check("codex", data_dir) == 0
    assert capsys.readouterr().out == ""


def test_approval_during_wait_continues_turn_with_connected_steps(monkeypatch, data_dir, capsys):
    monkeypatch.setattr(sync, "_codex_login_running", _running([True, True, False]))
    monkeypatch.setattr(sync, "_codex_mcp_signed_in", lambda: True)
    assert sync._cmd_stop_check("codex", data_dir) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    reason = out["reason"]
    assert "memory_search" in reason
    assert "Connected to Substrate." in reason
    assert "--host codex --record-connected" in reason
    assert "claude" not in reason.lower()
    assert "Import all" in reason
    assert _state(data_dir) is None  # success clears the counter


def test_still_pending_blocks_once_and_counts(monkeypatch, data_dir, capsys):
    monkeypatch.setattr(sync, "_codex_login_running", lambda: True)
    monkeypatch.setattr(sync, "_codex_mcp_signed_in", lambda: False)
    assert sync._cmd_stop_check("codex", data_dir) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "still waiting for the browser approval" in out["reason"]
    assert _state(data_dir)["blocks"] == 1


def test_login_ended_without_sign_in_fails_open(monkeypatch, data_dir, capsys):
    monkeypatch.setattr(sync, "_codex_login_running", _running([True, False]))
    monkeypatch.setattr(sync, "_codex_mcp_signed_in", lambda: False)
    assert sync._cmd_stop_check("codex", data_dir) == 0
    assert capsys.readouterr().out == ""
    assert _state(data_dir) is None


def test_continuations_are_bounded(monkeypatch, data_dir, capsys):
    (Path(data_dir) / sync.CODEX_STOP_FILE).write_text(
        json.dumps({"blocks": sync.CODEX_STOP_MAX_BLOCKS, "first": __import__("time").time()})
    )
    monkeypatch.setattr(sync, "_codex_login_running", lambda: True)
    monkeypatch.setattr(sync, "_codex_mcp_signed_in", lambda: False)
    assert sync._cmd_stop_check("codex", data_dir) == 0
    assert capsys.readouterr().out == ""


def test_ten_minute_window_is_bounded(monkeypatch, data_dir, capsys):
    (Path(data_dir) / sync.CODEX_STOP_FILE).write_text(
        json.dumps(
            {"blocks": 1, "first": __import__("time").time() - sync.CODEX_STOP_MAX_SECONDS - 5}
        )
    )
    monkeypatch.setattr(sync, "_codex_login_running", lambda: True)
    monkeypatch.setattr(sync, "_codex_mcp_signed_in", lambda: True)
    assert sync._cmd_stop_check("codex", data_dir) == 0
    assert capsys.readouterr().out == ""


def test_login_process_detection_matches_only_the_login(monkeypatch):
    class Done:
        def __init__(self, text):
            self.stdout = text

    cases = {
        "node /usr/bin/codex mcp login substrate-memory\n": True,
        "/bin/bash -lc codex mcp login substrate-memory\n": True,
        "node /usr/bin/codex mcp list --json\n": False,
        "python3 substrate_sync.py --host codex --stop-check\n": False,
        "codex mcp login other-server\n": False,
    }
    for text, expected in cases.items():
        monkeypatch.setattr(sync.subprocess, "run", lambda *a, _t=text, **k: Done(_t))
        assert sync._codex_login_running() is expected, text


def test_stop_hook_is_registered_with_a_timeout_above_the_wait():
    hooks = json.loads(HOOKS.read_text())["hooks"]["Stop"]
    commands = [h for group in hooks for h in group["hooks"] if h["type"] == "command"]
    assert len(commands) == 1
    assert "--host codex --stop-check" in commands[0]["command"]
    assert commands[0]["timeout"] > sync.CODEX_STOP_WAIT
