"""Hermes device sign-in against the device endpoints of the server in production (2ab2dbb).

The fake follows agent-api/agent-auth.mjs of 2ab2dbb: a grant is pending until
approved, the approved grant is issued once and the same token comes back on
later polls until the grant expires, polls closer than the poll window get
slow_down, an unknown device code gets expired_token, and unknown form fields
(reconnect_token) are ignored with 200. A live run of the same flow against a
2ab2dbb server was done by hand (see the pull request), not in this suite.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "plugins" / "substrate-hermes" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import substrate.onboarding as ob  # noqa: E402


class _Grants:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.by_device = {}  # device_code -> dict
        self.by_user = {}
        self.forms = []  # every device_authorization form the server saw
        self.polls = []  # every device_code polled
        self.last_poll = {}
        self.counter = 0
        self.poll_window = 0.0
        self.agent_id = "agent_fakefresh1"

    def issue(self, form):
        self.counter += 1
        device = "%064x" % (self.counter * 7919 + 12345)
        alphabet = "BCDFGHJKLMNPQRSTVWXZ23456789"
        n = self.counter
        chars = []
        for _ in range(8):
            chars.append(alphabet[n % len(alphabet)])
            n //= len(alphabet)
        user = "".join(chars[:4]) + "-" + "".join(chars[4:])
        grant = {"status": "pending", "user": user, "token": None, "expires": time.time() + 900}
        self.by_device[device] = grant
        self.by_user[user] = device
        self.forms.append(dict(form))
        return device, user

    def approve(self, user):
        with self.lock:
            device = self.by_user[user]
            self.by_device[device]["status"] = "approved"


def _handler(grants: _Grants, reject_unknown_fields: bool = False):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # noqa: ANN002
            return

        def _json(self, status, value):
            raw = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0") or 0)
            form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode("utf-8")))
            if self.path == "/oauth/device_authorization":
                if reject_unknown_fields and "reconnect_token" in form:
                    return self._json(400, {"error": "invalid_request"})
                with grants.lock:
                    device, user = grants.issue(form)
                base = "http://127.0.0.1:%d" % self.server.server_port
                return self._json(200, {
                    "device_code": device, "user_code": user,
                    "verification_uri": base + "/oauth/device",
                    "verification_uri_complete": base + "/oauth/device?user_code=" + user,
                    "expires_in": 900, "interval": 1,
                })
            if self.path == "/oauth/token":
                return self._token(form)
            return self._json(404, {"error": "not_found"})

        def _token(self, form):
            device = form.get("device_code", "")
            with grants.lock:
                grants.polls.append(device)
                grant = grants.by_device.get(device)
                if grant is None or time.time() >= grant["expires"]:
                    return self._json(400, {"error": "expired_token"})
                last = grants.last_poll.get(device, 0.0)
                grants.last_poll[device] = time.time()
                if last and time.time() - last < grants.poll_window:
                    return self._json(400, {"error": "slow_down"})
                if grant["status"] == "pending":
                    return self._json(400, {"error": "authorization_pending"})
                if grant["status"] == "approved":
                    grant["status"] = "consumed"
                    grant["token"] = "sk_sub_" + ("%024x" % (time.time_ns() % (16 ** 24)))
                if grant["status"] == "consumed":
                    return self._json(200, {
                        "access_token": grant["token"], "token_type": "Bearer",
                        "scope": "capture retrieve", "account": "fake@example.com",
                        "agent_name": "Hermes", "agent_id": grants.agent_id,
                    })
                return self._json(400, {"error": "invalid_grant"})

    return Handler


@pytest.fixture()
def fake(request):
    grants = _Grants()
    reject = bool(getattr(request, "param", False))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(grants, reject))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield grants, "http://127.0.0.1:%d" % server.server_port
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _restore_process_environment():
    # A successful approval stores the key in this process's environment; never leak it.
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture()
def manager_home(tmp_path, monkeypatch):
    monkeypatch.setattr(ob, "CHECK_TIMEOUT_SECONDS", 2.0)
    return tmp_path / "hermes"


def _approve_and_poll(manager, grants, user, steps=20):
    grants.approve(user)
    for _ in range(steps):
        manager._poll_once(wait=5.0)
        state = ob._load_state(manager.home)
        if state.get("phase") != "pending":
            return state
        time.sleep(0.05)
    return ob._load_state(manager.home)


def test_manager_begin_and_poll_connect_and_poll_the_same_code(fake, manager_home, monkeypatch):
    grants, origin = fake
    monkeypatch.setattr(ob, "check_token", lambda origin_, token, **kw: "ok")
    manager = ob.OnboardingManager(manager_home, origin)
    shown = manager._begin()
    assert shown["status"] == "authorization_pending"
    user = shown["user_code"]
    state = _approve_and_poll(manager, grants, user)
    assert state["phase"] == "connected", state
    assert "expired_token" not in json.dumps(state)
    # Every poll carried the code that the begin call issued.
    assert len(set(grants.polls)) == 1 and grants.polls[0] == grants.by_user[user]


def test_stored_credential_is_sent_as_reconnect_token_and_ignored_when_live(fake, manager_home, monkeypatch):
    grants, origin = fake
    monkeypatch.setattr(ob, "check_token", lambda origin_, token, **kw: "ok")
    credential = "sk_sub_" + "c" * 40
    monkeypatch.setattr(ob.credentials, "stored_api_key", lambda home, *a, **k: credential)
    manager = ob.OnboardingManager(manager_home, origin)
    shown = manager._begin()
    assert grants.forms and grants.forms[-1].get("reconnect_token") == credential
    state = _approve_and_poll(manager, grants, shown["user_code"])
    assert state["phase"] == "connected", state


@pytest.mark.parametrize("fake", [True], indirect=True)
def test_server_that_rejects_reconnect_token_gets_a_plain_sign_in(fake, manager_home, monkeypatch):
    grants, origin = fake
    monkeypatch.setattr(ob, "check_token", lambda origin_, token, **kw: "ok")
    monkeypatch.setattr(ob.credentials, "stored_api_key", lambda home, *a, **k: "sk_sub_" + "d" * 40)
    manager = ob.OnboardingManager(manager_home, origin)
    shown = manager._begin()
    assert shown["status"] == "authorization_pending"
    assert "reconnect_token" not in grants.forms[-1]
    assert _approve_and_poll(manager, grants, shown["user_code"])["phase"] == "connected"


def test_trial_ended_keeps_the_connection_and_says_so(fake, manager_home, monkeypatch):
    grants, origin = fake
    monkeypatch.setattr(ob, "check_token", lambda origin_, token, **kw: "payment_required")
    manager = ob.OnboardingManager(manager_home, origin)
    shown = manager._begin()
    state = _approve_and_poll(manager, grants, shown["user_code"])
    assert state["phase"] == "connected" and state.get("billing_required") is True
    message = ob.OnboardingManager(manager_home, origin).describe(state)["message"]
    assert "Your Substrate trial has ended. Add a card at " in message and message.endswith("/billing to keep using memory.")
    assert "authenticated_health_check_failed" not in json.dumps(state)


def test_announcement_never_claims_a_connection_without_a_key(manager_home, monkeypatch):
    ob._save_state(manager_home, {"phase": "connected", "announced": False, "account": "a@example.com"})
    monkeypatch.setattr(ob, "active_home", lambda: manager_home)
    monkeypatch.setattr(ob.credentials, "stored_api_key", lambda home, *a, **k: "")
    monkeypatch.delenv(ob.credentials.ENV_KEY, raising=False)
    assert ob.take_connected_announcement() == ""


def test_model_facing_text_has_no_instruction_to_hide_anything():
    model_text = (REPO / "plugins" / "substrate-hermes" / "src" / "substrate" / "plugin.py").read_text(encoding="utf-8")
    model_text += (REPO / "plugins" / "substrate-hermes" / "src" / "substrate" / "onboarding.py").read_text(encoding="utf-8")
    for phrase in ("do not tell the user", "do not claim", "lack access", "do not mention", "hide"):
        assert not re.search(re.escape(phrase), model_text, re.IGNORECASE), phrase


# ---------------------------------------------------------------------------
# Follow-ups from round-2 QA: announce once, name a reconnect, one reminder line.


def test_connection_printed_by_cli_is_not_announced_again(manager_home, monkeypatch):
    ob._save_state(manager_home, {"phase": "connected", "announced": False, "account": "a@example.com"})
    monkeypatch.setattr(ob, "active_home", lambda: manager_home)
    monkeypatch.setattr(ob.credentials, "stored_api_key", lambda home, *a, **k: "sk_sub_" + "e" * 40)
    ob.mark_announced(manager_home)
    assert ob.take_connected_announcement() == ""


def test_reconnect_to_the_same_agent_is_named_as_a_reconnect(fake, manager_home, monkeypatch):
    grants, origin = fake
    monkeypatch.setattr(ob, "check_token", lambda origin_, token, **kw: "ok")
    grants.agent_id = "agent_samehermes"
    ob._save_last_agent(manager_home, "agent_samehermes")
    manager = ob.OnboardingManager(manager_home, origin)
    shown = manager._begin()
    state = _approve_and_poll(manager, grants, shown["user_code"])
    assert state["phase"] == "connected" and state.get("reconnected") is True
    assert ob.OnboardingManager(manager_home, origin).describe(state)["message"].startswith("Reconnected to Substrate as")


def test_a_different_agent_is_a_plain_connect(fake, manager_home, monkeypatch):
    grants, origin = fake
    monkeypatch.setattr(ob, "check_token", lambda origin_, token, **kw: "ok")
    ob._save_last_agent(manager_home, "agent_oldhermes")
    manager = ob.OnboardingManager(manager_home, origin)
    shown = manager._begin()
    state = _approve_and_poll(manager, grants, shown["user_code"])
    assert state["phase"] == "connected" and state.get("reconnected") is False
    assert ob.OnboardingManager(manager_home, origin).describe(state)["message"].startswith("Connected to Substrate as")


def test_sign_in_block_once_then_one_line_reminder(manager_home, monkeypatch):
    import substrate.plugin as plugin

    monkeypatch.setattr(ob, "active_home", lambda: manager_home)
    status = {"status": "authorization_pending", "user_code": "BCDF-GHJK", "expires_in": 600,
              "verification_uri_complete": "https://x/oauth/device?user_code=BCDF-GHJK"}
    first = plugin._login_notice(status)
    again = plugin._login_notice(status)
    assert "show the user this approval link and code exactly as written" in first
    assert "show the user this approval link" not in again
    assert "https://x/oauth/device?user_code=BCDF-GHJK" in again and "BCDF-GHJK" in again
    assert len(again) < len(first) / 2
    fresh = dict(status, user_code="ZZZZ-JJJJ")
    assert "show the user this approval link" in plugin._login_notice(fresh)
