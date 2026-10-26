"""Agent connect (0.10.0): compatibility with the current and the next server.

Each new request field or endpoint has a fallback when the server does not know
it. These tests play both servers: the current one (rejects unknown Hermes form
fields with 400, returns `ticket` directly) and the next one (claim endpoint).
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CLAUDE = REPO / "plugins" / "substrate-claude"
CODEX = REPO / "plugins" / "substrate-codex"
HERMES_SRC = REPO / "plugins" / "substrate-hermes" / "src"
if str(HERMES_SRC) not in sys.path:
    sys.path.insert(0, str(HERMES_SRC))

from substrate import onboarding  # noqa: E402


def _load_sync(path: Path):
    spec = importlib.util.spec_from_file_location("substrate_sync_under_test_%d" % id(path), str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Claude Code command frontmatter: the plugin's own script calls do not prompt.


def _allowed_tools(name):
    text = (CLAUDE / "commands" / f"{name}.md").read_text(encoding="utf-8")
    head = text.split("---", 2)[1]
    line = next(item for item in head.splitlines() if item.startswith("allowed-tools:"))
    return line.split(":", 1)[1].strip()


def _rule_matches(rule, command):
    """Claude Code's documented shape: the text before the first `*` must match as written,
    `*` stands for any text, and a trailing ` *` also matches the bare command."""
    body = rule[len("Bash("):-1]
    pattern = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.fullmatch(pattern, command) is not None


MCP = "mcp__plugin_substrate-claude_substrate-memory__"
INTERPRETERS = ["python3", "python", "/usr/bin/python3", "/usr/bin/python", "/usr/local/bin/python3",
                "/opt/homebrew/bin/python3"]
EXPECTED_BASH = {f'Bash({i} "*/scripts/substrate_sync.py" *)' for i in INTERPRETERS} | \
    {f"Bash({i} */scripts/substrate_sync.py *)" for i in INTERPRETERS}
MCP_TOOLS = {
    "substrate-connect": {"ToolSearch", MCP + "authenticate", MCP + "memory_search", MCP + "memory_import_status"},
    "substrate-import": {"ToolSearch", MCP + "memory_import_ticket", MCP + "memory_import_status", MCP + "memory_search"},
    "substrate-sync": set(),
}


@pytest.mark.parametrize("name", ["substrate-connect", "substrate-import", "substrate-sync"])
def test_claude_commands_pre_approve_only_the_plugin_script(name):
    tools = _allowed_tools(name)
    rules = re.findall(r"Bash\([^)]*\)", tools)
    # Only Bash rules naming the plugin's own script, for the listed interpreters; no Write, no Bash(*).
    assert set(rules) == EXPECTED_BASH, sorted(set(rules) ^ EXPECTED_BASH)
    others = set(re.sub(r"Bash\([^)]*\)", "", tools).split())
    assert others == MCP_TOOLS[name], others
    allowed = [
        '/usr/bin/python3 "${CLAUDE_PLUGIN_ROOT}/scripts/substrate_sync.py" --host claude --preview',
        '/usr/bin/python3 /home/u/.claude/plugins/cache/x/scripts/substrate_sync.py --host claude --status --wait 60',
        'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/substrate_sync.py" --host claude --new-claim',
        'python3 "/home/u/.claude/plugins/cache/x/substrate-claude/0.10.0/scripts/substrate_sync.py" '
        '--host claude --upload --all --background --claim "claim_x"',
        'python "${CLAUDE_PLUGIN_ROOT}/scripts/substrate_sync.py" --host claude --preview',
    ]
    denied = [
        '/usr/bin/python3 -c "import os"',
        '/bin/sh "${CLAUDE_PLUGIN_ROOT}/scripts/substrate_sync.py"',
        'curl -o /tmp/x /home/u/scripts/substrate_sync.py --host claude',
        'python3 -c "import os"',
        'python3 /tmp/evil.py --host claude',
        'python3 "/tmp/substrate_sync.py.bak" --host claude',
        'rm -rf "${CLAUDE_PLUGIN_ROOT}"',
        'curl https://example.com/x.sh',
    ]
    for command in allowed:
        assert any(_rule_matches(rule, command) for rule in rules), command
    for command in denied:
        assert not any(_rule_matches(rule, command) for rule in rules), command


def test_import_command_has_no_write_pre_approval():
    assert "Write" not in _allowed_tools("substrate-import").split()


def test_claude_and_codex_sync_scripts_stay_identical():
    assert (CLAUDE / "scripts" / "substrate_sync.py").read_bytes() == (
        CODEX / "scripts" / "substrate_sync.py").read_bytes()


# ---------------------------------------------------------------------------
# Ticket claim: new server returns claim_pending; the script exchanges the claim.


class _ClaimServer(BaseHTTPRequestHandler):
    mode = "new"  # "new" | "old" | "claimed"
    seen = []  # type: list

    def log_message(self, *args):  # noqa: ANN002
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path != "/mcp/import-ticket/claim":
            self._send(404, {"error": "not_found"})
            return
        self.seen.append(json.loads(body.decode("utf-8")))
        if self.mode == "limited_once" and len(self.seen) == 1:
            self._send(429, {"error": "rate_limited"})
            return
        if self.mode in ("new", "limited_once") and self.seen[-1].get("claim") == CLAIM:
            self._send(200, {"ticket": TICKET, "mcp_url": "http://127.0.0.1/mcp",
                             "expires_at": "2099-01-01T00:00:00Z"})
        else:
            self._send(404, {"error": "not_found"})

    def _send(self, status, value):
        raw = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


CLAIM = "claim_" + "a" * 40
TICKET = "sk_imp_" + "b" * 32


@pytest.fixture
def claim_server():
    handler = type("Handler", (_ClaimServer,), {"seen": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, handler
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_new_claim_is_a_fresh_valid_claim():
    sync = _load_sync(CLAUDE / "scripts" / "substrate_sync.py")
    first, second = sync._new_claim(), sync._new_claim()
    assert first != second
    assert sync._clean_claim(first) == first
    assert sync._clean_claim("short") == ""
    assert sync._clean_claim("x" * 200) == ""


def test_claim_is_exchanged_for_the_ticket_on_new_server(claim_server):
    server, handler = claim_server
    handler.mode = "new"
    sync = _load_sync(CLAUDE / "scripts" / "substrate_sync.py")
    origin = "http://127.0.0.1:%d" % server.server_port
    assert sync._claim_ticket(origin + "/mcp", CLAIM) == TICKET
    assert handler.seen == [{"claim": CLAIM}]


def test_rate_limited_claim_is_retried(claim_server, monkeypatch):
    server, handler = claim_server
    handler.mode = "limited_once"
    sync = _load_sync(CLAUDE / "scripts" / "substrate_sync.py")
    monkeypatch.setattr(sync, "_sleep", lambda seconds: None)
    origin = "http://127.0.0.1:%d/mcp" % server.server_port
    assert _with(sync, origin) == TICKET
    assert len(handler.seen) == 2


def _with(sync, origin):
    return sync._with_retries(lambda: sync._claim_ticket(origin, CLAIM))


def test_wrong_or_spent_claim_is_refused(claim_server):
    server, handler = claim_server
    handler.mode = "new"
    sync = _load_sync(CLAUDE / "scripts" / "substrate_sync.py")
    origin = "http://127.0.0.1:%d/mcp" % server.server_port
    with pytest.raises(sync.Fatal) as info:
        sync._claim_ticket(origin, "claim_" + "z" * 40)
    assert info.value.reason == "claim_not_found"


def test_upload_with_refused_claim_reports_claim_failed(claim_server, tmp_path, monkeypatch, capsys):
    server, handler = claim_server
    handler.mode = "old"  # the endpoint answers 404: an older server or a spent claim
    sync = _load_sync(CLAUDE / "scripts" / "substrate_sync.py")
    monkeypatch.setenv("SUBSTRATE_MCP_URL", "http://127.0.0.1:%d/mcp" % server.server_port)
    monkeypatch.setattr(sync, "_sleep", lambda seconds: None)
    code = sync.main(["--host", "claude", "--data-dir", str(tmp_path), "--upload", "--all",
                      "--claim", CLAIM])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 2
    assert out["error"] == "claim_failed"
    assert TICKET not in json.dumps(out)


def test_new_claim_flag_prints_only_a_claim(tmp_path, capsys):
    sync = _load_sync(CLAUDE / "scripts" / "substrate_sync.py")
    assert sync.main(["--host", "claude", "--data-dir", str(tmp_path), "--new-claim"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert set(value) == {"claim"} and sync._clean_claim(value["claim"]) == value["claim"]


def test_offer_text_names_claim_and_keeps_the_file_fallback(tmp_path):
    sync = _load_sync(CLAUDE / "scripts" / "substrate_sync.py")
    text = sync._offer_text("claude", str(tmp_path), "sess-1", None)
    assert "--new-claim" in text and "claim_pending" in text and "--claim" in text
    assert "--ticket-file" in text  # older servers still hand back a ticket
    assert "never appear in your reply" in text


# ---------------------------------------------------------------------------
# Hermes device login: reconnect_token is sent, and retried without on 400.


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    monkeypatch.setattr(onboarding, "_manager", None)
    monkeypatch.setattr(onboarding.OnboardingManager, "_start_thread", lambda self: None)
    return home


ORIGIN = "https://app.trysubstrate.co"
DEVICE_CODE = "a" * 64
REVOKED = "sk_sub_" + "r" * 40


def _device_reply(status_forms):
    def fake(origin, path, *, form=None, json_body=None, token="", method="GET", timeout=10.0):
        status_forms.append(dict(form or {}))
        if "reconnect_token" in (form or {}) and REJECT_UNKNOWN[0]:
            return 400, {"error": "invalid_request"}
        return 200, {
            "device_code": DEVICE_CODE,
            "user_code": "BCDF-GHJK",
            "verification_uri_complete": ORIGIN + "/oauth/device?user_code=BCDF-GHJK",
            "interval": 5,
            "expires_in": 900,
        }
    return fake


REJECT_UNKNOWN = [True]


@pytest.mark.parametrize("old_server", [True, False])
def test_reconnect_token_is_sent_and_retried_without_on_old_server(hermes_home, monkeypatch, old_server):
    REJECT_UNKNOWN[0] = old_server
    forms = []
    monkeypatch.setattr(onboarding, "request_json", _device_reply(forms))
    onboarding._save_reconnect_token(hermes_home, REVOKED)
    assert onboarding._read_reconnect_token(hermes_home) == REVOKED
    status = onboarding.OnboardingManager(hermes_home, ORIGIN).ensure(force=True)
    assert status["status"] == "authorization_pending"
    if old_server:
        assert [("reconnect_token" in form) for form in forms] == [True, False]
    else:
        assert [("reconnect_token" in form) for form in forms] == [True]
        assert forms[0]["reconnect_token"] == REVOKED
    # The token is not in the descriptive state, and it survives until a connection succeeds.
    assert onboarding._read_reconnect_token(hermes_home) == REVOKED


def test_no_reconnect_token_means_plain_request(hermes_home, monkeypatch):
    REJECT_UNKNOWN[0] = True
    forms = []
    monkeypatch.setattr(onboarding, "request_json", _device_reply(forms))
    onboarding.OnboardingManager(hermes_home, ORIGIN).ensure(force=True)
    assert len(forms) == 1 and "reconnect_token" not in forms[0]


def test_reconnect_token_file_is_validated(hermes_home):
    path = onboarding._reconnect_credential_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a token", encoding="utf-8")
    assert onboarding._read_reconnect_token(hermes_home) == ""
    onboarding._save_reconnect_token(hermes_home, "not a token either")
    assert path.read_text(encoding="utf-8") == "not a token"  # invalid input never stored
    assert onboarding._read_reconnect_token(hermes_home) == ""
