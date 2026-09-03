"""Onboarding automation tests for the Substrate retrieval plugin."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from substrate import contract
from substrate import onboarding
from substrate import plugin


def _no_thread(self):
    return None


@pytest.fixture(autouse=True)
def _disable_onboarding_thread(monkeypatch):
    monkeypatch.setattr(onboarding.OnboardingManager, "_start_thread", _no_thread)

@pytest.fixture(autouse=True)
def _allow_test_origins(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_DEVELOPMENT_MODE", "1")
    monkeypatch.setenv("SUBSTRATE_API_URL", "https://memory.example")
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    monkeypatch.delenv("SUBSTRATE_WIKI_ORIGIN", raising=False)


def _grant(status="pending", **extra):
    body = {
        "device_code": "device-secret",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://memory.example/oauth/device",
        "verification_uri_complete": "https://memory.example/oauth/device?user_code=ABCD-EFGH",
        "expires_in": 900,
        "interval": 1,
    }
    body.update(extra)
    return (200, body) if status == "pending" else (status, body)


CAPABILITIES = {
    "contract_version": 1,
    "provider": "substrate",
    "server_commit": "main",
    "limits": dict(contract.LIMITS),
    "actions": sorted(contract.ACTIONS),
    "kinds": sorted(contract.KINDS),
    "tenant": {"tenant_id": "agent", "brief_version": 0},
}


def test_tool_without_key_starts_onboarding_and_returns_link(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(onboarding, "active_home", lambda: tmp_path.resolve())
    monkeypatch.setattr(onboarding, "_profile_homes", lambda: [tmp_path.resolve()])
    monkeypatch.setattr(
        onboarding, "request_json", lambda *a, **k: _grant()
    )
    result = json.loads(plugin.memory_search({"query": "hello"}))
    assert result["status"] == "authorization_required"
    assert result["user_code"] == "ABCD-EFGH"
    assert result["verification_uri_complete"].startswith("https://memory.example/oauth/device")
    state = json.loads((tmp_path / "substrate" / "onboarding.json").read_text())
    assert state["phase"] == "pending"
    # device_code is a credential and must never be persisted to state
    assert "device_code" not in state


def test_onboarding_completes_and_stores_key_in_dotenv(monkeypatch, tmp_path):
    home = tmp_path
    (home / ".env").write_text("OPENAI_API_KEY=sk-existing\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(onboarding, "active_home", lambda: home.resolve())
    responses = iter([
        _grant(),                     # device authorization
        (400, {"error": "authorization_pending"}),
        (200, {"access_token": "sk-sub-token", "token_type": "Bearer"}),
        (200, CAPABILITIES),          # capability preflight
        (200, {"contract_version": 1, "results": []}),  # search preflight
    ])
    monkeypatch.setattr(onboarding, "request_json", lambda *a, **k: next(responses))
    monkeypatch.setattr(onboarding.time, "sleep", lambda s: None)
    manager = onboarding.OnboardingManager(home.resolve(), "https://memory.example")
    status = manager.ensure(force=True)
    assert status["status"] == "authorization_pending"
    # simulate the background thread synchronously: pending, then issued token
    assert manager._poll_once() is True
    manager._poll_once()
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "SUBSTRATE_API_KEY=sk-sub-token" in env_text
    assert "OPENAI_API_KEY=sk-existing" in env_text
    assert stat.S_IMODE((home / ".env").stat().st_mode) & 0o077 == 0
    state = json.loads((home / "substrate" / "onboarding.json").read_text())
    assert state["phase"] == "connected"
    assert "access_token" not in state and "device_code" not in state


def test_onboarding_declined_and_expired_fail_closed(monkeypatch, tmp_path):
    home = tmp_path
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(onboarding, "active_home", lambda: home.resolve())
    responses = iter([
        _grant(),
        (400, {"error": "access_denied"}),
    ])
    monkeypatch.setattr(onboarding, "request_json", lambda *a, **k: next(responses))
    monkeypatch.setattr(onboarding.time, "sleep", lambda s: None)
    manager = onboarding.OnboardingManager(home.resolve(), "https://memory.example")
    manager.ensure(force=True)
    manager._poll_once()
    state = json.loads((home / "substrate" / "onboarding.json").read_text())
    assert state["phase"] == "declined"

    responses2 = iter([
        _grant(),
        (400, {"error": "expired_token"}),
    ])
    monkeypatch.setattr(onboarding, "request_json", lambda *a, **k: next(responses2))
    manager2 = onboarding.OnboardingManager(home.resolve(), "https://memory.example")
    manager2.ensure(force=True)
    manager2._poll_once()
    state2 = json.loads((home / "substrate" / "onboarding.json").read_text())
    assert state2["phase"] == "failed"
    assert state2["error_class"] == "authorization_expired"


def test_invalid_key_is_never_stored(monkeypatch, tmp_path):
    home = tmp_path
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(onboarding, "active_home", lambda: home.resolve())
    responses = iter([
        _grant(),
        (200, {"access_token": "sk-bad", "token_type": "Bearer"}),
        (200, CAPABILITIES),
        (401, {"error": "unauthorized"}),  # search preflight fails
    ])
    monkeypatch.setattr(onboarding, "request_json", lambda *a, **k: next(responses))
    monkeypatch.setattr(onboarding.time, "sleep", lambda s: None)
    manager = onboarding.OnboardingManager(home.resolve(), "https://memory.example")
    manager.ensure(force=True)
    manager._poll_once()
    assert not (home / ".env").exists() or "SUBSTRATE_API_KEY" not in (home / ".env").read_text()
    state = json.loads((home / "substrate" / "onboarding.json").read_text())
    assert state["phase"] == "failed"
    assert state["error_class"] == "authenticated_health_check_failed"


def test_transient_poll_failures_retry(monkeypatch, tmp_path):
    home = tmp_path
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(onboarding, "active_home", lambda: home.resolve())
    responses = iter([
        _grant(),
        (503, {"error": "unavailable"}),
        (400, {"error": "authorization_pending"}),
    ])
    monkeypatch.setattr(onboarding, "request_json", lambda *a, **k: next(responses))
    monkeypatch.setattr(onboarding.time, "sleep", lambda s: None)
    manager = onboarding.OnboardingManager(home.resolve(), "https://memory.example")
    manager.ensure(force=True)
    assert manager._poll_once() is True  # 503 is transient
    assert manager._poll_once() is True  # pending


def test_verification_url_requires_matching_user_code(monkeypatch):
    assert onboarding.safe_verification_url(
        "https://memory.example",
        "https://memory.example/oauth/device?user_code=ABCD-EFGH",
        "ABCD-EFGH",
    ) == "https://memory.example/oauth/device?user_code=ABCD-EFGH"
    with pytest.raises(onboarding.OnboardingError):
        onboarding.safe_verification_url(
            "https://memory.example",
            "https://memory.example/oauth/device?user_code=WRONG",
            "ABCD-EFGH",
        )
    with pytest.raises(onboarding.OnboardingError):
        onboarding.safe_verification_url(
            "https://memory.example",
            "https://attacker.example/oauth/device?user_code=ABCD-EFGH",
            "ABCD-EFGH",
        )
    with pytest.raises(onboarding.OnboardingError):
        onboarding.safe_verification_url(
            "https://memory.example",
            "https://memory.example/other?user_code=ABCD-EFGH",
            "ABCD-EFGH",
        )


def test_write_env_key_replaces_existing_line_and_keeps_others(tmp_path):
    home = tmp_path
    (home / ".env").write_text(
        "OPENAI_API_KEY=sk-a\nSUBSTRATE_API_KEY=sk-old\nOTHER=1\n", encoding="utf-8"
    )
    onboarding.write_env_key(home, "sk-new")
    text = (home / ".env").read_text(encoding="utf-8")
    assert text.count("SUBSTRATE_API_KEY=") == 1
    assert "SUBSTRATE_API_KEY=sk-new" in text
    assert "OPENAI_API_KEY=sk-a" in text and "OTHER=1" in text


def test_pre_llm_call_without_key_returns_connect_notice(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(onboarding, "active_home", lambda: tmp_path.resolve())
    monkeypatch.setattr(onboarding, "_profile_homes", lambda: [tmp_path.resolve()])
    monkeypatch.setattr(
        plugin.SubstrateClient, "post_json",
        lambda *a, **k: (_ for _ in ()).throw(plugin.ClientError("invalid_config")),
    )
    monkeypatch.setattr(onboarding, "request_json", lambda *a, **k: _grant())
    result = plugin.pre_llm_call("s", "hello", [])
    assert result and result["context"].startswith("<substrate-connect>")
    assert "https://memory.example/oauth/device?user_code=ABCD-EFGH" in result["context"]


def test_agent_name_is_sent_with_the_grant_and_surfaced(monkeypatch, tmp_path):
    home = tmp_path
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("SUBSTRATE_AGENT_NAME", "Henry's Hermes")
    monkeypatch.setattr(onboarding, "active_home", lambda: home.resolve())
    seen = {}

    def fake_request_json(origin, path, *, form=None, **kwargs):
        if path == "/oauth/device_authorization":
            seen["form"] = form
            return (200, {
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri_complete": "https://memory.example/oauth/device?user_code=ABCD-EFGH",
                "expires_in": 300,
                "interval": 1,
                "agent_name": "Henry's Hermes",
            })
        raise AssertionError("unexpected request")

    monkeypatch.setattr(onboarding, "request_json", fake_request_json)
    manager = onboarding.OnboardingManager(home.resolve(), "https://memory.example")
    status = manager.ensure(force=True)
    assert seen["form"]["agent_name"] == "Henry's Hermes"
    assert status["status"] == "authorization_pending"
    assert status["agent_name"] == "Henry's Hermes"
    state = json.loads((home / "substrate" / "onboarding.json").read_text())
    assert state["agent_name"] == "Henry's Hermes"
    # device codes stay private; names are not credentials and may be in state
    assert "device_code" not in state


def test_agent_name_falls_back_to_hermes_profile(monkeypatch, tmp_path):
    monkeypatch.delenv("SUBSTRATE_AGENT_NAME", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "telegram-henry")
    assert onboarding.resolve_agent_name() == "telegram-henry"
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("SUBSTRATE_AGENT_NAME", raising=False)
    assert onboarding.resolve_agent_name() == ""
