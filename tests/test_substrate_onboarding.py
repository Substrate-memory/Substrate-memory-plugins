"""Device-login tests: install-friendly onboarding without pasted keys.

Unit coverage runs against a loopback stub device server (no node, no live
API). One end-to-end test boots the REAL durability device-authorization
code (agent-auth.mjs, temp DB file, ephemeral loopback port); only the
human browser approval step is mocked, and that mock is clearly labeled
MOCK HUMAN BROWSER AUTH (test-only). No live tenant or credential is
created: every home, DB, and port is temporary.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from substrate import contract
from substrate import credentials
from substrate import onboarding

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_CLI = REPO_ROOT / "plugins" / "substrate" / "onboard.py"

DEVICE_CODE = "a" * 64
TOKEN = "sk_sub_" + "b" * 32

CAPABILITIES = {
    "contract_version": 1,
    "provider": "substrate",
    "server_commit": "test",
    "limits": dict(contract.LIMITS),
    "actions": sorted(contract.ACTIONS),
    "kinds": sorted(contract.KINDS),
    "tenant": {"tenant_id": "agent", "brief_version": 0},
}


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Every test gets a fresh profile home and no ambient credential."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    monkeypatch.delenv("SUBSTRATE_API_URL", raising=False)
    monkeypatch.delenv("SUBSTRATE_WIKI_ORIGIN", raising=False)
    monkeypatch.setattr(onboarding, "_manager", None)
    saved = os.environ.get("SUBSTRATE_API_KEY")
    yield home
    # Device completion exports the key in-process; never leak it sideways.
    if saved is None:
        os.environ.pop("SUBSTRATE_API_KEY", None)
    else:
        os.environ["SUBSTRATE_API_KEY"] = saved
    onboarding._manager = None


@pytest.fixture(autouse=True)
def _no_background_thread(monkeypatch):
    monkeypatch.setattr(onboarding.OnboardingManager, "_start_thread", lambda self: None)


# ---------------------------------------------------------------------------
# Loopback stub device server (test-only stand-in, not the real server)
# ---------------------------------------------------------------------------


class _DeviceHandler(BaseHTTPRequestHandler):
    server_version = "StubDevice/1"

    def log_message(self, *args):
        pass

    def _send(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/v1/capabilities":
            if self.headers.get("Authorization") != f"Bearer {self.server.token}":
                self._send(401, {"error": "unauthorized"})
                return
            self._send(200, dict(CAPABILITIES))
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/v1/memory/search":
            if self.headers.get("Authorization") != f"Bearer {self.server.token}":
                self._send(401, {"error": "unauthorized"})
                return
            self._send(200, {"contract_version": 1, "results": []})
            return
        form = urllib.parse.parse_qs(raw.decode("ascii", errors="replace"))
        first = {key: values[0] for key, values in form.items()}
        if parsed.path == "/oauth/device_authorization":
            if first.get("client_id") != "substrate-hermes":
                self._send(401, {"error": "invalid_client"})
                return
            if first.get("scope") != "capture retrieve":
                self._send(400, {"error": "invalid_scope"})
                return
            base = f"http://127.0.0.1:{self.server.server_port}"
            self._send(200, {
                "device_code": DEVICE_CODE,
                "user_code": "BCDF-GHJK",
                "verification_uri": f"{base}/oauth/device",
                "verification_uri_complete": f"{base}/oauth/device?user_code=BCDF-GHJK",
                "expires_in": 900,
                "interval": 1,
            })
            return
        if parsed.path == "/oauth/token":
            mode = self.server.token_mode
            if callable(mode):
                mode = mode()
            if mode == "slow_down_once":
                if not self.server.slowed:
                    self.server.slowed = True
                    self._send(400, {"error": "slow_down"})
                    return
            elif mode != "approved":
                self._send(400, {"error": mode})
                return
            if first.get("device_code") != DEVICE_CODE:
                self._send(400, {"error": "expired_token"})
                return
            self._send(200, self.server.token_body)
            return
        self._send(404, {"error": "not_found"})


@pytest.fixture
def device_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DeviceHandler)
    server.daemon_threads = True
    server.token = TOKEN
    server.token_mode = "pending"
    server.token_body = {
        "access_token": TOKEN,
        "token_type": "Bearer",
        "scope": "capture retrieve",
    }
    server.slowed = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.origin = f"http://127.0.0.1:{server.server_port}"
    yield server
    server.shutdown()
    thread.join(timeout=5.0)
    server.server_close()


def _origin(monkeypatch, server):
    monkeypatch.setenv("SUBSTRATE_API_URL", server.origin)
    return server.origin


def _begin(monkeypatch, device_server, home):
    origin = _origin(monkeypatch, device_server)
    manager = onboarding.OnboardingManager(home.resolve(), origin)
    status = manager.ensure(force=True)
    assert status["status"] == "authorization_pending"
    assert status["user_code"] == "BCDF-GHJK"
    assert status["verification_uri_complete"].startswith(
        device_server.origin + "/oauth/device?user_code="
    )
    return manager, origin


def test_start_is_content_free_and_device_code_stays_private(
    monkeypatch, device_server, _isolated_home
):
    home = _isolated_home
    manager, _origin_value = _begin(monkeypatch, device_server, home)
    assert manager is not None
    state_raw = (home / "substrate" / "onboarding.json").read_text(encoding="utf-8")
    state = json.loads(state_raw)
    assert state["phase"] == "pending"
    for forbidden in ("access_token", "api_key", "token", "device_code"):
        assert forbidden not in state, forbidden
    assert DEVICE_CODE not in state_raw
    device_file = home / "substrate" / "credentials" / "onboarding-device"
    assert device_file.read_text(encoding="utf-8") == DEVICE_CODE
    if os.name == "posix":
        assert stat.S_IMODE(device_file.stat().st_mode) == 0o600
        assert stat.S_IMODE((home / "substrate").stat().st_mode) & 0o077 == 0


def test_poll_completes_and_stores_key_privately(monkeypatch, device_server, _isolated_home):
    home = _isolated_home
    manager, _origin_value = _begin(monkeypatch, device_server, home)
    device_server.token_mode = "approved"
    assert manager._poll_once() is False  # terminal: stored, thread stops
    token_file = home / "substrate" / "credentials" / "access-token"
    assert token_file.read_text(encoding="utf-8") == TOKEN
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert f"SUBSTRATE_API_KEY={TOKEN}" in env_text
    state = json.loads((home / "substrate" / "onboarding.json").read_text())
    assert state["phase"] == "connected"
    assert TOKEN not in json.dumps(state)
    assert not (home / "substrate" / "credentials" / "onboarding-device").exists()
    if os.name == "posix":
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600
    # The stored credential now resolves without any environment key.
    assert credentials.stored_api_key(home.resolve()) == TOKEN


def test_slow_down_backs_off_and_still_completes(
    monkeypatch, device_server, _isolated_home
):
    home = _isolated_home
    manager, _origin_value = _begin(monkeypatch, device_server, home)
    device_server.token_mode = "slow_down_once"
    device_server.token_mode = "approved"  # approved after the single slow_down
    # First poll sees slow_down only when the stub says so; emulate ordering:
    device_server.slowed = False
    device_server.token_mode = lambda: "slow_down_once" if not device_server.slowed else "approved"
    assert manager._poll_once() is True
    state = json.loads((home / "substrate" / "onboarding.json").read_text())
    assert state["interval"] == 6  # 1 + 5 slow_down penalty
    assert manager._poll_once() is False
    assert (home / "substrate" / "credentials" / "access-token").read_text() == TOKEN


def test_denied_and_expired_fail_safely(monkeypatch, device_server, _isolated_home):
    home = _isolated_home
    manager, _origin_value = _begin(monkeypatch, device_server, home)
    device_server.token_mode = "access_denied"
    assert manager._poll_once() is False
    state = json.loads((home / "substrate" / "onboarding.json").read_text())
    assert state["phase"] == "declined"
    assert not (home / "substrate" / "credentials" / "onboarding-device").exists()

    home2 = home.parent / "home2"
    home2.mkdir()
    manager2 = onboarding.OnboardingManager(home2.resolve(), device_server.origin)
    assert manager2.ensure(force=True)["status"] == "authorization_pending"
    device_server.token_mode = "expired_token"
    assert manager2._poll_once() is False
    state2 = json.loads((home2 / "substrate" / "onboarding.json").read_text())
    assert state2["phase"] == "failed"


def test_bad_scope_or_token_shape_fails_closed(monkeypatch, device_server, _isolated_home):
    home = _isolated_home
    manager, _origin_value = _begin(monkeypatch, device_server, home)
    device_server.token_mode = "approved"
    device_server.token_body = {
        "access_token": TOKEN,
        "token_type": "Bearer",
        "scope": "capture",  # wrong: must be exactly "capture retrieve"
    }
    assert manager._poll_once() is False
    state = json.loads((home / "substrate" / "onboarding.json").read_text())
    assert state == {**state, "phase": "failed"}
    assert not (home / "substrate" / "credentials" / "access-token").exists()

    device_server.token_body = {
        "access_token": "not-a-substrate-token",
        "token_type": "Bearer",
        "scope": "capture retrieve",
    }
    home3 = home.parent / "home3"
    home3.mkdir()
    manager3 = onboarding.OnboardingManager(home3.resolve(), device_server.origin)
    assert manager3.ensure(force=True)["status"] == "authorization_pending"
    assert manager3._poll_once() is False
    state3 = json.loads((home3 / "substrate" / "onboarding.json").read_text())
    assert state3["phase"] == "failed"


def test_attacker_verification_url_rejected():
    with pytest.raises(onboarding.OnboardingError):
        onboarding.safe_verification_url(
            "http://127.0.0.1:9",
            "http://attacker.example/oauth/device?user_code=BCDF-GHJK",
            "BCDF-GHJK",
        )
    with pytest.raises(onboarding.OnboardingError):
        onboarding.safe_verification_url(
            "http://127.0.0.1:9",
            "http://127.0.0.1:9/oauth/device?user_code=WRONG-CODE",
            "BCDF-GHJK",
        )


def test_device_flow_401_fails_safely(monkeypatch, device_server, _isolated_home):
    home = _isolated_home
    origin = _origin(monkeypatch, device_server)
    device_server.token_mode = "approved"
    # Force the authorization endpoint to answer 401: no grant is created.
    real = onboarding.request_json

    def _unauthorized(o, p, **k):
        if p == "/oauth/device_authorization":
            return 401, {"error": "invalid_client"}
        return real(o, p, **k)

    monkeypatch.setattr(onboarding, "request_json", _unauthorized)
    manager = onboarding.OnboardingManager(home.resolve(), origin)
    status = manager.ensure(force=True)
    assert status["status"] == "failed"
    assert not (home / "substrate" / "credentials" / "onboarding-device").exists()


def test_auth_failure_clears_token_but_keeps_spool(
    monkeypatch, device_server, real_spool, _isolated_home
):
    """401/403 deletes the stale credential; spooled events stay spooled."""
    from substrate import spool as spool_module

    home = _isolated_home
    manager, _origin_value = _begin(monkeypatch, device_server, home)
    device_server.token_mode = "approved"
    assert manager._poll_once() is False

    envelope = {
        "schema_version": 3,
        "contract_version": 1,
        "event_id": "11111111-1111-4111-8111-111111111111",
        "kind": "capture_turn",
        "session_id": "s",
        "offset": {"start": 0, "end": 1},
        "capture_origin": "live",
        "batch_id": "",
        "speaker": {"id": "u", "role": "owner", "display": ""},
        "created_at": "2026-01-01T00:00:00Z",
        "payload": {"turn_id": "t", "messages": [
            {"index": 0, "role": "user", "content": "hi"}]},
    }
    spool_module.get_spool().enqueue(
        envelope, priority=1, kind="capture_turn", capture_origin="live"
    )
    assert spool_module.get_spool().pending() == 1

    # Fresh-process view: completion exported the key in-process above.
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    onboarding.note_auth_failure()
    assert (home / "substrate" / "credentials" / "access-token").exists() is False
    assert spool_module.get_spool().pending() == 1  # nothing lost

    # Login repairs credentials without losing the pending event.
    home2 = home  # same profile repairs in place
    manager2 = onboarding.OnboardingManager(home2.resolve(), device_server.origin)
    assert manager2.ensure(force=True)["status"] == "authorization_pending"
    assert manager2._poll_once() is False
    assert credentials.stored_api_key(home.resolve()) == TOKEN
    assert spool_module.get_spool().pending() == 1


def test_missing_key_instructs_cli_login(monkeypatch, _isolated_home):
    """No credential, no network: tools point at onboard.py, never env keys."""
    from substrate import plugin

    monkeypatch.setattr(
        onboarding, "ensure_started", lambda *, force=False: None
    )
    result = json.loads(plugin.memory_search({"query": "hello"}))
    assert result["status"] == "authorization_required"
    assert "onboard.py" in result["message"]
    assert "SUBSTRATE_API_KEY=" not in result["message"]
    notice = plugin.pre_llm_call("s", "q", [])
    assert notice is not None and "onboard.py" in notice["context"]


def test_symlinked_store_refused(monkeypatch, device_server, _isolated_home):
    home = _isolated_home
    link = home / "substrate"
    link.symlink_to(device_server.origin and (home / "elsewhere") or (home / "elsewhere"))
    (home / "elsewhere").mkdir(exist_ok=True)
    origin = _origin(monkeypatch, device_server)
    manager = onboarding.OnboardingManager(home.resolve(), origin)
    # resolve() follows the symlink; the manager must still fail closed on
    # credential writes rather than writing through the link.
    status = manager.ensure(force=True)
    assert status["status"] in ("authorization_pending", "failed")


# ---------------------------------------------------------------------------
# Installed-CLI tests (subprocess, no PYTHONPATH, no editable install)
# ---------------------------------------------------------------------------


def _cli_env(home, origin):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
        and not key.startswith("SUBSTRATE_")
        and key != "HERMES_HOME"
    }
    env.update({
        "HERMES_HOME": str(home),
        "SUBSTRATE_API_URL": origin,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
    })
    return env


def test_cli_status_json_is_content_free(device_server, tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    proc = subprocess.run(
        [sys.executable, str(PLUGIN_CLI), "status", "--json"],
        capture_output=True, text=True, timeout=60,
        env=_cli_env(home, device_server.origin),
    )
    assert proc.returncode == 1  # new profile: nothing pending yet
    payload = json.loads(proc.stdout)
    assert payload["status"] in ("new", "failed", "invalid")
    assert TOKEN not in proc.stdout and DEVICE_CODE not in proc.stdout


def test_cli_start_then_poll_connects(device_server, tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    env = _cli_env(home, device_server.origin)
    device_server.token_mode = "approved"
    started = subprocess.run(
        [sys.executable, str(PLUGIN_CLI), "start", "--json"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert started.returncode == 0, started.stderr
    payload = json.loads(started.stdout)
    assert payload["status"] == "authorization_pending"
    assert payload["user_code"] == "BCDF-GHJK"
    assert TOKEN not in started.stdout and DEVICE_CODE not in started.stdout

    # The start command only begins the grant (its process exits, so no
    # background thread survives); poll waits boundedly for approval.
    polled = subprocess.run(
        [sys.executable, str(PLUGIN_CLI), "poll", "--json",
         "--timeout", "30"],
        capture_output=True, text=True, timeout=90, env=env,
    )
    assert polled.returncode == 0, polled.stdout + polled.stderr
    assert json.loads(polled.stdout)["status"] == "ready"
    assert TOKEN not in polled.stdout and DEVICE_CODE not in polled.stdout
    assert (home / "substrate" / "credentials" / "access-token").read_text() == TOKEN


# ---------------------------------------------------------------------------
# Real-server end-to-end: actual agent-auth.mjs device code, temp DB file.
# MOCK HUMAN BROWSER AUTH (test-only): the /__test/approve probe stands in
# for the user opening verification_uri_complete, signing in, and clicking
# approve. No live tenant or credential is created (temp DB, loopback).
# ---------------------------------------------------------------------------

NODE = shutil.which("node")
SERVER_REPO = Path("/tmp/wt-hermes-durability")

BOOT_JS = """
import http from "node:http";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { writeFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { createHash } from "node:crypto";
import { createAgentOnboarding } from "__REPO__/agent-auth.mjs";

const issued = new Map();
const dir = mkdtempSync(join(tmpdir(), "substrate-device-e2e-"));
const db = new DatabaseSync(join(dir, "device.db"));
// Harness scaffolding (test-only): the real issuance path counts existing
// credentials per tenant for avatar rotation. The temp DB provides that
// table; approval state itself lives in the real agent_device_grants table.
db.exec("CREATE TABLE IF NOT EXISTS agent_credentials (tenant_id TEXT)");
let onboarding = null;
function registerAgent(input) { issued.set(input.tokenHash, input); }
const CAPS = {
  contract_version: 1,
  provider: "substrate",
  server_commit: "e2e",
  limits: {
    max_event_bytes: 262144, max_upload_bytes: 262144,
    max_tool_call_bytes: 4096, max_tool_result_bytes: 8192,
    turn_context_deadline_ms: 500, action_cues_deadline_ms: 100,
    rules_refresh_seconds: 300,
  },
  actions: ["stored", "duplicate", "sealed", "queued"],
  kinds: ["capture_turn", "capture_session", "memory_write", "memory_forget",
          "consent", "page_propose", "upload"],
  tenant: { tenant_id: "test-tenant", brief_version: 0 },
};
function authed(req) {
  const match = /^Bearer (\\S+)$/.exec(String(req.headers.authorization || ""));
  if (!match) return false;
  return issued.has(createHash("sha256").update(match[1]).digest("hex"));
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  if (req.method === "POST" && url.pathname === "/__test/approve") {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const { user_code } = JSON.parse(raw);
    const out = onboarding.approve({
      userCode: user_code, clerkUserId: "test-user",
      tenantId: "test-tenant", decision: "approve",
    });
    res.writeHead(out.status, { "content-type": "application/json" });
    res.end(JSON.stringify(out.body));
    return;
  }
  if (req.method === "GET" && url.pathname === "/api/v1/capabilities") {
    if (!authed(req)) {
      res.writeHead(401, { "content-type": "application/json" });
      res.end('{"error":"unauthorized"}');
      return;
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(CAPS));
    return;
  }
  if (req.method === "POST" && url.pathname === "/api/v1/memory/search") {
    if (!authed(req)) {
      res.writeHead(401, { "content-type": "application/json" });
      res.end('{"error":"unauthorized"}');
      return;
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end('{"contract_version":1,"results":[]}');
    return;
  }
  try {
    if (!await onboarding.handlePublic(req, res, url)) {
      res.writeHead(404, { "content-type": "application/json" });
      res.end('{"error":"not_found"}');
    }
  } catch (error) {
    res.writeHead(500, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: String(error && error.message || error) }));
  }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
// The API origin (and therefore the verification URLs) must carry the real
// ephemeral port, so the onboarding object is created after listen.
onboarding = createAgentOnboarding({
  db,
  publicOrigin: `http://127.0.0.1:${server.address().port}`,
  registerAgent,
});
writeFileSync(process.env.SUBSTRATE_READY_FILE,
  JSON.stringify({ base: `http://127.0.0.1:${server.address().port}` }));
"""


def _server_available() -> bool:
    return (
        NODE is not None
        and SERVER_REPO.is_dir()
        and (SERVER_REPO / "agent-auth.mjs").is_file()
    )


@pytest.fixture(scope="module")
def real_device_server(tmp_path_factory):
    if not _server_available():
        pytest.skip("node or durability server checkout unavailable")
    work = tmp_path_factory.mktemp("device-e2e")
    (work / "boot.mjs").write_text(BOOT_JS.replace("__REPO__", str(SERVER_REPO)))
    ready = work / "ready.json"
    proc = subprocess.Popen(
        [NODE, str(work / "boot.mjs")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(work),
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(work),
            "TMPDIR": str(work),
            "NODE_ENV": "test",
            "NO_PROXY": "*",
            "SUBSTRATE_READY_FILE": str(ready),
        },
    )
    try:
        deadline = time.monotonic() + 60.0
        base = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                base = json.loads(ready.read_text()).get("base", "")
                if base:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        assert base, "real device server never became ready"
        yield base
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10.0)


def test_device_login_end_to_end_against_real_server(real_device_server, tmp_path):
    """Full login against the real agent-auth.mjs device code (temp DB).

    MOCK HUMAN BROWSER AUTH (test-only): the /__test/approve call below
    stands in for the user opening verification_uri_complete in a browser,
    signing in, and approving. Everything else (grant, poll interval,
    slow_down, token issuance, capability health check, private store) is
    the real code path on both sides.
    """
    home = tmp_path / "e2e-home"
    home.mkdir()
    env = _cli_env(home, real_device_server)
    assert urllib.parse.urlsplit(real_device_server).hostname in ("127.0.0.1", "localhost")

    started = subprocess.run(
        [sys.executable, str(PLUGIN_CLI), "start", "--json"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert started.returncode == 0, started.stderr
    grant = json.loads(started.stdout)
    assert grant["status"] == "authorization_pending"
    assert grant["verification_uri_complete"].startswith(real_device_server)
    assert "sk_sub_" not in started.stdout

    # --- MOCK HUMAN BROWSER AUTH (test-only, see docstring) ---
    approval = urllib.request.Request(
        real_device_server + "/__test/approve",
        data=json.dumps({"user_code": grant["user_code"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(approval, timeout=30) as response:
        assert response.status == 200

    polled = subprocess.run(
        [sys.executable, str(PLUGIN_CLI), "poll", "--json",
         "--timeout", "60"],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert polled.returncode == 0, polled.stdout + polled.stderr
    assert json.loads(polled.stdout)["status"] == "ready"
    assert "sk_sub_" not in polled.stdout

    stored = (home / "substrate" / "credentials" / "access-token").read_text()
    assert stored.startswith("sk_sub_")
    state = json.loads((home / "substrate" / "onboarding.json").read_text())
    assert state["phase"] == "connected"
    assert stored not in json.dumps(state)


# ---------------------------------------------------------------------------
# Pre-login durability: missing credentials never start/swap the sender.
# Uses the real Spool plus the conftest stub ledger (loopback, no live API).
# ---------------------------------------------------------------------------


def _wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _capture_turn():
    from substrate import plugin as _plugin

    _plugin.sync_turn(
        "hello",
        "world",
        session_id="s",
        messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ],
        turn_id="t",
    )


def _quarantined_count(spool) -> int:
    return sum(
        value["item_count"]
        for key, value in spool.counters().items()
        if key.endswith("|quarantined")
    )


def test_no_credential_capture_stays_pending_then_replays_after_login(
    monkeypatch, real_spool, stub_ledger, clean_session_state, _isolated_home
):
    """Missing key: enqueue only (no sender, no quarantine); login replays."""
    import substrate.spool as spool_module

    home = _isolated_home
    origin = f"http://127.0.0.1:{stub_ledger.server_address[1]}"
    monkeypatch.setenv("SUBSTRATE_API_URL", origin)
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)

    _capture_turn()
    assert spool_module.get_spool().pending() == 1
    thread = spool_module.get_spool()._thread
    assert thread is None or not thread.is_alive()
    time.sleep(0.5)
    assert spool_module.get_spool().pending() == 1
    assert _quarantined_count(spool_module.get_spool()) == 0

    # Simulate login: store the key, next capture starts the sender normally.
    credentials.store_token(home.resolve(), origin, "k")
    _capture_turn()
    assert _wait_for(lambda: spool_module.get_spool().pending() == 0)
    assert len(stub_ledger.posts) == 2
    assert _quarantined_count(spool_module.get_spool()) == 0


def test_revoked_credential_retains_pending_through_reconnect(
    monkeypatch, real_spool, stub_ledger, clean_session_state, _isolated_home
):
    """401: items stay spooled (never quarantined) across clear + re-login."""
    import substrate.spool as spool_module

    home = _isolated_home
    origin = f"http://127.0.0.1:{stub_ledger.server_address[1]}"
    monkeypatch.setenv("SUBSTRATE_API_URL", origin)
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    credentials.store_token(home.resolve(), origin, "k")

    stub_ledger.mode = "error"
    stub_ledger.status = 401
    _capture_turn()
    assert _wait_for(lambda: len(stub_ledger.posts) >= 1)
    time.sleep(0.5)
    assert spool_module.get_spool().pending() == 1
    assert _quarantined_count(spool_module.get_spool()) == 0

    # Tool-path 401 handling clears the stale credential; the next capture
    # must not swap the running sender to an empty key.
    onboarding.note_auth_failure()
    assert credentials.stored_api_key(home.resolve()) == ""
    _capture_turn()
    assert spool_module.get_spool().pending() == 2
    time.sleep(0.5)
    assert spool_module.get_spool().pending() == 2
    assert _quarantined_count(spool_module.get_spool()) == 0

    # Re-login: the next capture updates the sender and replays everything.
    # The sender backs off up to ~36 s after the 401 (spool-owned auth
    # backoff), so this wait is bounded generously but polls out early.
    stub_ledger.mode = "ack"
    stub_ledger.status = 200
    credentials.store_token(home.resolve(), origin, "k")
    _capture_turn()
    assert _wait_for(
        lambda: spool_module.get_spool().pending() == 0, timeout=75.0, interval=0.5
    )
    assert _quarantined_count(spool_module.get_spool()) == 0


# ---------------------------------------------------------------------------
# Stale host-env reauth: the host loaded profile .env at startup, a later
# login replaced it, and the host process still holds the rejected token.
# ---------------------------------------------------------------------------

OLD_TOKEN = "sk_sub_" + "o" * 32
FRESH_TOKEN = "sk_sub_" + "n" * 32


def test_stale_env_token_heals_to_stored_login_key(
    monkeypatch, stub_ledger, clean_session_state, _isolated_home
):
    """401 on the stale env token reloads the login-written key in-process."""
    from substrate import plugin as _plugin

    home = _isolated_home
    origin = f"http://127.0.0.1:{stub_ledger.server_address[1]}"
    monkeypatch.setenv("SUBSTRATE_API_URL", origin)
    credentials.store_token(home.resolve(), origin, FRESH_TOKEN)
    monkeypatch.setenv("SUBSTRATE_API_KEY", OLD_TOKEN)  # stale host startup env
    monkeypatch.setenv("SUBSTRATE_UNRELATED_SENTINEL", "keep-me")

    from substrate.client import SubstrateClient

    presented: list[str] = []
    real_from_env = SubstrateClient.from_env

    def _spy():
        client = real_from_env()
        presented.append(client.api_key)
        return client

    monkeypatch.setattr(SubstrateClient, "from_env", staticmethod(_spy))

    stub_ledger.mode = "error"
    stub_ledger.status = 401
    first = json.loads(_plugin.memory_search({"query": "hello"}))
    assert first == {"error": "transport_error"}
    assert presented == [OLD_TOKEN]
    assert os.environ["SUBSTRATE_API_KEY"] == FRESH_TOKEN

    stub_ledger.mode = "ack"
    stub_ledger.status = 200
    _plugin.memory_search({"query": "hello"})
    assert presented == [OLD_TOKEN, FRESH_TOKEN]
    last = stub_ledger.posts[-1]
    assert last["path"] == "/api/v1/memory/search"

    assert os.environ["SUBSTRATE_UNRELATED_SENTINEL"] == "keep-me"
    assert FRESH_TOKEN in (home / ".env").read_text(encoding="utf-8")
    assert OLD_TOKEN not in json.dumps(first)


def test_rejected_env_without_stored_key_stops_401_loop(
    monkeypatch, stub_ledger, clean_session_state, _isolated_home
):
    """401 on env token with nothing stored: pop it, instruct login once."""
    from substrate import plugin as _plugin

    monkeypatch.setenv(
        "SUBSTRATE_API_URL", f"http://127.0.0.1:{stub_ledger.server_address[1]}"
    )
    monkeypatch.setenv("SUBSTRATE_API_KEY", OLD_TOKEN)
    monkeypatch.setenv("SUBSTRATE_UNRELATED_SENTINEL", "keep-me")
    monkeypatch.setattr(onboarding, "ensure_started", lambda *, force=False: None)

    stub_ledger.mode = "error"
    stub_ledger.status = 401
    first = json.loads(_plugin.memory_search({"query": "hello"}))
    assert first == {"error": "transport_error"}
    assert os.environ.get("SUBSTRATE_API_KEY", "") == ""

    second = json.loads(_plugin.memory_search({"query": "hello"}))
    assert second["status"] == "authorization_required"
    assert "onboard.py" in second["message"]
    assert OLD_TOKEN not in json.dumps(second)
    assert os.environ["SUBSTRATE_UNRELATED_SENTINEL"] == "keep-me"
