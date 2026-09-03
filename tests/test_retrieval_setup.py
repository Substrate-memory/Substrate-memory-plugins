"""Setup, credential migration, and device-authorization tests."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from substrate import contract
from substrate import setup


@pytest.fixture(autouse=True)
def _allow_test_origins(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_DEVELOPMENT_MODE", "1")


def test_connect_migrates_existing_valid_credential(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setenv("SUBSTRATE_API_KEY", "legacy-token")
    monkeypatch.setattr(setup.onboarding, "token_is_valid", lambda origin, token: token == "legacy-token")
    monkeypatch.setattr(setup.onboarding, "_save_origin", lambda home, origin: saved.update(
        home=home, origin=origin
    ))

    result = setup.connect(tmp_path, "https://memory.example")
    assert result == {
        "status": "ready",
        "credential": "existing",
        "origin": "https://memory.example",
    }
    assert saved == {"home": tmp_path, "origin": "https://memory.example"}


def test_connect_completes_device_authorization_without_exposing_token(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    monkeypatch.setattr(setup.onboarding, "_stored_api_key", lambda: "")
    pending = {
        "status": "authorization_pending",
        "verification_uri_complete": "https://memory.example/oauth/device?user_code=ABCD-EFGH",
        "user_code": "ABCD-EFGH",
        "expires_in": 300,
    }
    manager = setup.onboarding.OnboardingManager(tmp_path, "https://memory.example")
    monkeypatch.setattr(setup.onboarding, "OnboardingManager", lambda home, origin: manager)
    monkeypatch.setattr(manager, "ensure", lambda *, force=False: pending)
    monkeypatch.setattr(setup.onboarding, "_load_state", lambda home: {"phase": "connected"})

    result = setup.connect(tmp_path, "https://memory.example")
    output = capsys.readouterr().out
    assert result["status"] == "ready"
    assert "https://memory.example/oauth/device?user_code=ABCD-EFGH" in output
    assert "access-secret" not in output
    assert "device-secret" not in output


def test_connect_reports_only_the_safe_error_class(monkeypatch, tmp_path):
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    monkeypatch.setattr(setup.onboarding, "_stored_api_key", lambda: "")
    manager = setup.onboarding.OnboardingManager(tmp_path, "https://memory.example")
    monkeypatch.setattr(setup.onboarding, "OnboardingManager", lambda home, origin: manager)
    monkeypatch.setattr(manager, "ensure", lambda *, force=False: {
        "status": "failed",
        "error_class": "authorization_failed",
    })

    with pytest.raises(setup.SetupError, match="^authorization_failed$") as failure:
        setup.connect(tmp_path, "https://memory.example")
    assert "server-secret-detail" not in str(failure.value)


def test_secure_save_uses_owner_only_regular_files(tmp_path):
    setup._secure_write(tmp_path / "token", "secret")
    token = tmp_path / "token"
    assert token.read_text(encoding="utf-8") == "secret"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600


def test_origin_and_verification_url_fail_closed(monkeypatch):
    monkeypatch.delenv("SUBSTRATE_DEVELOPMENT_MODE", raising=False)
    with pytest.raises(setup.SetupError):
        setup._origin("http://memory.example")
    with pytest.raises(setup.SetupError):
        setup._origin("https://memory.example/path")
    with pytest.raises(setup.SetupError):
        setup._safe_verification_url(
            "https://memory.example",
            "https://attacker.example/oauth/device?user_code=ABCD",
            "ABCD",
        )
    with pytest.raises(setup.SetupError):
        setup._safe_verification_url(
            "https://memory.example",
            "https://memory.example/oauth/device?user_code=WRONG",
            "ABCD",
        )


def test_validate_token_accepts_published_v5_capabilities_shape(monkeypatch):
    value = {
        "contract_version": 1,
        "provider": "substrate",
        "server_commit": "main",
        "limits": dict(contract.LIMITS),
        "actions": sorted(contract.ACTIONS),
        "kinds": sorted(contract.KINDS),
        "tenant": {"tenant_id": "agent", "brief_version": 0},
    }
    responses = iter([
        (200, value),
        (200, {"contract_version": 1, "results": []}),
    ])
    monkeypatch.setattr(setup, "_request", lambda *args, **kwargs: next(responses))
    assert setup._validate_token("https://memory.example", "token") is True

    value["provider"] = "wrong"
    monkeypatch.setattr(setup, "_request", lambda *args, **kwargs: (200, value))
    assert setup._validate_token("https://memory.example", "token") is False
