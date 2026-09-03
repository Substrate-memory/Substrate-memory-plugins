"""Live session-completion capture tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from substrate import contract
from substrate import plugin


@pytest.fixture(autouse=True)
def _reset_sent_sessions():
    with plugin._SENT_SESSIONS_LOCK:
        plugin._SENT_SESSIONS.clear()
    yield
    with plugin._SENT_SESSIONS_LOCK:
        plugin._SENT_SESSIONS.clear()


def test_session_envelope_is_contract_valid():
    envelope = plugin._session_envelope("session-1", "reset", platform="telegram", chat_type="direct", next_session_id="session-2")
    assert envelope is not None
    assert envelope["kind"] == "capture_session"
    assert envelope["payload"]["boundary"] == "reset"
    assert envelope["payload"]["session_complete"] is True
    assert envelope["payload"]["next_session_id"] == "session-2"
    contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])


def test_session_envelope_rejects_invalid_boundary():
    assert plugin._session_envelope("s", "not-a-boundary") is None


def test_end_session_enqueues_once_and_ignores_repeats(monkeypatch):
    queued = []
    monkeypatch.setattr(plugin._CAPTURE_WORKER, "enqueue", queued.append)
    plugin.end_session("session-1", boundary="end")
    plugin.end_session("session-1", boundary="reset")
    plugin.end_session("session-1", boundary="end")
    assert len(queued) == 1
    assert queued[0]["kind"] == "capture_session"
    assert queued[0]["payload"]["session_complete"] is True


def test_end_session_swallows_all_failures(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("nope")
    monkeypatch.setattr(plugin._CAPTURE_WORKER, "enqueue", boom)
    plugin.end_session("session-1", boundary="end")  # must not raise


def test_on_session_reset_uses_old_session_id(monkeypatch):
    queued = []
    monkeypatch.setattr(plugin._CAPTURE_WORKER, "enqueue", queued.append)
    plugin.on_session_reset(old_session_id="old-1", new_session_id="new-1", platform="telegram")
    assert len(queued) == 1
    envelope = queued[0]
    assert envelope["session_id"] == "old-1"
    assert envelope["payload"]["boundary"] == "reset"
    assert envelope["payload"]["next_session_id"] == "new-1"


def test_on_session_finalize_uses_session_id(monkeypatch):
    queued = []
    monkeypatch.setattr(plugin._CAPTURE_WORKER, "enqueue", queued.append)
    plugin.on_session_finalize(session_id="s-final", platform="cli")
    assert queued[0]["session_id"] == "s-final"
    assert queued[0]["payload"]["boundary"] == "end"


def test_register_includes_session_hooks():
    registered = {}
    class Ctx:
        def register_hook(self, name, cb):
            registered[name] = cb
        def register_tool(self, **kwargs):
            pass
        def register_system_prompt_section(self, *a, **k):
            pass
    plugin.register(Ctx())
    assert "on_session_reset" in registered
    assert "on_session_finalize" in registered
    assert registered["on_session_reset"] is plugin.on_session_reset
