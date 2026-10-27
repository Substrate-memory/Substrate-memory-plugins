"""Codex sign-in that survives the end of the turn, and the SessionStart announcement.

`--signin-detach` starts `codex mcp login substrate-memory` in its own session and
prints the approval link it writes; a live login is reused. `--session-start`
says the connection works once, or re-shows a pending link. No real login or
Codex process here: subprocess and the sign-in check are stubbed.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CODEX_SCRIPT = REPO / "plugins" / "substrate-codex" / "scripts" / "substrate_sync.py"
HOOKS = REPO / "plugins" / "substrate-codex" / "hooks" / "hooks.json"
LINK = "https://substrate.example.test/oauth/mcp/authorize?response_type=code&state=abc"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("substrate_sync_detach_%d" % id(path), str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sync = _load(CODEX_SCRIPT)


class _FakePopen:
    """Writes the approval link into the log the way codex mcp login does."""

    last = None

    def __init__(
        self, args, stdin=None, stdout=None, stderr=None, start_new_session=False, close_fds=True
    ):
        assert start_new_session is True, "login must outlive the turn: own session"
        assert args[-3:] == ["mcp", "login", "substrate-memory"]
        stdout.write(
            "Authorize `substrate-memory` by opening this URL in your browser:\n%s\n" % LINK
        )
        stdout.flush()
        self.pid = 424242
        _FakePopen.last = self


def _state(data_dir):
    return json.loads((Path(data_dir) / sync.CODEX_LOGIN_FILE).read_text())


def test_detach_reports_connected_without_starting_a_login(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sync, "_codex_mcp_signed_in", lambda: True)
    monkeypatch.setattr(
        sync.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no"))
    )
    assert sync._cmd_signin_detach("codex", str(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "connected"}


def test_detach_starts_a_new_login_in_its_own_session(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sync, "_codex_mcp_signed_in", lambda: False)
    monkeypatch.setattr(sync, "_sleep", lambda s: None)
    monkeypatch.setattr(sync.subprocess, "Popen", _FakePopen)
    assert sync._cmd_signin_detach("codex", str(tmp_path)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "started" and out["link"] == LINK
    assert _state(tmp_path)["pid"] == 424242


def test_detach_reuses_a_live_login(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sync, "_codex_mcp_signed_in", lambda: False)
    monkeypatch.setattr(sync, "_codex_login_alive", lambda state: True)
    (tmp_path / sync.CODEX_LOGIN_LOG).write_text("open %s now\n" % LINK)
    monkeypatch.setattr(
        sync.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no second login")),
    )
    assert sync._cmd_signin_detach("codex", str(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "pending", "link": LINK}


def test_detach_reports_error_when_no_link_appears(monkeypatch, tmp_path, capsys):
    class Silent(_FakePopen):
        def __init__(
            self,
            args,
            stdin=None,
            stdout=None,
            stderr=None,
            start_new_session=False,
            close_fds=True,
        ):
            self.pid = 1

    monkeypatch.setattr(sync, "_codex_mcp_signed_in", lambda: False)
    monkeypatch.setattr(sync, "_sleep", lambda s: None)
    monkeypatch.setattr(sync, "CODEX_LOGIN_WAIT", 0.0)
    monkeypatch.setattr(sync.subprocess, "Popen", Silent)
    assert sync._cmd_signin_detach("codex", str(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_session_start_announces_a_working_connection_once(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sync, "_codex_mcp_signed_in", lambda: True)
    assert sync._cmd_session_start("codex", str(tmp_path)) == 0
    out = json.loads(capsys.readouterr().out)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Connected to Substrate." in context and "--host codex --preview" in context
    assert "Import all" in context and "Do not wait for another message" in context
    # second session: already announced, so nothing is injected
    assert sync._cmd_session_start("codex", str(tmp_path)) == 0
    assert capsys.readouterr().out == ""


def test_session_start_re_shows_a_pending_link_in_one_sentence(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sync, "_codex_mcp_signed_in", lambda: False)
    monkeypatch.setattr(sync, "_codex_login_alive", lambda state: True)
    (tmp_path / sync.CODEX_LOGIN_LOG).write_text(LINK)
    assert sync._cmd_session_start("codex", str(tmp_path)) == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert LINK in context and "Approve connection" in context


def test_session_start_is_silent_when_nothing_is_pending(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sync, "_codex_mcp_signed_in", lambda: False)
    assert sync._cmd_session_start("codex", str(tmp_path)) == 0
    assert capsys.readouterr().out == ""


def test_session_start_hook_is_registered():
    hooks = json.loads(HOOKS.read_text())["hooks"]["SessionStart"]
    commands = [h["command"] for group in hooks for h in group["hooks"] if h["type"] == "command"]
    assert len(commands) == 1 and "--host codex --session-start" in commands[0]
