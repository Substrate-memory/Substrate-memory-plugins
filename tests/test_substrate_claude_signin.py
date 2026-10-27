"""Claude Code sign-in continuation: PostToolUse(authenticate) and Stop hooks.

The Stop hook may hold a setup turn open while the browser sign-in is pending. These
tests pin the decision logic (pending, connected, cancelled, abandoned, bounded,
fail-open) and the exact text the model receives.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUG = REPO / "plugins" / "substrate-claude"
SCRIPT = PLUG / "scripts" / "substrate_sync.py"
CODEX_SCRIPT = REPO / "plugins" / "substrate-codex" / "scripts" / "substrate_sync.py"
AUTH_LINK = (
    "https://sync.example.test:8446/oauth/mcp/authorize?response_type=code"
    "&client_id=mcp_test&code_challenge=abc&state=xyz&scope=retrieve+capture"
)
AUTH_TOOL = "mcp__plugin_substrate-claude_substrate-memory__authenticate"


def _load():
    spec = importlib.util.spec_from_file_location("substrate_sync_signin", str(SCRIPT))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "claude-home"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.delenv("SUBSTRATE_DATA_DIR", raising=False)
    monkeypatch.setenv("ENABLE_CLAUDEAI_MCP_SERVERS", "false")
    data = config / "substrate-memory"
    module = _load()
    module.SIGNIN_STOP_WAIT = 0.0
    module._sleep = lambda seconds: None
    return module, data


def _stdin(monkeypatch: pytest.MonkeyPatch, payload: dict | str) -> None:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))


def _start(module, data, monkeypatch, session="sess-1", response=None):
    if response is None:
        response = [{"type": "text", "text": "Sign in: " + AUTH_LINK + "\nIf the page shows an error..."}]
    _stdin(monkeypatch, {"session_id": session, "tool_name": AUTH_TOOL, "tool_response": response})
    return module._cmd_signin_start("claude", str(data))


def _stop(module, data, monkeypatch, session="sess-1", capsys=None):
    _stdin(monkeypatch, {"session_id": session, "stop_hook_active": False})
    code = module._cmd_stop_check("claude", str(data))
    out = capsys.readouterr().out if capsys is not None else ""
    return code, out


def _state(data: Path) -> dict:
    return json.loads((data / "import-offer.json").read_text())["signin"]


# ---------------------------------------------------------------- PostToolUse(authenticate)


def test_signin_start_gives_exact_sentence_and_link(env, monkeypatch, capsys) -> None:
    module, data = env
    assert _start(module, data, monkeypatch) == 0
    out = json.loads(capsys.readouterr().out)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "Open this link, sign in and choose **Approve connection**. I'll continue automatically." in context
    assert AUTH_LINK in context
    assert "Omit the tool's other notes" in context
    assert "Connected to Substrate." in context and "Reconnected" in context
    state = _state(data)
    assert state["state"] == "pending" and state["session_id"] == "sess-1"
    assert state["blocks"] == 0 and state["reconnect"] is False


def test_signin_start_without_link_asks_for_run_mcp(env, monkeypatch, capsys) -> None:
    module, data = env
    _start(module, data, monkeypatch, response="Authenticating, no url here")
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "No sign-in link was found" in context
    assert "/mcp" in context and AUTH_LINK not in context


def test_signin_start_marks_reconnect_when_previously_connected(env, monkeypatch, capsys) -> None:
    module, data = env
    data.mkdir(parents=True)
    (data / "import-offer.json").write_text(json.dumps({"connected_at": "2026-10-20T00:00:00Z"}))
    _start(module, data, monkeypatch)
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "was connected before" in context
    assert _state(data)["reconnect"] is True


def test_signin_start_is_fail_open_on_garbage_stdin(env, monkeypatch, capsys) -> None:
    module, data = env
    _stdin(monkeypatch, "not json at all")
    assert module.main(["--host", "claude", "--signin-start", "--data-dir", str(data)]) == 0
    # No session means no pending state, so the Stop hook can never block on it.
    assert not (data / "import-offer.json").exists() or "signin" not in json.loads(
        (data / "import-offer.json").read_text())


# ---------------------------------------------------------------- Stop decisions


def test_normal_turns_never_block(env, monkeypatch, capsys) -> None:
    module, data = env
    code, out = _stop(module, data, monkeypatch, capsys=capsys)
    assert code == 0 and out == ""
    assert not data.exists()  # no state is written by a turn that has nothing pending


def test_pending_blocks_with_polling_reason(env, monkeypatch, capsys) -> None:
    module, data = env
    _start(module, data, monkeypatch)
    capsys.readouterr()
    code, out = _stop(module, data, monkeypatch, capsys=capsys)
    assert code == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"
    reason = decision["reason"]
    assert "continuation 1 of 8" in reason
    assert "memory_search" in reason and "--pause 15" in reason and "--record-connected" in reason
    assert "not approved yet" in reason.lower() or "Not approved yet" in reason
    assert _state(data)["blocks"] == 1


def test_pending_reports_approval_when_credentials_appear(env, monkeypatch, capsys) -> None:
    module, data = env
    _start(module, data, monkeypatch)
    capsys.readouterr()
    monkeypatch.setattr(module, "_claude_mcp_signed_in", lambda: True)
    _, out = _stop(module, data, monkeypatch, capsys=capsys)
    assert "The browser approval is in." in json.loads(out)["reason"]


def test_stop_waits_for_approval_then_blocks(env, monkeypatch, capsys) -> None:
    module, data = env
    module.SIGNIN_STOP_WAIT = 30.0
    _start(module, data, monkeypatch)
    capsys.readouterr()
    answers = iter([False, False, True])
    monkeypatch.setattr(module, "_claude_mcp_signed_in", lambda: next(answers, True))
    _, out = _stop(module, data, monkeypatch, capsys=capsys)
    assert "approval is in" in json.loads(out)["reason"]


def test_record_connected_ends_pending_and_stops_blocking(env, monkeypatch, capsys) -> None:
    module, data = env
    _start(module, data, monkeypatch)
    capsys.readouterr()
    assert module.main(["--host", "claude", "--record-connected", "--data-dir", str(data)]) == 0
    assert json.loads(capsys.readouterr().out) == {"connected": True, "reconnected": False}
    assert _state(data)["state"] == "done"
    _, out = _stop(module, data, monkeypatch, capsys=capsys)
    assert out == ""


def test_record_connected_reports_reconnect(env, monkeypatch, capsys) -> None:
    module, data = env
    data.mkdir(parents=True)
    (data / "import-offer.json").write_text(json.dumps({"connected_at": "2026-10-20T00:00:00Z"}))
    _start(module, data, monkeypatch)
    capsys.readouterr()
    module.main(["--host", "claude", "--record-connected", "--data-dir", str(data)])
    assert json.loads(capsys.readouterr().out)["reconnected"] is True


def test_record_connected_still_reports_disconnect_reconnect(env, monkeypatch, capsys) -> None:
    module, data = env
    data.mkdir(parents=True)
    (data / "import-offer.json").write_text(json.dumps({"disconnected_at": "2026-10-26T00:00:00Z"}))
    module.main(["--host", "claude", "--record-connected", "--data-dir", str(data)])
    assert json.loads(capsys.readouterr().out)["reconnected"] is True


def test_other_session_never_blocked(env, monkeypatch, capsys) -> None:
    module, data = env
    _start(module, data, monkeypatch, session="sess-1")
    capsys.readouterr()
    code, out = _stop(module, data, monkeypatch, session="sess-2", capsys=capsys)
    assert code == 0 and out == ""


def test_missing_session_never_blocked(env, monkeypatch, capsys) -> None:
    module, data = env
    _start(module, data, monkeypatch)
    capsys.readouterr()
    _stdin(monkeypatch, {"stop_hook_active": True})
    assert module._cmd_stop_check("claude", str(data)) == 0
    assert capsys.readouterr().out == ""


def test_block_count_bound(env, monkeypatch, capsys) -> None:
    module, data = env
    _start(module, data, monkeypatch)
    capsys.readouterr()
    blocked = 0
    for _ in range(module.SIGNIN_MAX_BLOCKS + 3):
        _, out = _stop(module, data, monkeypatch, capsys=capsys)
        if out:
            blocked += 1
    assert blocked == module.SIGNIN_MAX_BLOCKS
    assert _state(data)["state"] == "expired"


def test_time_bound(env, monkeypatch, capsys) -> None:
    module, data = env
    _start(module, data, monkeypatch)
    capsys.readouterr()
    state = _state(data)
    state["started"] = time.time() - module.SIGNIN_MAX_SECONDS - 1
    module._signin_save(str(data), state)
    _, out = _stop(module, data, monkeypatch, capsys=capsys)
    assert out == ""
    assert _state(data)["state"] == "expired"


def test_cancel_prompt_stops_blocking(env, monkeypatch, capsys) -> None:
    module, data = env
    _start(module, data, monkeypatch)
    capsys.readouterr()
    _stdin(monkeypatch, {"session_id": "sess-1", "prompt": "Not now, forget it"})
    assert module.main(["--host", "claude", "--offer-check", "--data-dir", str(data)]) == 0
    capsys.readouterr()
    assert _state(data)["state"] == "cancelled"
    _, out = _stop(module, data, monkeypatch, capsys=capsys)
    assert out == ""


@pytest.mark.parametrize("prompt", ["continue", "done", "ok", "approved", "I can't see the link"])
def test_setup_words_keep_the_sign_in_pending(prompt: str, env) -> None:
    module, _ = env
    assert module.SIGNIN_CANCEL_RE.match(prompt) is None


@pytest.mark.parametrize("prompt", ["stop", "Cancel that", "never mind", "Forget it", "not now"])
def test_cancel_words_end_the_sign_in(prompt: str, env) -> None:
    module, _ = env
    assert module.SIGNIN_CANCEL_RE.match(prompt) is not None


def test_state_write_failure_never_blocks(env, monkeypatch, capsys) -> None:
    module, data = env
    _start(module, data, monkeypatch)
    capsys.readouterr()

    def broken(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(module, "_write_json", broken)
    code, out = _stop(module, data, monkeypatch, capsys=capsys)
    assert code == 0 and out == ""


def test_stop_check_entry_point_fails_open(env, monkeypatch, capsys) -> None:
    module, data = env
    _stdin(monkeypatch, "{{{ garbage")
    assert module.main(["--host", "claude", "--stop-check", "--data-dir", str(data)]) == 0
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------- end to end and packaging


def test_stop_hook_subprocess_round_trip(tmp_path: Path) -> None:
    config = tmp_path / "claude-home"
    config.mkdir()
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config), ENABLE_CLAUDEAI_MCP_SERVERS="false")
    env.pop("SUBSTRATE_DATA_DIR", None)
    env["SUBSTRATE_SIGNIN_STOP_WAIT"] = "0"
    start = subprocess.run(
        [sys.executable, str(SCRIPT), "--host", "claude", "--signin-start"],
        input=json.dumps({"session_id": "s-e2e", "tool_response": AUTH_LINK}),
        capture_output=True, text=True, env=env, timeout=30)
    assert start.returncode == 0 and AUTH_LINK in start.stdout
    stop = subprocess.run(
        [sys.executable, str(SCRIPT), "--host", "claude", "--stop-check"],
        input=json.dumps({"session_id": "s-e2e"}),
        capture_output=True, text=True, env=env, timeout=60)
    assert stop.returncode == 0
    assert json.loads(stop.stdout)["decision"] == "block"


def test_hook_commands_are_wired_and_fail_open() -> None:
    hooks = json.loads((PLUG / "hooks" / "hooks.json").read_text())["hooks"]
    post = [g for g in hooks["PostToolUse"] if g.get("matcher")]
    assert [g["matcher"] for g in post] == [AUTH_TOOL]
    start_cmd = post[0]["hooks"][0]["command"]
    assert "--signin-start" in start_cmd and start_cmd.endswith("|| true")
    stop_groups = [h for g in hooks["Stop"] for h in g["hooks"] if h.get("type") == "command"]
    assert len(stop_groups) == 1
    stop = stop_groups[0]
    assert "--stop-check" in stop["command"] and stop["command"].endswith("|| true")
    # Longer than the wait (SIGNIN_STOP_WAIT, 30 s) and below the 600 s default.
    assert 30 < stop["timeout"] < 600


def test_claude_and_codex_scripts_stay_byte_identical() -> None:
    assert SCRIPT.read_bytes() == CODEX_SCRIPT.read_bytes()


def test_signin_start_replaces_tool_notes_and_shows_user_link(env, monkeypatch, capsys) -> None:
    module, data = env
    _start(module, data, monkeypatch, response=[{"type": "text", "text": (
        "Sign in: " + AUTH_LINK + "\nIf the redirect page shows a connection error, paste the URL here.")}])
    out = json.loads(capsys.readouterr().out)
    replaced = out["hookSpecificOutput"]["updatedToolOutput"]
    assert AUTH_LINK in replaced and "paste" not in replaced.lower() and "localhost" not in replaced.lower()
    assert AUTH_LINK in out["systemMessage"]
    assert "Open this link, sign in and choose **Approve connection**." in out["systemMessage"]


def test_signin_start_without_link_keeps_the_tool_output(env, monkeypatch, capsys) -> None:
    module, data = env
    _start(module, data, monkeypatch, response="no url here")
    out = json.loads(capsys.readouterr().out)
    assert "updatedToolOutput" not in out["hookSpecificOutput"] and "systemMessage" not in out
