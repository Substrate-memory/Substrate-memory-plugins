# Session-level isolation for the multi-host test suite.
#
# Each host plugin vendors top-level module names (substrate_core and, for
# some hosts, runtime/contract/bridge/...), so this reaps any colliding
# modules and plugin sys.path entries before collection and after every
# test. Host test files additionally wrap their own imports in
# _hostload.isolated() so collection order never decides which hosts copy
# wins. Hermes reference tests (test_substrate_*.py) import the Hermes
# plugin themselves and are unaffected: they hold their own references.
from __future__ import annotations

import sys
from pathlib import Path

import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _hostload

_hostload.scrub()

# The Hermes reference plugin (plugins/substrate) is a src-layout checkout:
# its importable package lives at plugins/substrate/src. Insert after the
# scrub above so the entry survives collection.
HERMES_SRC = str(Path(__file__).resolve().parent.parent / "plugins" / "substrate" / "src")
if HERMES_SRC not in sys.path:
    sys.path.insert(0, HERMES_SRC)


@pytest.fixture(autouse=True)
def _reap_host_module_leak():
    before_path = list(sys.path)
    before_modules = {
        name for name in sys.modules if _hostload._colliding(name)
    }
    yield
    sys.path[:] = before_path
    for name in list(sys.modules):
        if _hostload._colliding(name) and name not in before_modules:
            del sys.modules[name]


# --- Hermes reference plugin fixtures (ported from the plugin source) ---
# No module patching: the real ``substrate.spool`` interface is used
# directly. ``FakeSpool`` is a plain in-memory double for fast unit tests;
# ``real_spool`` wires a real ``Spool`` in a temp dir (never the live DB);
# ``stub_ledger`` is a local HTTP ledger stub (no live API).

class FakeSpool:
    """In-memory stand-in: records enqueues, never sends."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.start_calls: list = []
        self._next = 0

    def enqueue(self, envelope: dict, *, priority: int, kind: str,
                capture_origin: str) -> str:
        self._next += 1
        item_id = f"item-{self._next}"
        self.items.append({
            "item_id": item_id,
            "envelope": envelope,
            "priority": priority,
            "kind": kind,
            "capture_origin": capture_origin,
        })
        return item_id

    def start(self, client) -> None:
        self.start_calls.append(client)

    def stop(self, timeout: float = 5.0) -> None:
        pass

    def counters(self) -> dict:
        return {"queued": len(self.items)}

    def reset(self) -> None:
        self.items.clear()
        self.start_calls.clear()


@pytest.fixture
def fake_spool(monkeypatch):
    from substrate import plugin as _plugin

    fake = FakeSpool()
    monkeypatch.setattr(_plugin, "get_spool", lambda: fake)
    return fake


@pytest.fixture
def clean_session_state():
    from substrate import plugin as _plugin

    _plugin._SESSION_STATE["active"] = None
    _plugin._SESSION_STATE["high_water"].clear()
    _plugin._SUBAGENT_PARENTS.clear()
    yield
    _plugin._SESSION_STATE["active"] = None
    _plugin._SESSION_STATE["high_water"].clear()
    _plugin._SUBAGENT_PARENTS.clear()


def _spool_base(tmp_path):
    """Prefer tmpfs to avoid disk-journal stalls; disk tests opt out."""
    shm = Path("/dev/shm")
    try:
        if shm.is_dir():
            probe = shm / f"substrate-probe-{os.getpid()}"
            probe.mkdir(exist_ok=True)
            probe.rmdir()
            return Path(
                tempfile.mkdtemp(prefix="substrate-test-spool-", dir=str(shm))
            )
    except OSError:
        pass
    return tmp_path


@pytest.fixture
def real_spool(tmp_path):
    """A real Spool in a temp dir (tmpfs when available), as the singleton."""
    import substrate.spool as spool_module

    spool_module.configure_spool(_spool_base(tmp_path) / "spool")
    yield spool_module.get_spool()
    spool_module.reset_spool()


class _StubHandler(BaseHTTPRequestHandler):
    server_version = "StubLedger/1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, ValueError):
            body = {}
        self.server.posts.append({  # type: ignore[attr-defined]
            "path": self.path,
            "body": body,
            "idempotency_key": self.headers.get("Idempotency-Key"),
        })
        mode = self.server.mode  # type: ignore[attr-defined]
        delay = self.server.delay  # type: ignore[attr-defined]
        if delay:
            time.sleep(delay)
        status = self.server.status  # type: ignore[attr-defined]
        if mode == "ack":
            payload = {
                "event_id": body.get("event_id"),
                "accepted": True,
                "stored": True,
                "status": "accepted",
                "action": "stored",
                "handle": self.server.handle,  # type: ignore[attr-defined]
            }
        elif mode == "bad_ack":
            payload = {"stored": True, "event_id": "mismatch", "action": "stored"}
        else:
            payload = {"error": "not_found"}
        raw_out = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw_out)))
        self.end_headers()
        try:
            self.wfile.write(raw_out)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture
def stub_ledger():
    """Local stub ledger server. Tune via ``server.mode/delay/status/handle``.

    Modes: "ack" (200 + stored ACK with handle), "bad_ack" (200, mismatched
    id), "error" (configurable status, default 404). ``server.posts`` records
    every POST with its Idempotency-Key.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    server.daemon_threads = True
    server.mode = "ack"
    server.delay = 0.0
    server.status = 200
    server.handle = "m:44a1b02e"
    server.posts = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5.0)
    server.server_close()
