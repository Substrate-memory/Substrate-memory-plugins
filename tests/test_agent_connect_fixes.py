"""Agent connect fix batch 1 (plugins): F2, F3, F20, F21, F25, F27.

Every network step has a time budget: a server that accepts the connection and
never answers must end the step with a clear message, not a 20-minute retry
loop. Wording checks keep the connect flow the same in every text.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CLAUDE = REPO / "plugins" / "substrate-claude"
CODEX = REPO / "plugins" / "substrate-codex"
CLAIM = "claim_" + "a" * 40


def _load_sync(path: Path):
    spec = importlib.util.spec_from_file_location("substrate_sync_fixes_%d" % id(path), str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sync = _load_sync(CLAUDE / "scripts" / "substrate_sync.py")


class _Silent(BaseHTTPRequestHandler):
    """Accepts the request and never answers (until the test ends)."""

    protocol_version = "HTTP/1.1"
    release = threading.Event()

    def log_message(self, *args):  # noqa: ANN002
        return

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        self.release.wait(30)


class _Quiet(ThreadingHTTPServer):
    daemon_threads = True


@pytest.fixture()
def silent_url():
    handler = type("Silent", (_Silent,), {"release": threading.Event()})
    server = _Quiet(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/mcp" % server.server_port
    handler.release.set()
    server.shutdown()
    server.server_close()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("SUBSTRATE_SYNC_BACKOFF_MAX", "0")
    monkeypatch.delenv("SUBSTRATE_MCP_URL", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# F20: every step has a budget and says what happened.


def test_default_budgets_bound_each_step() -> None:
    assert sync.CLAIM_TIMEOUT <= sync.REQUEST_TIMEOUT
    assert sync.CLAIM_BUDGET + sync.CLAIM_TIMEOUT <= 40
    assert sync.SETUP_BUDGET + sync.SETUP_TIMEOUT <= 70
    assert sync.UPLOAD_BUDGET >= sync.REQUEST_TIMEOUT  # a background batch may take a while


def test_claim_on_a_silent_server_fails_fast_with_a_clear_message(home, silent_url, monkeypatch, capsys):
    monkeypatch.setattr(sync, "CLAIM_TIMEOUT", 0.3)
    monkeypatch.setattr(sync, "CLAIM_BUDGET", 1.0)
    started = time.monotonic()
    code = sync.main(["--host", "claude", "--data-dir", str(home / "d"), "--upload", "--all",
                      "--background", "--claim", CLAIM, "--mcp-url", silent_url])
    elapsed = time.monotonic() - started
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 2 and out["error"] == "claim_timeout"
    assert "nothing was uploaded" in out["message"] and CLAIM not in json.dumps(out)
    assert elapsed < 5


def test_handshake_on_a_silent_server_stops_with_a_clear_summary(home, silent_url, monkeypatch, capsys):
    monkeypatch.setattr(sync, "SETUP_TIMEOUT", 0.3)
    monkeypatch.setattr(sync, "SETUP_BUDGET", 1.0)
    ticket = home / "ticket.txt"
    ticket.write_text("sk_imp_" + "x" * 40, encoding="utf-8")
    started = time.monotonic()
    code = sync.main(["--host", "claude", "--data-dir", str(home / "d"), "--upload", "--all",
                      "--ticket-file", str(ticket), "--mcp-url", silent_url])
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert code == 1 and "did not answer" in out and "nothing new was imported" in out
    assert elapsed < 5
    status = json.loads((home / "d" / "import-status.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"


def test_budget_stops_retries_before_the_loop_grows(monkeypatch) -> None:
    calls = []

    def action():
        calls.append(1)
        raise sync.Retryable("network: timeout")

    monkeypatch.setattr(sync, "_sleep", lambda seconds: None)
    monkeypatch.setenv("SUBSTRATE_SYNC_BACKOFF_MAX", "60")
    with pytest.raises(sync.Unavailable) as info:
        sync._with_retries(action, budget=5.0)
    assert info.value.reason == "server_unavailable (network: timeout)"
    assert isinstance(info.value, sync.Fatal) and len(calls) == 3  # waits of 2 s and 4 s fit in 5 s; 8 s does not


def test_no_budget_keeps_the_old_bounded_retry(monkeypatch) -> None:
    monkeypatch.setattr(sync, "_sleep", lambda seconds: None)
    with pytest.raises(sync.Fatal) as info:
        sync._with_retries(lambda: (_ for _ in ()).throw(sync.Retryable("http_503")))
    assert not isinstance(info.value, sync.Unavailable)


# ---------------------------------------------------------------------------
# F2 / F3 / F25: wording of the connect and import texts.


def _text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_setup_text_pastes_the_link_and_invents_no_network_advice(tmp_path) -> None:
    text = " ".join(sync._setup_text("claude", str(tmp_path), "s1", None, False).split())
    assert "paste the full sign-in URL" in text and "Paste the link again" in text
    assert "never mention ports, callback addresses or localhost" in text.lower()
    assert "pasting the address-bar URL only if" not in text
    assert "Offer this once" in text


@pytest.mark.parametrize("path", [
    CLAUDE / "commands" / "substrate-connect.md",
    CODEX / "skills" / "substrate-connect" / "SKILL.md",
])
def test_connect_texts_paste_the_link_and_ignore_the_mcp_offer(path: Path) -> None:
    text = _text(path)
    assert "paste the full" in text.lower() or "paste that full link" in text.lower()
    assert "never mention ports" in text.lower()
    assert "Offer this once" in text


@pytest.mark.parametrize("path", [
    CLAUDE / "skills" / "substrate-memory" / "SKILL.md",
    CODEX / "skills" / "substrate-memory" / "SKILL.md",
    CODEX / "skills" / "substrate-import" / "SKILL.md",
])
def test_memory_and_import_skills_ignore_the_mcp_offer(path: Path) -> None:
    assert "Offer this once" in _text(path)


def test_import_texts_handle_timeouts_and_the_account_check() -> None:
    connect = _text(CLAUDE / "commands" / "substrate-connect.md")
    assert "already_imported" in connect and "memory_import_status" in connect
    assert "--preview --force" in _text(CLAUDE / "commands" / "substrate-import.md")
    for path in (CLAUDE / "commands" / "substrate-import.md", CODEX / "skills" / "substrate-import" / "SKILL.md"):
        text = _text(path)
        assert "claim_timeout" in text and "server_unavailable" in text
    codex_connect = _text(CODEX / "skills" / "substrate-connect" / "SKILL.md")
    assert "already_imported" in codex_connect and "no_imports" in codex_connect


# ---------------------------------------------------------------------------
# F27: one Codex order everywhere: approve, hooks, /hooks, new session, continue.

_CODEX_ORDER = {
    "docs/install.md": ["codex mcp login substrate-memory", "codex_user_hooks.py install",
                        "approve the substrate hooks once"],
    "docs/installation.md": ["codex mcp login substrate-memory", "approve the substrate hooks once in `/hooks`"],
    "plugins/substrate-codex/README.md": ["codex mcp login substrate-memory", "approve the hooks once in `/hooks`"],
    "plugins/substrate-codex/INSTALL.md": ["approve the connection", "install the hooks",
                                           "approves the hooks in `/hooks`"],
    "plugins/substrate-codex/skills/substrate-connect/SKILL.md": [
        "approve the connection first", "same reply: install the substrate hooks", "approve them once in /hooks"],
}


@pytest.mark.parametrize("relative", sorted(_CODEX_ORDER))
def test_codex_steps_run_in_the_same_order(relative: str) -> None:
    text = _text(REPO / relative).lower()
    positions = [text.find(marker) for marker in _CODEX_ORDER[relative]]
    assert all(position >= 0 for position in positions), (relative, positions)
    assert positions == sorted(positions), (relative, positions)


def test_codex_last_line_says_new_session_and_continue() -> None:
    for relative in ("docs/install.md", "plugins/substrate-codex/skills/substrate-connect/SKILL.md",
                     "plugins/substrate-codex/README.md"):
        text = _text(REPO / relative)
        assert "new session" in text and "continue" in text, relative


# ---------------------------------------------------------------------------
# H6: a trial that ended (HTTP 402) is said plainly, in every plugin.


class _PaymentRequired(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: ANN002
        return

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        self.send_response(402)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_upload_on_a_trial_ended_account_says_so_plainly(home, monkeypatch, capsys):
    server = _Quiet(("127.0.0.1", 0), _PaymentRequired)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ticket = home / "ticket.txt"
        ticket.write_text("sk_imp_" + "x" * 40, encoding="utf-8")
        url = "http://127.0.0.1:%d/mcp" % server.server_port
        code = sync.main(["--host", "claude", "--data-dir", str(home / "d"), "--upload", "--all",
                          "--ticket-file", str(ticket), "--mcp-url", url])
        out = capsys.readouterr().out.strip().splitlines()[-1]
        assert code == 1
        assert out.startswith("Your Substrate trial has ended. Add a card at http://127.0.0.1:")
        assert out.count("/billing to keep using memory.") == 1
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("path", [
    CLAUDE / "skills" / "substrate-memory" / "SKILL.md",
    CODEX / "skills" / "substrate-memory" / "SKILL.md",
    REPO / "plugins" / "substrate-mcp" / "skills" / "substrate-memory" / "SKILL.md",
])
def test_every_skill_says_the_trial_ended_plainly(path: Path) -> None:
    text = _text(path)
    assert "payment_required" in text and "/billing to keep using memory" in text


# ---------------------------------------------------------------------------
# N4 / N7 / N9 / F3 (round 2, Claude Code): exact SYNC form, install-prompt session, wording.


def test_commands_name_the_exact_sync_form_and_the_tool_load() -> None:
    connect = _text(CLAUDE / "commands" / "substrate-connect.md")
    assert "Run it exactly in that form" in connect and "full interpreter path" in connect
    assert "load it first with ToolSearch" in connect
    assert "Never describe this session or computer as remote" in connect
    assert "never say" in connect and "already in Substrate" in connect
    setup = _text_of_setup()
    assert "load it with ToolSearch first" in setup and "Never describe this session" in setup
    assert "never mention ports" in setup.lower()


def _text_of_setup() -> str:
    return " ".join(sync._setup_text("claude", "/tmp/x", "s1", None, False).split())


def test_install_prompt_session_is_never_imported(tmp_path) -> None:
    prompt = ("Install Substrate memory in this agent by following "
              "https://trysubstrate.co/install.md?via=app")
    setup_line = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}})
    other_line = json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}})
    assistant_line = json.dumps({"type": "assistant", "message": {"role": "assistant",
                                                                    "content": "I can install it: " + prompt}})
    setup = tmp_path / "setup.jsonl"
    setup.write_text(other_line + "\n" + setup_line + "\n", encoding="utf-8")
    plain = tmp_path / "plain.jsonl"
    plain.write_text(other_line + "\n" + assistant_line + "\n", encoding="utf-8")
    assert sync._has_install_prompt(str(setup)) is True
    assert sync._has_install_prompt(str(plain)) is False
