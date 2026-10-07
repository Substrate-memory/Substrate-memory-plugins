"""Import offer + direct history upload (substrate_sync.py, Claude Code and Codex).

Covers the UserPromptSubmit offer hook, --preview/--list/--record-decision,
and --upload/--status against a local fake MCP server that records every
call. No network beyond 127.0.0.1, never the live service.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CLAUDE_PLUG = REPO / "plugins" / "substrate-claude"
CODEX_PLUG = REPO / "plugins" / "substrate-codex"
SCRIPT = CLAUDE_PLUG / "scripts" / "substrate_sync.py"
FIX = REPO / "tests" / "fixtures" / "substrate-claude"
REDACTION = json.loads((REPO / "contract" / "redaction-fixtures.json").read_text())

HERMES_SRC = str(REPO / "plugins" / "substrate-hermes" / "src")
if HERMES_SRC not in sys.path:
    sys.path.insert(0, HERMES_SRC)

from substrate import contract as c  # noqa: E402

TICKET = "sk_imp_" + "T" * 40


def _load():
    spec = importlib.util.spec_from_file_location("substrate_sync_t", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _load()


# ---------------------------------------------------------------------------
# Transcript builders
# ---------------------------------------------------------------------------

def _ts(day: int, minute: int = 0) -> str:
    return "2026-%02d-%02dT10:%02d:00.000Z" % (9 if day <= 30 else 10, (day - 1) % 30 + 1, minute)


def write_claude_session(home: Path, session_id: str, turns: int = 2, *, day: int = 1,
                         project: str = "proj", tool_calls: int = 0, result_bytes: int = 10,
                         prompt: str = "Question number {n} about deploys",
                         cwd: str = "/home/u/proj") -> Path:
    path = home / "projects" / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for n in range(turns):
        stamp = _ts(day, n % 60)
        lines.append({"type": "user", "sessionId": session_id, "timestamp": stamp, "cwd": cwd,
                      "message": {"role": "user", "content": prompt.format(n=n)}})
        content = [{"type": "text", "text": f"Answer {n}."}]
        for k in range(tool_calls):
            content.append({"type": "tool_use", "id": f"toolu_{n}_{k}", "name": "Bash",
                            "input": {"command": f"echo {k}"}})
        lines.append({"type": "assistant", "sessionId": session_id, "timestamp": stamp,
                      "message": {"role": "assistant", "content": content}})
        if tool_calls:
            results = [{"type": "tool_result", "tool_use_id": f"toolu_{n}_{k}",
                        "content": "r" * result_bytes} for k in range(tool_calls)]
            lines.append({"type": "user", "sessionId": session_id, "timestamp": stamp,
                          "message": {"role": "user", "content": results}})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    _age(path)
    return path


def write_codex_session(home: Path, session_id: str, turns: int = 2) -> Path:
    path = home / "sessions" / "2026" / "09" / "01" / f"rollout-2026-09-01T10-00-00-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "session_meta", "timestamp": _ts(1), "payload": {"id": session_id}}]
    for n in range(turns):
        lines.append({"type": "response_item", "timestamp": _ts(1, n), "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": f"codex question {n}"}]}})
        lines.append({"type": "response_item", "timestamp": _ts(1, n), "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": f"codex answer {n}"}]}})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    _age(path)
    return path


def _age(path: Path, seconds: float = 86400) -> None:
    """Old transcripts: only a file written in the last minutes looks live."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    claude_home = tmp_path / "claude-home"
    codex_home = tmp_path / "codex-home"
    data = tmp_path / "data"
    claude_home.mkdir()
    codex_home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("SUBSTRATE_DATA_DIR", str(data))
    monkeypatch.setenv("SUBSTRATE_SYNC_BACKOFF_MAX", "0")
    monkeypatch.delenv("SUBSTRATE_IMPORT_TICKET", raising=False)
    monkeypatch.delenv("SUBSTRATE_MCP_URL", raising=False)
    monkeypatch.setattr(sync, "_sleep", lambda seconds: None)
    return {"claude": claude_home, "codex": codex_home, "data": data, "tmp": tmp_path}


def run(*args: str, stdin: str | None = None, extra_env: dict | None = None,
        timeout: float = 60) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    if extra_env:
        environment.update(extra_env)
    return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin,
                          capture_output=True, text=True, timeout=timeout, env=environment)


def main_json(capsys, *args: str) -> tuple[int, list]:
    code = sync.main(list(args))
    out = capsys.readouterr().out
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            rows.append(json.loads(line))
    return code, rows


# ---------------------------------------------------------------------------
# Fake MCP server
# ---------------------------------------------------------------------------

def _event_key(item: dict) -> str:
    return hashlib.sha256(c.canonical_bytes({k: item[k] for k in
                                             ("kind", "session_id", "offset", "payload")})).hexdigest()


class FakeMcp:
    """Streamable-HTTP MCP stub: records calls, dedupes items like the server."""

    def __init__(self, *, sse: bool = False, stateful: bool = False,
                 server_name: str = "substrate-memory") -> None:
        self.sse = sse
        self.stateful = stateful
        self.server_name = server_name
        self.calls: list[dict] = []
        self.bodies: list[bytes] = []
        self.headers: list[dict] = []
        self.store: set[str] = set()
        self.http_queue: list[int] = []      # statuses for the next tools/call requests
        self.tool_error_queue: list[str] = []  # isError categories for next tools/call
        self.auth_fail_after: int | None = None
        self.poison = "POISON"
        self.import_calls = 0
        self.session_header = "sess-" + uuid.uuid4().hex
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: ANN002
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                with fake.lock:
                    fake.bodies.append(raw)
                    fake.headers.append(dict(self.headers))
                    status, body, extra = fake.handle(raw, dict(self.headers))
                self.send_response(status)
                for key, value in extra.items():
                    self.send_header(key, value)
                if body is None:
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if fake.sse and status == 200:
                    payload = ("event: message\ndata: " + json.dumps(body) + "\n\n").encode()
                    self.send_header("Content-Type", "text/event-stream")
                else:
                    payload = json.dumps(body).encode()
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/mcp" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def handle(self, raw: bytes, headers: dict):
        lowered = {k.lower(): v for k, v in headers.items()}
        if lowered.get("authorization") != "Bearer " + TICKET:
            return 401, {"error": "unauthorized"}, {}
        if "text/event-stream" not in lowered.get("accept", "") or \
                "application/json" not in lowered.get("accept", ""):
            return 406, {"error": "not_acceptable"}, {}
        if len(raw) > 262144:
            return 413, {"error": "payload_too_large"}, {}
        message = json.loads(raw)
        self.calls.append(message)
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": self.server_name, "version": "test"},
                      "instructions": "substrate-mcp-contract/2 test"}
            extra = {"Mcp-Session-Id": self.session_header} if self.stateful else {}
            return 200, {"jsonrpc": "2.0", "id": message["id"], "result": result}, extra
        if self.stateful and lowered.get("mcp-session-id") != self.session_header:
            return 400, {"error": "missing session"}, {}
        if method == "notifications/initialized":
            return 202, None, {}
        if method != "tools/call":
            return 200, {"jsonrpc": "2.0", "id": message.get("id"),
                         "error": {"code": -32601, "message": "Method not found"}}, {}
        if self.http_queue:
            status = self.http_queue.pop(0)
            return status, {"error": "queued"}, {"Retry-After": "0"}
        params = message["params"]
        if params["name"] != "memory_import":
            return 200, self._tool_error(message, "forbidden"), {}
        if self.auth_fail_after is not None and self.import_calls >= self.auth_fail_after:
            return 401, {"error": "unauthorized"}, {}
        if self.tool_error_queue:
            return 200, self._tool_error(message, self.tool_error_queue.pop(0)), {}
        arguments = params["arguments"]
        items = arguments["items"]
        assert 1 <= len(items) <= 64
        if any(self.poison in json.dumps(item) for item in items):
            return 200, self._tool_error(message, "invalid_request"), {}
        results = []
        for index, item in enumerate(items):
            if item.get("capture_origin") != "history_replay" or \
                    item.get("batch_id") != arguments.get("batch_id"):
                results.append({"index": index, "action": "rejected", "error": "invalid_request"})
                continue
            key = _event_key(item)
            if key in self.store:
                results.append({"index": index, "action": "duplicate"})
            else:
                self.store.add(key)
                results.append({"index": index, "action": "stored"})
        self.import_calls += 1
        body = {"contract_version": 2, "batch_id": arguments.get("batch_id"),
                "accepted": sum(1 for r in results if r["action"] != "rejected"),
                "rejected": sum(1 for r in results if r["action"] == "rejected"),
                "results": results}
        return 200, {"jsonrpc": "2.0", "id": message["id"], "result": {
            "content": [{"type": "text", "text": "Imported."}], "structuredContent": body}}, {}

    @staticmethod
    def _tool_error(message: dict, category: str) -> dict:
        body = {"contract_version": 2, "error": category}
        return {"jsonrpc": "2.0", "id": message["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(body)}],
            "structuredContent": body, "isError": True}}

    def import_requests(self) -> list[dict]:
        return [m for m in self.calls if m.get("method") == "tools/call"]

    def sent_items(self) -> list[dict]:
        return [item for m in self.import_requests()
                for item in m["params"]["arguments"]["items"]]


@pytest.fixture()
def server():
    fake = FakeMcp()
    yield fake
    fake.close()


def ticket_env(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("SUBSTRATE_IMPORT_TICKET", TICKET)
    monkeypatch.setenv("SUBSTRATE_MCP_URL", url)


# ---------------------------------------------------------------------------
# Offer hook (--offer-check)
# ---------------------------------------------------------------------------

def connect(host: str = "claude") -> None:
    assert run("--host", host, "--record-connected").returncode == 0


def _offer(env_paths: dict, stdin: str | None = '{"session_id": "current-1"}',
           host: str = "claude") -> subprocess.CompletedProcess:
    return run("--host", host, "--offer-check", stdin=stdin)


def test_offer_emitted_when_undecided(env) -> None:
    connect()
    write_claude_session(env["claude"], "old-1")
    write_claude_session(env["claude"], "current-1")
    proc = _offer(env)
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    hook = out["hookSpecificOutput"]
    assert hook["hookEventName"] == "UserPromptSubmit"
    text = hook["additionalContext"]
    for phrase in ("Import all", "Let me pick", "Not now", "Connected to Substrate.",
                   "memory_import_ticket", "--preview", "--upload --all --background",
                   "--status --wait 60", "SUBSTRATE_IMPORT_TICKET", "SUBSTRATE_MCP_URL",
                   "--record-decision no", "never show the ticket",
                   "Do not stop and wait for the user"):
        assert phrase in text, phrase
    assert '--exclude-session "current-1"' in text
    assert str(SCRIPT) in text and str(env["data"]) in text
    assert TICKET not in text
    # The hook remembers the live session so uploads exclude it by default.
    state = json.loads((env["data"] / "import-offer.json").read_text())
    assert state["last_session"]["id"] == "current-1"
    assert "decision" not in state and state["connected_at"]


@pytest.mark.parametrize("decision", ["yes", "no", "picked", "none"])
def test_offer_silent_once_decided(env, decision: str) -> None:
    connect()
    write_claude_session(env["claude"], "old-1")
    assert run("--host", "claude", "--record-decision", decision).returncode == 0
    proc = _offer(env)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_offer_zero_sessions_records_none_silently(env) -> None:
    connect()
    write_claude_session(env["claude"], "current-1")  # only the live session exists
    proc = _offer(env)
    assert proc.returncode == 0 and proc.stdout == ""
    state = json.loads((env["data"] / "import-offer.json").read_text())
    assert state["decision"] == "none"
    # Nothing at all (fresh Cowork-like profile).
    shutil.rmtree(env["claude"] / "projects")
    (env["data"] / "import-offer.json").unlink()
    connect()
    proc = _offer(env)
    assert proc.returncode == 0 and proc.stdout == ""
    assert json.loads((env["data"] / "import-offer.json").read_text())["decision"] == "none"


def test_offer_ignores_subagent_transcripts(env) -> None:
    connect()
    write_claude_session(env["claude"], "current-1")
    sub = env["claude"] / "projects" / "proj" / "current-1" / "subagents" / "agent-a1.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text(json.dumps({"type": "user", "sessionId": "current-1",
                               "message": {"role": "user", "content": "x"}}) + "\n")
    proc = _offer(env)
    assert proc.stdout == ""


@pytest.mark.parametrize("marker", [b"{not json", b"[1, 2]", b"\x00\xff\xfe", b"",
                                    b'{"decision": "maybe"}', b'{"decision": 7}'])
def test_offer_corrupt_marker_never_fails(env, marker: bytes) -> None:
    write_claude_session(env["claude"], "old-1")
    env["data"].mkdir(parents=True, exist_ok=True)
    (env["data"] / "import-offer.json").write_bytes(marker)
    proc = _offer(env)
    assert proc.returncode == 0
    assert proc.stderr == ""
    # Unknown decision = undecided: the offer is emitted as valid JSON.
    assert "additionalContext" in json.loads(proc.stdout)["hookSpecificOutput"]


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"session_id": 5}',
                                   '{"session_id": "' + "x" * 600 + '"}', "\x00"])
def test_offer_bad_stdin_still_valid(env, stdin: str) -> None:
    write_claude_session(env["claude"], "old-1")
    proc = _offer(env, stdin=stdin)
    assert proc.returncode == 0 and proc.stderr == ""
    json.loads(proc.stdout)


def test_offer_marker_is_a_directory(env) -> None:
    write_claude_session(env["claude"], "old-1")
    (env["data"] / "import-offer.json").mkdir(parents=True)
    proc = _offer(env)
    assert proc.returncode == 0 and proc.stderr == ""
    json.loads(proc.stdout)


def test_offer_read_only_data_dir_still_offers(env, tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    write_claude_session(env["claude"], "old-1")
    env["data"].mkdir(parents=True)
    env["data"].chmod(0o500)
    try:
        proc = _offer(env)
    finally:
        env["data"].chmod(0o700)
    assert proc.returncode == 0
    assert "additionalContext" in json.loads(proc.stdout)["hookSpecificOutput"]


def test_offer_unwritable_bad_args_exit_zero(env) -> None:
    proc = run("--host", "claude", "--offer-check", "--bogus", stdin="{}")
    assert proc.returncode == 0
    proc = run("--host", "nonsense", "--offer-check", stdin="{}")
    assert proc.returncode == 0 and proc.stdout == ""


def test_offer_latency_200_transcripts(env) -> None:
    connect()
    for n in range(200):
        write_claude_session(env["claude"], f"s-{n:03d}", turns=3, project=f"p{n % 7}")
    run("--host", "claude", "--offer-check", stdin='{"session_id": "s-000"}')  # warm caches
    timings = []
    for _ in range(3):
        start = time.perf_counter()
        proc = _offer(env, stdin='{"session_id": "s-000"}')
        timings.append(time.perf_counter() - start)
        assert proc.returncode == 0 and json.loads(proc.stdout)
    assert min(timings) < 0.2, timings
    run("--host", "claude", "--record-decision", "no")
    start = time.perf_counter()
    proc = _offer(env, stdin='{"session_id": "s-000"}')
    assert proc.stdout == "" and time.perf_counter() - start < 0.2


def test_offer_codex_host(env) -> None:
    connect("codex")
    write_codex_session(env["codex"], "codex-old")
    proc = run("--host", "codex", "--offer-check", stdin='{"session_id": "codex-now"}')
    text = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "--host codex" in text and '"platform": "codex"' in text
    state = env["data"] / "import-offer.json"
    assert json.loads(state.read_text())["last_session"]["id"] == "codex-now"


def test_offer_codex_current_rollout_not_counted(env) -> None:
    connect("codex")
    write_codex_session(env["codex"], "codex-now")
    proc = run("--host", "codex", "--offer-check", stdin='{"session_id": "codex-now"}')
    assert proc.stdout == ""
    assert json.loads((env["data"] / "import-offer.json").read_text())["decision"] == "none"


def test_windows_quoting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync.os, "name", "nt")
    assert sync._quote("C:\\Users\\Pavel\\.claude\\plugins\\x.py") == '"C:/Users/Pavel/.claude/plugins/x.py"'


def test_default_data_dir_follows_host_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SUBSTRATE_DATA_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    assert sync._data_dir("claude") == str(tmp_path / "cc" / "substrate-memory")
    assert sync._data_dir("codex") == str(tmp_path / "cx" / "substrate-memory")
    assert sync._data_dir("claude", "/explicit") == "/explicit"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert sync._data_dir("claude") == os.path.join(os.path.expanduser("~"), ".claude",
                                                    "substrate-memory")


def test_setup_continues_until_connected(env) -> None:
    """Fresh install: after /reload-plugins any prompt carries setup forward."""
    write_claude_session(env["claude"], "old-1")
    proc = _offer(env)
    assert proc.returncode == 0
    text = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    for phrase in ("Substrate setup is not finished", "memory_search", "Connected to Substrate.",
                   "--record-connected", "authenticate", "Approve connection",
                   "--pause 15", "--preview", "Import all / Let me pick / Not now",
                   "Never ask for a token", "/reload-plugins"):
        assert phrase in text, phrase
    assert str(SCRIPT) in text and '--exclude-session "current-1"' in text
    # Still not connected on the next prompt: still carried forward.
    assert "setup is not finished" in _offer(env).stdout
    connect()
    after = json.loads(_offer(env).stdout)["hookSpecificOutput"]["additionalContext"]
    assert after.startswith("[substrate] Import offer pending")
    run("--host", "claude", "--record-decision", "no")
    assert _offer(env).stdout == ""


def test_setup_without_history_skips_offer_part(env) -> None:
    proc = _offer(env)  # no transcripts at all: decision none, but not connected
    text = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "setup is not finished" in text and "--preview" not in text
    state = json.loads((env["data"] / "import-offer.json").read_text())
    assert state["decision"] == "none" and "connected_at" not in state
    connect()
    assert _offer(env).stdout == ""


def test_decided_but_not_connected_still_sets_up(env) -> None:
    write_claude_session(env["claude"], "old-1")
    run("--host", "claude", "--record-decision", "no")
    text = json.loads(_offer(env).stdout)["hookSpecificOutput"]["additionalContext"]
    assert "setup is not finished" in text and "Import all" not in text


def test_record_connected_is_idempotent(env, capsys) -> None:
    code, rows = main_json(capsys, "--host", "claude", "--record-connected")
    assert rows == [{"connected": True}]
    first = json.loads((env["data"] / "import-offer.json").read_text())["connected_at"]
    main_json(capsys, "--host", "claude", "--record-connected")
    assert json.loads((env["data"] / "import-offer.json").read_text())["connected_at"] == first


def test_upload_records_connected(env, capsys, server, monkeypatch) -> None:
    write_claude_session(env["claude"], "a")
    ticket_env(monkeypatch, server.url)
    assert sync.main(["--host", "claude", "--upload", "--all"]) == 0
    assert json.loads((env["data"] / "import-offer.json").read_text())["connected_at"]


# ---------------------------------------------------------------------------
# --preview / --list / --record-decision
# ---------------------------------------------------------------------------

def test_preview_counts_range_and_exclusions(env, capsys) -> None:
    write_claude_session(env["claude"], "a", turns=3, day=2)
    write_claude_session(env["claude"], "b", turns=4, day=20)
    write_claude_session(env["claude"], "live", turns=5, day=40)
    code, rows = main_json(capsys, "--host", "claude", "--preview", "--exclude-session", "live")
    assert code == 0
    preview = rows[0]
    assert preview["sessions"] == 2 and preview["turns"] == 7
    assert preview["first_at"].startswith("2026-09-02") and preview["last_at"].startswith("2026-09-20")
    assert preview["excluded"] == ["live"] and preview["decision"] is None
    assert preview["bytes_estimate"] > 0


def test_preview_human_summary_on_stderr(env) -> None:
    write_claude_session(env["claude"], "a", turns=3, day=2)
    proc = run("--host", "claude", "--preview")
    assert "Found 1 past conversation (3 turns) from 2 Sep 2026 to 2 Sep 2026." in proc.stderr
    assert json.loads(proc.stdout)["sessions"] == 1


def test_preview_excludes_session_seen_by_hook(env) -> None:
    write_claude_session(env["claude"], "a")
    write_claude_session(env["claude"], "live")
    _offer(env, stdin='{"session_id": "live"}')
    proc = run("--host", "claude", "--preview")
    preview = json.loads(proc.stdout)
    assert preview["sessions"] == 1 and preview["excluded"] == ["live"]


def test_preview_stale_hook_session_not_excluded(env) -> None:
    write_claude_session(env["claude"], "a")
    write_claude_session(env["claude"], "old-live")
    env["data"].mkdir(parents=True)
    (env["data"] / "import-offer.json").write_text(json.dumps(
        {"last_session": {"id": "old-live", "at": time.time() - 13 * 3600}}))
    assert json.loads(run("--host", "claude", "--preview").stdout)["sessions"] == 2


def test_recent_transcript_is_taken_as_current_when_unknown(env) -> None:
    write_claude_session(env["claude"], "old")
    live = write_claude_session(env["claude"], "probably-live")
    _age(live, 30)
    preview = json.loads(run("--host", "claude", "--preview").stdout)
    assert preview["sessions"] == 1 and preview["excluded"] == ["probably-live"]
    # A known current session wins over the guess.
    preview = json.loads(run("--host", "claude", "--preview", "--exclude-session", "x").stdout)
    assert preview["sessions"] == 2 and preview["excluded"] == ["x"]


def test_preview_zero_and_decision_reported(env, capsys) -> None:
    code, rows = main_json(capsys, "--host", "claude", "--record-decision", "no")
    assert rows == [{"decision": "no"}]
    code, rows = main_json(capsys, "--host", "claude", "--preview")
    assert rows[0]["sessions"] == 0 and rows[0]["decision"] == "no"
    assert rows[0]["first_at"] is None


def test_list_has_titles_dates_and_skips_meta(env, capsys) -> None:
    path = write_claude_session(env["claude"], "a", prompt="Plan the launch {n} api_key: s3cr3tvalue")
    lines = path.read_text().splitlines()
    meta = {"type": "user", "sessionId": "a", "timestamp": _ts(1),
            "message": {"role": "user", "content": "<local-command-caveat>Caveat</local-command-caveat>"}}
    path.write_text(json.dumps(meta) + "\n" + "\n".join(lines) + "\n")
    write_claude_session(env["claude"], "live")
    code, rows = main_json(capsys, "--host", "claude", "--list", "--exclude-session", "live")
    assert [r["session_id"] for r in rows] == ["a"]
    assert rows[0]["title"].startswith("Plan the launch 0")
    assert "s3cr3tvalue" not in rows[0]["title"]
    assert rows[0]["first_at"] and rows[0]["last_at"]


def test_subagent_transcripts_not_listed(env, capsys) -> None:
    write_claude_session(env["claude"], "parent")
    sub_home = env["claude"] / "projects" / "proj" / "parent" / "subagents"
    sub_home.mkdir(parents=True)
    (sub_home / "agent-x.jsonl").write_text(json.dumps(
        {"type": "user", "sessionId": "parent", "timestamp": _ts(1),
         "message": {"role": "user", "content": "subagent prompt"}}) + "\n")
    code, rows = main_json(capsys, "--host", "claude", "--list")
    assert [r["session_id"] for r in rows] == ["parent"]


def _subagent_lines(parent: str, text: str) -> str:
    rows = [{"type": "user", "sessionId": parent, "isSidechain": True, "timestamp": _ts(1),
             "message": {"role": "user", "content": text}},
            {"type": "assistant", "sessionId": parent, "isSidechain": True, "timestamp": _ts(1),
             "message": {"role": "assistant", "content": [{"type": "text", "text": "sub answer"}]}}]
    return "".join(json.dumps(r) + "\n" for r in rows)


def test_flat_agent_transcripts_skipped(env, capsys, server, monkeypatch) -> None:
    """Older layout: agent-*.jsonl next to the parent, same sessionId."""
    write_claude_session(env["claude"], "parent", turns=2)
    folder = env["claude"] / "projects" / "proj"
    for n in range(3):
        path = folder / f"agent-{n:04x}.jsonl"
        path.write_text(_subagent_lines("parent", f"subagent task {n}"))
        _age(path)
    code, rows = main_json(capsys, "--host", "claude", "--list")
    assert [(r["session_id"], r["turns"]) for r in rows] == [("parent", 2)]
    assert rows[0]["path"].endswith("parent.jsonl")
    ticket_env(monkeypatch, server.url)
    assert sync.main(["--host", "claude", "--upload", "--all"]) == 0
    sent = server.sent_items()
    assert [i["kind"] for i in sent] == ["capture_turn", "capture_turn", "capture_session"]
    assert "subagent task" not in json.dumps(sent)
    assert [i["offset"]["start"] for i in sent] == [0, 2, 4]


def test_sidechain_records_and_files_skipped(env, capsys) -> None:
    """Any file name: sidechain-only transcripts vanish; sidechain lines in a
    main transcript are ignored (no offset collisions with the parent)."""
    write_claude_session(env["claude"], "parent", turns=2)
    folder = env["claude"] / "projects" / "proj"
    copy = folder / "flattened-copy.jsonl"
    copy.write_text(_subagent_lines("parent", "flattened subagent"))
    _age(copy)
    main_file = folder / "parent.jsonl"
    main_file.write_text(_subagent_lines("parent", "inline sidechain") + main_file.read_text())
    _age(main_file)
    code, rows = main_json(capsys, "--host", "claude", "--list")
    assert [(r["session_id"], r["turns"]) for r in rows] == [("parent", 2)]
    entry = sync._catalogue("claude")[0]
    assert "sidechain" not in json.dumps(entry["turns"])
    assert entry["path"].endswith("parent.jsonl")


def test_offer_hook_ignores_flat_agent_files(env) -> None:
    connect()
    write_claude_session(env["claude"], "current-1")
    path = env["claude"] / "projects" / "proj" / "agent-abc.jsonl"
    path.write_text(_subagent_lines("current-1", "x"))
    assert _offer(env).stdout == ""


def test_status_names_uploading_session(env, capsys, server, monkeypatch) -> None:
    write_claude_session(env["claude"], "a")
    ticket_env(monkeypatch, server.url)
    sync.main(["--host", "claude", "--upload", "--all"])
    status = json.loads((env["data"] / "import-status.json").read_text())
    assert status["uploading_session"] == "a" and "current_session" not in status


def test_windows_transcript_paths(env, capsys) -> None:
    write_claude_session(env["claude"], "win-1", project="C--Users-Pavel-Projects-app",
                         cwd="C:\\Users\\Pavel\\Projects\\app",
                         prompt="Open C:\\Users\\Pavel\\notes.txt please {n}")
    code, rows = main_json(capsys, "--host", "claude", "--list")
    assert rows[0]["session_id"] == "win-1"
    items = sync._build_envelopes("win-1", sync._catalogue("claude")[0]["turns"],
                                  "history_replay", 0)
    assert "C:\\Users\\Pavel\\notes.txt" in items[0]["payload"]["messages"][0]["content"]


def test_claude_config_dir_honoured(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                    capsys) -> None:
    other = tmp_path / "other-config"
    write_claude_session(other, "elsewhere")
    write_claude_session(env["claude"], "here")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    code, rows = main_json(capsys, "--host", "claude", "--list")
    assert [r["session_id"] for r in rows] == ["elsewhere"]


def test_pause_is_bounded(env, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    waited = []
    monkeypatch.setattr(sync, "_sleep", waited.append)
    code, rows = main_json(capsys, "--host", "claude", "--pause", "500")
    assert code == 0 and waited == [60.0] and rows == [{"paused": 60.0}]


# ---------------------------------------------------------------------------
# Item fitting and batch limits
# ---------------------------------------------------------------------------

def _validate(items: list) -> None:
    for item in items:
        envelope = dict(item, schema_version=3, contract_version=1, event_id=str(uuid.uuid4()))
        if not envelope.get("batch_id"):
            envelope["batch_id"] = ""
        c.validate_envelope(envelope)
        assert len(c.canonical_bytes(item)) <= sync.MAX_ITEM_BYTES


def test_small_items_unchanged_by_fitting(env) -> None:
    write_claude_session(env["claude"], "a", tool_calls=3)
    entry = sync._catalogue("claude")[0]
    for item in sync._build_envelopes("a", entry["turns"], "history_replay", 0):
        assert sync._fit_item(item) == [item]


def test_huge_turn_fits_contract(env) -> None:
    # 150 tool calls with 8 KiB results: > 64 calls and > 1 MB as one item.
    write_claude_session(env["claude"], "big", turns=1, tool_calls=150, result_bytes=9000)
    entry = sync._catalogue("claude")[0]
    raw = sync._build_envelopes("big", entry["turns"], "history_replay", 0)
    assert len(raw) == 1 and not sync._item_fits(raw[0])
    fitted = [part for item in raw for part in sync._fit_item(item)]
    _validate(fitted)
    # Offsets stay contiguous and cover the original turn exactly.
    assert fitted[0]["offset"]["start"] == raw[0]["offset"]["start"]
    assert fitted[-1]["offset"]["end"] == raw[0]["offset"]["end"]
    for left, right in zip(fitted, fitted[1:]):
        assert left["offset"]["end"] == right["offset"]["start"]


def test_split_messages_when_shrinking_is_not_enough(env) -> None:
    # 4000 tool results: even empty excerpts exceed one event.
    write_claude_session(env["claude"], "wide", turns=1, tool_calls=4000, result_bytes=1)
    entry = sync._catalogue("claude")[0]
    raw = sync._build_envelopes("wide", entry["turns"], "history_replay", 0)
    fitted = sync._fit_item(raw[0])
    assert len(fitted) > 1
    _validate(fitted)
    assert len({part["payload"]["turn_id"] for part in fitted}) == len(fitted)


def test_split_batches_respects_64_items_and_240k() -> None:
    small = [{"n": i, "pad": "x" * 10} for i in range(200)]
    batches = sync._split_batches(small)
    assert [len(b) for b in batches] == [64, 64, 64, 8]
    big = [{"n": i, "pad": "y" * 100_000} for i in range(7)]
    batches = sync._split_batches(big)
    assert all(len(c.canonical_bytes({"items": b})) <= 240 * 1024 for b in batches)
    assert [len(b) for b in batches] == [2, 2, 2, 1]


# ---------------------------------------------------------------------------
# --upload against the fake MCP server
# ---------------------------------------------------------------------------

def upload(capsys, *args: str) -> tuple[int, str]:
    code = sync.main(["--host", "claude", "--upload", *args])
    return code, capsys.readouterr().out.strip()


def test_upload_requires_ticket_from_env(env, capsys) -> None:
    write_claude_session(env["claude"], "a")
    code, out = upload(capsys, "--all")
    assert code == 2 and json.loads(out)["error"] == "missing_ticket"


def test_upload_refuses_plain_http_remote(env, capsys, monkeypatch) -> None:
    ticket_env(monkeypatch, "http://example.com/mcp")
    code, out = upload(capsys, "--all")
    assert code == 2 and json.loads(out)["error"] == "invalid_url"


def test_upload_needs_selection(env, capsys, server, monkeypatch) -> None:
    ticket_env(monkeypatch, server.url)
    code, out = upload(capsys)
    assert code == 2 and json.loads(out)["error"] == "nothing_selected"


@pytest.mark.parametrize("sse,stateful", [(False, False), (True, False), (True, True)])
def test_upload_all_handshake_and_summary(env, capsys, monkeypatch, sse, stateful) -> None:
    fake = FakeMcp(sse=sse, stateful=stateful)
    try:
        write_claude_session(env["claude"], "a", turns=3, day=1)
        write_claude_session(env["claude"], "b", turns=2, day=2)
        ticket_env(monkeypatch, fake.url)
        code, out = upload(capsys, "--all")
        assert code == 0, out
        assert out.splitlines()[-1] == "Imported 2 sessions (5 turns): 5 stored, 0 duplicate, 0 rejected."
        methods = [m.get("method") for m in fake.calls]
        assert methods[0] == "initialize"
        if stateful:
            assert methods[1] == "notifications/initialized"
        assert all(m == "tools/call" for m in methods[(2 if stateful else 1):])
        for headers in fake.headers:
            lowered = {k.lower(): v for k, v in headers.items()}
            assert lowered["authorization"] == "Bearer " + TICKET
        batch_ids = {m["params"]["arguments"]["batch_id"] for m in fake.import_requests()}
        assert len(batch_ids) == 1
        (batch_id,) = batch_ids
        assert all(item["batch_id"] == batch_id for item in fake.sent_items())
        assert all(item["capture_origin"] == "history_replay" for item in fake.sent_items())
        _validate(fake.sent_items())
        status = json.loads((env["data"] / "import-status.json").read_text())
        assert status["state"] == "done" and status["sessions_done"] == 2
        assert TICKET not in (env["data"] / "import-status.json").read_text()
        offer = json.loads((env["data"] / "import-offer.json").read_text())
        assert offer["decision"] == "yes"
    finally:
        fake.close()


def test_upload_seals_each_session_once_after_its_turns(env, capsys, server, monkeypatch) -> None:
    write_claude_session(env["claude"], "a", turns=3, day=1)
    write_claude_session(env["claude"], "b", turns=70, day=2)   # two batches
    ticket_env(monkeypatch, server.url)
    code, out = upload(capsys, "--all")
    assert code == 0
    assert out == "Imported 2 sessions (73 turns): 73 stored, 0 duplicate, 0 rejected."
    sent = server.sent_items()
    _validate(sent)
    for session in ("a", "b"):
        mine = [i for i in sent if i["session_id"] == session]
        seals = [i for i in mine if i["kind"] == "capture_session"]
        assert len(seals) == 1 and mine[-1] is seals[0], session
        last_end = max(i["offset"]["end"] for i in mine if i["kind"] == "capture_turn")
        seal = seals[0]
        assert seal["offset"] == {"start": last_end, "end": last_end}
        assert seal["capture_origin"] == "history_replay"
        assert seal["payload"] == {"boundary": "end", "session_complete": True,
                                   "message_high_water": last_end, "platform": "claude",
                                   "chat_type": "direct"}
    status = json.loads((env["data"] / "import-status.json").read_text())
    assert status["sessions_sealed"] == 2 and status["seal_failed"] == 0
    # Rerun (forced past the local skip): the seals dedupe on the server too.
    code, out = upload(capsys, "--all", "--force")
    assert out.endswith("0 stored, 73 duplicate, 0 rejected.")
    assert len(server.store) == 73 + 2


def test_rejected_seal_does_not_break_import(env, capsys, server, monkeypatch) -> None:
    write_claude_session(env["claude"], "a", turns=2)
    original = server.handle

    def refuse_seals(raw, headers):
        if b'"capture_session"' in raw:
            message = json.loads(raw)
            return 200, FakeMcp._tool_error(message, "invalid_request"), {}
        return original(raw, headers)
    server.handle = refuse_seals
    ticket_env(monkeypatch, server.url)
    code, out = upload(capsys, "--all")
    assert code == 0 and out.endswith("2 stored, 0 duplicate, 0 rejected.")
    status = json.loads((env["data"] / "import-status.json").read_text())
    assert status["seal_failed"] == 1


def test_catchup_session_output_never_seals(env, capsys) -> None:
    write_claude_session(env["claude"], "live", turns=3)
    for origin in ("catchup", "history_replay"):
        code = sync.main(["--host", "claude", "--session", "live", "--origin", origin])
        out = capsys.readouterr().out
        kinds = {i["kind"] for line in out.splitlines() for i in json.loads(line)["items"]}
        assert code == 0 and kinds == {"capture_turn"}


def test_upload_wrong_server_identity_is_fatal(env, capsys, monkeypatch) -> None:
    fake = FakeMcp(server_name="someone-else")
    try:
        write_claude_session(env["claude"], "a")
        ticket_env(monkeypatch, fake.url)
        code, out = upload(capsys, "--all")
        assert code == 1 and "not_substrate" in out
        assert fake.import_requests() == []
    finally:
        fake.close()


def test_upload_excludes_current_session(env, capsys, server, monkeypatch) -> None:
    write_claude_session(env["claude"], "old")
    write_claude_session(env["claude"], "live-explicit")
    write_claude_session(env["claude"], "live-hook")
    _offer(env, stdin='{"session_id": "live-hook"}')
    ticket_env(monkeypatch, server.url)
    code, out = upload(capsys, "--all", "--exclude-session", "live-explicit")
    assert code == 0
    assert {item["session_id"] for item in server.sent_items()} == {"old"}


def test_upload_selected_sessions_records_picked(env, capsys, server, monkeypatch) -> None:
    for name in ("a", "b", "c"):
        write_claude_session(env["claude"], name)
    ticket_env(monkeypatch, server.url)
    code, out = upload(capsys, "--session", "a", "--session", "c")
    assert code == 0
    assert {item["session_id"] for item in server.sent_items()} == {"a", "c"}
    assert json.loads((env["data"] / "import-offer.json").read_text())["decision"] == "picked"


def test_upload_batches_many_and_large_turns(env, capsys, server, monkeypatch) -> None:
    write_claude_session(env["claude"], "many", turns=150)
    write_claude_session(env["claude"], "heavy", turns=6, tool_calls=40, result_bytes=8000, day=3)
    ticket_env(monkeypatch, server.url)
    code, out = upload(capsys, "--all")
    assert code == 0, out
    requests = server.import_requests()
    sizes = [len(m["params"]["arguments"]["items"]) for m in requests]
    assert max(sizes) == 64 and len(requests) >= 4
    for message in requests:
        batch = message["params"]["arguments"]["items"]
        assert len(c.canonical_bytes({"items": batch})) <= 240 * 1024
    assert all(len(body) <= 262144 for body in server.bodies)
    assert "156 turns" in out and "rejected" in out


def test_upload_retries_429_and_5xx(env, capsys, server, monkeypatch) -> None:
    write_claude_session(env["claude"], "a")
    waits = []
    monkeypatch.setattr(sync, "_sleep", waits.append)
    server.http_queue = [429, 503, 429]
    server.tool_error_queue = ["rate_limited"]
    ticket_env(monkeypatch, server.url)
    code, out = upload(capsys, "--all")
    assert code == 0, out
    assert len(waits) == 4
    assert out.endswith("2 stored, 0 duplicate, 0 rejected.")


def test_upload_gives_up_after_bounded_retries(env, capsys, server, monkeypatch) -> None:
    write_claude_session(env["claude"], "a")
    server.http_queue = [503] * 50
    ticket_env(monkeypatch, server.url)
    code, out = upload(capsys, "--all")
    assert code == 1 and "try again later" in out
    status = json.loads((env["data"] / "import-status.json").read_text())
    assert status["state"] == "failed"


def test_upload_backoff_uses_retry_after_and_cap(monkeypatch) -> None:
    monkeypatch.setenv("SUBSTRATE_SYNC_BACKOFF_MAX", "60")
    assert sync._backoff(1, None) == 2 and sync._backoff(3, None) == 8
    assert sync._backoff(10, None) == 60 and sync._backoff(1, 5.0) == 5.0


def test_upload_401_stops_cleanly_and_resumes(env, capsys, monkeypatch) -> None:
    fake = FakeMcp()
    try:
        write_claude_session(env["claude"], "s1", turns=70, day=1)   # 2 batches
        write_claude_session(env["claude"], "s2", turns=70, day=2)   # 2 batches
        write_claude_session(env["claude"], "s3", turns=3, day=3)
        fake.auth_fail_after = 3  # s1 complete, s2 half sent, then the ticket expires
        ticket_env(monkeypatch, fake.url)
        code, out = upload(capsys, "--all")
        assert code == 3
        assert "ticket expired or was revoked after 1 of 3" in out
        assert "Ask me to continue the import" in out
        status = json.loads((env["data"] / "import-status.json").read_text())
        assert status["state"] == "stopped"
        stored_first = len(fake.store)
        assert stored_first == 70 + 64 + 1  # s1 turns + its seal, half of s2
        # The agent mints a fresh ticket and runs the same command again.
        fake.auth_fail_after = None
        code, out = upload(capsys, "--all")
        assert code == 0, out
        # s1 skipped locally (counted as duplicate); s2's first batch is resent
        # and the server dedupes it; s2's rest and s3 are new.
        assert out == ("Imported 3 sessions (143 turns): 9 stored, 134 duplicate, 0 rejected.")
        assert len(fake.store) == 143 + 3  # plus one seal per session
    finally:
        fake.close()


def test_upload_tool_level_unauthorized_also_stops(env, capsys, server, monkeypatch) -> None:
    write_claude_session(env["claude"], "a")
    server.tool_error_queue = ["unauthorized"]
    ticket_env(monkeypatch, server.url)
    code, out = upload(capsys, "--all")
    assert code == 3 and "Ask me to continue" in out


def test_rerun_is_idempotent(env, capsys, server, monkeypatch) -> None:
    write_claude_session(env["claude"], "a", turns=4)
    ticket_env(monkeypatch, server.url)
    assert upload(capsys, "--all")[0] == 0
    first_calls = len(server.import_requests())
    code, out = upload(capsys, "--all")
    assert out == "Imported 1 session (4 turns): 0 stored, 4 duplicate, 0 rejected."
    assert len(server.import_requests()) == first_calls  # skipped locally
    code, out = upload(capsys, "--all", "--force")
    assert out == "Imported 1 session (4 turns): 0 stored, 4 duplicate, 0 rejected."
    assert len(server.import_requests()) == first_calls + 1  # server-side dedupe


def test_changed_session_is_resent(env, capsys, server, monkeypatch) -> None:
    write_claude_session(env["claude"], "a", turns=2)
    ticket_env(monkeypatch, server.url)
    upload(capsys, "--all")
    write_claude_session(env["claude"], "a", turns=3)  # the conversation continued
    code, out = upload(capsys, "--all")
    assert out.endswith("1 stored, 2 duplicate, 0 rejected.")


def test_bad_item_is_isolated_by_bisection(env, capsys, server, monkeypatch) -> None:
    path = write_claude_session(env["claude"], "a", turns=10)
    text = path.read_text().replace("Question number 6", "Question POISON 6")
    path.write_text(text)
    _age(path)
    ticket_env(monkeypatch, server.url)
    code, out = upload(capsys, "--all")
    assert code == 0
    assert out.endswith("9 stored, 0 duplicate, 1 rejected.")


def test_413_is_bisected(env, capsys, server, monkeypatch) -> None:
    write_claude_session(env["claude"], "a", turns=4)
    server.http_queue = [413]
    ticket_env(monkeypatch, server.url)
    code, out = upload(capsys, "--all")
    assert code == 0 and out.endswith("4 stored, 0 duplicate, 0 rejected.")


def test_redaction_applied_on_upload(env, capsys, server, monkeypatch) -> None:
    secrets = [case["in"] for case in REDACTION["text"] if case["in"] != case["out"]]
    home = env["claude"] / "projects" / "proj"
    home.mkdir(parents=True)
    lines = []
    for n, secret in enumerate(secrets):
        lines.append({"type": "user", "sessionId": "red", "timestamp": _ts(1, n),
                      "message": {"role": "user", "content": f"note {n}: {secret}"}})
        lines.append({"type": "assistant", "sessionId": "red", "timestamp": _ts(1, n),
                      "message": {"role": "assistant", "content": [
                          {"type": "text", "text": f"echo {secret}"},
                          {"type": "tool_use", "id": f"t{n}", "name": "Bash",
                           "input": {"command": "x", "api_key": "k-" + str(n), "nested": {"password": "pw"}}}]}})
    (home / "red.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    shutil.copy(FIX / "claude-projects" / "proj-test" / "test-claude-session-01.jsonl",
                home / "test-claude-session-01.jsonl")
    _age(home / "red.jsonl")
    _age(home / "test-claude-session-01.jsonl")
    ticket_env(monkeypatch, server.url)
    code, out = upload(capsys, "--all")
    assert code == 0
    wire = b"".join(server.bodies).decode("utf-8")
    for leaked in ("abc123def456", "sk_sub_FAKEFAKE00", "xyz-987", "hunter2",
                   "sk_live_1234567890abcdef", "should-be-redacted", '"k-0"', '"pw"'):
        assert leaked not in wire, leaked
    assert "[REDACTED]" in wire
    for case in REDACTION["text"]:
        expected = case["out"]
        assert any(expected in item["payload"]["messages"][0]["content"]
                   for item in server.sent_items()
                   if item["session_id"] == "red" and item["kind"] == "capture_turn") or \
            case["in"] == case["out"]


def test_codex_upload(env, capsys, server, monkeypatch) -> None:
    write_codex_session(env["codex"], "cx-1", turns=3)
    ticket_env(monkeypatch, server.url)
    code = sync.main(["--host", "codex", "--upload", "--all"])
    out = capsys.readouterr().out.strip()
    assert code == 0 and out == "Imported 1 session (3 turns): 3 stored, 0 duplicate, 0 rejected."
    assert {i["session_id"] for i in server.sent_items()} == {"cx-1"}


# ---------------------------------------------------------------------------
# --background / --status (real subprocesses)
# ---------------------------------------------------------------------------

def _poll_until_done(timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = run("--host", "claude", "--status", "--wait", "5")
        status = json.loads(proc.stdout)
        if status["done"]:
            return status
    raise AssertionError("import did not finish")


def test_background_upload_and_status(env, server) -> None:
    for n in range(5):
        write_claude_session(env["claude"], f"bg-{n}", turns=20, day=n + 1)
    write_claude_session(env["claude"], "live")
    proc = run("--host", "claude", "--upload", "--all", "--background",
               "--exclude-session", "live",
               extra_env={"SUBSTRATE_IMPORT_TICKET": TICKET, "SUBSTRATE_MCP_URL": server.url})
    assert proc.returncode == 0, proc.stderr
    started = json.loads(proc.stdout)
    assert started["started"] is True
    assert started["status_file"] == str(env["data"] / "import-status.json")
    assert TICKET not in proc.stdout
    status = _poll_until_done()
    assert status["state"] == "done"
    assert status["message"] == "Imported 5 sessions (100 turns): 100 stored, 0 duplicate, 0 rejected."
    assert {i["session_id"] for i in server.sent_items()} == {f"bg-{n}" for n in range(5)}
    for name in os.listdir(env["data"]):
        assert TICKET not in (env["data"] / name).read_text(errors="replace"), name
    assert json.loads((env["data"] / "import-offer.json").read_text())["decision"] == "yes"


def test_background_refuses_second_concurrent_run(env, server) -> None:
    write_claude_session(env["claude"], "a")
    env["data"].mkdir(parents=True)
    (env["data"] / "import-status.json").write_text(json.dumps(
        {"state": "running", "pid": os.getpid(), "updated_ts": time.time()}))
    proc = run("--host", "claude", "--upload", "--all", "--background",
               extra_env={"SUBSTRATE_IMPORT_TICKET": TICKET, "SUBSTRATE_MCP_URL": server.url})
    assert proc.returncode == 4 and json.loads(proc.stdout)["error"] == "already_running"


def test_status_none_and_dead_process(env) -> None:
    status = json.loads(run("--host", "claude", "--status").stdout)
    assert status["state"] == "none" and status["done"] is True
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    env["data"].mkdir(parents=True)
    (env["data"] / "import-status.json").write_text(json.dumps(
        {"state": "running", "pid": dead.pid, "updated_ts": time.time(),
         "sessions_total": 4, "sessions_done": 1}))
    status = json.loads(run("--host", "claude", "--status").stdout)
    assert status["state"] == "stopped" and status["done"] is True
    assert "Ask me to continue the import" in status["message"]


def test_status_running_message(env) -> None:
    env["data"].mkdir(parents=True)
    (env["data"] / "import-status.json").write_text(json.dumps(
        {"state": "running", "pid": os.getpid(), "updated_ts": time.time(),
         "sessions_total": 40, "sessions_done": 12, "stored": 300, "duplicate": 2}))
    start = time.time()
    status = json.loads(run("--host", "claude", "--status", "--wait", "1").stdout)
    assert time.time() - start < 10
    assert status["done"] is False
    assert status["message"] == ("Importing past conversations: 12 of 40 done "
                                 "(300 stored, 2 already there).")


# ---------------------------------------------------------------------------
# Hook wiring in both packages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("plug,host,root", [(CLAUDE_PLUG, "claude", "${CLAUDE_PLUGIN_ROOT}"),
                                            (CODEX_PLUG, "codex", "$R")])
def test_offer_command_hook_wired(plug: Path, host: str, root: str) -> None:
    hooks = json.loads((plug / "hooks" / "hooks.json").read_text())["hooks"]
    commands = [h for group in hooks["UserPromptSubmit"] for h in group["hooks"]
                if h["type"] == "command"]
    assert len(commands) == 1
    command = commands[0]["command"]
    assert commands[0]["timeout"] <= 5
    assert f'{root}/scripts/substrate_sync.py" --host {host} --offer-check' in command
    assert command.rstrip().endswith("|| true")
    assert "python3" in command and "|| python " in command
    mcp = [h for group in hooks["UserPromptSubmit"] for h in group["hooks"]
           if h["type"] == "mcp_tool"]
    assert [h["tool"] for h in mcp] == ["memory_turn_context"]


def test_hook_command_runs_under_sh(env) -> None:
    hooks = json.loads((CLAUDE_PLUG / "hooks" / "hooks.json").read_text())["hooks"]
    command = [h for g in hooks["UserPromptSubmit"] for h in g["hooks"]
               if h["type"] == "command"][0]["command"]
    command = command.replace("${CLAUDE_PLUGIN_ROOT}", str(CLAUDE_PLUG))
    write_claude_session(env["claude"], "old")
    proc = subprocess.run(["/bin/sh", "-c", command], input='{"session_id": "now"}',
                          capture_output=True, text=True, timeout=30, env=dict(os.environ))
    assert proc.returncode == 0
    assert "additionalContext" in json.loads(proc.stdout)["hookSpecificOutput"]
    # Without any python on PATH the hook still exits 0 silently.
    proc = subprocess.run(["/bin/sh", "-c", command], input="{}", capture_output=True, text=True,
                          timeout=30, env={"PATH": "/nonexistent"})
    assert proc.returncode == 0 and proc.stdout == ""


def test_codex_hook_command_finds_plugin_root(env, tmp_path: Path) -> None:
    hooks = json.loads((CODEX_PLUG / "hooks" / "hooks.json").read_text())["hooks"]
    command = [h for g in hooks["UserPromptSubmit"] for h in g["hooks"]
               if h["type"] == "command"][0]["command"]
    write_codex_session(env["codex"], "old")
    for variable in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        environment = dict(os.environ)
        environment[variable] = str(CODEX_PLUG)
        proc = subprocess.run(["/bin/sh", "-c", command], input='{"session_id": "now"}',
                              capture_output=True, text=True, timeout=30, env=environment,
                              cwd=str(tmp_path))
        assert "additionalContext" in json.loads(proc.stdout)["hookSpecificOutput"], variable
    # No variable at all: Codex runs plugin hooks from the plugin root.
    proc = subprocess.run(["/bin/sh", "-c", command], input='{"session_id": "now"}',
                          capture_output=True, text=True, timeout=30, env=dict(os.environ),
                          cwd=str(CODEX_PLUG))
    assert "additionalContext" in json.loads(proc.stdout)["hookSpecificOutput"]
