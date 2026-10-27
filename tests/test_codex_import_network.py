"""Network-blocked versus server-timeout classification in substrate_sync.py.

Codex runs commands in a sandbox where the outbound connection can fail at once
(DNS, refused, EPERM). That used to be retried as a server problem and reported
as claim_timeout. Now it is network_blocked: one fast failure, no retries.
A real server timeout keeps the old retry budget and claim_timeout. No live
network: every outbound call is mocked, except one refused loopback port.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import socket
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CODEX_SCRIPT = REPO / "plugins" / "substrate-codex" / "scripts" / "substrate_sync.py"
CLAUDE_SCRIPT = REPO / "plugins" / "substrate-claude" / "scripts" / "substrate_sync.py"
CLAIM = "claim_" + "a" * 40
TICKET = "sk_imp_" + "T" * 40
MCP_URL = "https://substrate.example.test/mcp"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("substrate_sync_net_%d" % id(path), str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sync = _load(CODEX_SCRIPT)


class _FakeOpener:
    """Stands in for urllib's opener; every open() raises the given error."""

    def __init__(self, error: BaseException, calls: list) -> None:
        self._error = error
        self._calls = calls

    def open(self, request, timeout):  # noqa: ANN001, ANN201
        self._calls.append(timeout)
        raise self._error


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status = 200
        self.headers = {}

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *exc):  # noqa: ANN002, ANN204
        return False

    def read(self, limit: int = -1) -> bytes:
        return self._body


class _OkOpener:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def open(self, request, timeout):  # noqa: ANN001, ANN201
        return _Response(self._body)


@pytest.fixture()
def fast(monkeypatch):
    """No real sleeping; a tiny backoff cap and budget so retries would show up as calls."""
    waits: list = []
    monkeypatch.setattr(sync, "_sleep", lambda seconds: waits.append(seconds))
    monkeypatch.setenv("SUBSTRATE_SYNC_BACKOFF_MAX", "60")
    return waits


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))  # never scan the real ~/.codex
    monkeypatch.delenv("SUBSTRATE_MCP_URL", raising=False)
    return tmp_path


def _patch_opener(monkeypatch, opener) -> None:  # noqa: ANN001
    monkeypatch.setattr(sync.urllib.request, "build_opener", lambda *handlers: opener)


# ---------------------------------------------------------------------------
# Classification


@pytest.mark.parametrize(
    "error",
    [
        socket.gaierror(-2, "Name or service not known"),
        ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
        ConnectionResetError(errno.ECONNRESET, "reset before any answer"),
        PermissionError(errno.EPERM, "Operation not permitted"),
        PermissionError(errno.EACCES, "Permission denied"),
        OSError(errno.ENETUNREACH, "Network is unreachable"),
        OSError(errno.EHOSTUNREACH, "No route to host"),
    ],
)
def test_blocked_errors_are_network_blocked(error: BaseException) -> None:
    assert isinstance(sync._network_error(error), sync.NetworkBlocked)


@pytest.mark.parametrize(
    "reason",
    [
        socket.gaierror(-3, "Temporary failure in name resolution"),
        ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
        PermissionError(errno.EPERM, "Operation not permitted"),
    ],
)
def test_urlerror_wrapping_a_blocked_reason_is_network_blocked(reason: BaseException) -> None:
    error = urllib.error.URLError(reason)
    assert isinstance(sync._network_error(error), sync.NetworkBlocked)


@pytest.mark.parametrize(
    "error",
    [
        socket.timeout("timed out"),
        TimeoutError("timed out"),
        urllib.error.URLError(socket.timeout("timed out")),
    ],
)
def test_timeouts_stay_retryable_server_timeouts(error: BaseException) -> None:
    classified = sync._network_error(error)
    assert isinstance(classified, sync.Retryable)
    assert not isinstance(classified, sync.NetworkBlocked)
    assert classified.reason.startswith("network: ")


def test_other_oserror_keeps_the_old_retryable_classification() -> None:
    classified = sync._network_error(OSError(errno.EIO, "I/O error"))
    assert isinstance(classified, sync.Retryable)
    assert classified.reason == "network: OSError"


def test_network_blocked_is_not_a_retryable_or_fatal_subclass() -> None:
    # _with_retries must let it through at once; _cmd_upload must catch it before Fatal.
    assert not issubclass(sync.NetworkBlocked, sync.Retryable)
    assert not issubclass(sync.NetworkBlocked, sync.Fatal)


# ---------------------------------------------------------------------------
# Claim exchange (foreground, before any upload)


def test_claim_blocked_is_one_attempt_no_retry(monkeypatch, fast) -> None:
    calls: list = []
    _patch_opener(
        monkeypatch, _FakeOpener(urllib.error.URLError(socket.gaierror(-2, "no dns")), calls)
    )
    with pytest.raises(sync.NetworkBlocked):
        sync._with_retries(lambda: sync._claim_ticket(MCP_URL, CLAIM), budget=sync.CLAIM_BUDGET)
    assert len(calls) == 1 and fast == []


def test_claim_blocked_cli_prints_network_blocked_not_claim_timeout(
    monkeypatch, fast, home, capsys
) -> None:
    calls: list = []
    _patch_opener(
        monkeypatch, _FakeOpener(urllib.error.URLError(socket.gaierror(-2, "no dns")), calls)
    )
    code = sync.main(
        [
            "--host",
            "codex",
            "--data-dir",
            str(home / "d"),
            "--upload",
            "--all",
            "--background",
            "--claim",
            CLAIM,
            "--mcp-url",
            MCP_URL,
        ]
    )
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 2
    assert out["error"] == "network_blocked"
    assert out["message"] == (
        "Network is blocked here. Rerun this command with network access. Nothing was uploaded."
    )
    assert "claim_timeout" not in json.dumps(out)
    assert len(calls) == 1 and fast == []  # no 14 s of backoff before giving up
    assert not (home / "d" / "import-status.json").exists()  # nothing started


def test_claim_real_server_timeout_still_claim_timeout(monkeypatch, fast, home, capsys) -> None:
    calls: list = []
    _patch_opener(monkeypatch, _FakeOpener(socket.timeout("timed out"), calls))
    monkeypatch.setattr(sync, "CLAIM_BUDGET", 5.0)
    code = sync.main(
        [
            "--host",
            "codex",
            "--data-dir",
            str(home / "d"),
            "--upload",
            "--all",
            "--background",
            "--claim",
            CLAIM,
            "--mcp-url",
            MCP_URL,
        ]
    )
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 2 and out["error"] == "claim_timeout"
    assert "nothing was uploaded" in out["message"] and CLAIM not in json.dumps(out)
    assert len(calls) > 1  # the timeout path still retries inside its budget


def test_claim_normal_answer_unchanged(monkeypatch, fast) -> None:
    body = json.dumps({"ticket": TICKET}).encode("utf-8")
    _patch_opener(monkeypatch, _OkOpener(body))
    assert sync._claim_ticket(MCP_URL, CLAIM) == TICKET


def test_claim_404_unchanged(monkeypatch, fast) -> None:
    error = urllib.error.HTTPError(MCP_URL, 404, "Not Found", {}, None)
    _patch_opener(monkeypatch, _FakeOpener(error, []))
    with pytest.raises(sync.Fatal) as info:
        sync._claim_ticket(MCP_URL, CLAIM)
    assert info.value.reason == "claim_not_found"


def test_claim_refused_loopback_port_is_network_blocked() -> None:
    # A real connect to a loopback port with nothing listening: refused at once, no retry.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(sync.NetworkBlocked):
        sync._claim_ticket("http://127.0.0.1:%d/mcp" % port, CLAIM)


# ---------------------------------------------------------------------------
# MCP client (upload handshake and batches)


def test_mcp_post_blocked_is_network_blocked(monkeypatch) -> None:
    calls: list = []
    _patch_opener(
        monkeypatch, _FakeOpener(urllib.error.URLError(socket.gaierror(-2, "no dns")), calls)
    )
    client = sync.McpClient(MCP_URL, TICKET)
    with pytest.raises(sync.NetworkBlocked):
        client._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})


def test_mcp_post_timeout_is_retryable(monkeypatch) -> None:
    _patch_opener(monkeypatch, _FakeOpener(socket.timeout("timed out"), []))
    client = sync.McpClient(MCP_URL, TICKET)
    with pytest.raises(sync.Retryable) as info:
        client._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert info.value.reason.startswith("network: ")


def test_upload_handshake_blocked_reports_network_blocked(monkeypatch, fast, home, capsys) -> None:
    ticket = home / "ticket.txt"
    ticket.write_text(TICKET, encoding="utf-8")

    def blocked(self):  # noqa: ANN001, ANN202
        raise sync.NetworkBlocked("network_blocked")

    monkeypatch.setattr(sync.McpClient, "initialize", blocked)
    code = sync.main(
        [
            "--host",
            "codex",
            "--data-dir",
            str(home / "d"),
            "--upload",
            "--all",
            "--ticket-file",
            str(ticket),
            "--mcp-url",
            MCP_URL,
        ]
    )
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert code == 1
    assert "Network is blocked here. Rerun this command with network access." in last
    assert "did not answer" not in last
    status = json.loads((home / "d" / "import-status.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"


# ---------------------------------------------------------------------------
# Both copies stay byte-identical


def test_claude_and_codex_sync_scripts_are_byte_identical() -> None:
    assert CODEX_SCRIPT.read_bytes() == CLAUDE_SCRIPT.read_bytes()


def test_codex_offer_names_codex_import_skill_not_claude() -> None:
    """The Codex hook must point at the Codex import skill (``$substrate-import``), never the Claude one."""
    for module in (sync, _load(CLAUDE_SCRIPT)):
        codex_text = module._offer_text("codex", "/tmp/data", "sess-1", None)
        assert "substrate-claude:substrate-import" not in codex_text
        assert "Skill tool" not in codex_text
        assert "use the `$substrate-import` skill" in codex_text
        setup = module._setup_text("codex", "/tmp/data", "sess-1", None, offer=True)
        assert "substrate-claude:substrate-import" not in setup
        assert "follow $substrate-import" in setup


def test_claude_offer_still_names_claude_import_skill() -> None:
    text = sync._offer_text("claude", "/tmp/data", "sess-1", None)
    assert "invoke the Skill tool with skill `substrate-claude:substrate-import`" in text
    setup = sync._setup_text("claude", "/tmp/data", "sess-1", None, offer=True)
    assert "/substrate-claude:substrate-import (or $substrate-import)" in setup
