from __future__ import annotations

import json

import pytest
from pathlib import Path

from substrate_wiki.onboarding import (
    HOSTED_ORIGIN,
    HostedOAuthClient,
    OnboardingError,
    OnboardingManager,
)


class Store:
    backend = "test-vault"
    def __init__(self): self.values = {}
    def get(self, slot="access-token"): return self.values.get(slot, "")
    def put(self, value, slot="access-token"): self.values[slot] = value
    def delete(self, slot="access-token"): self.values.pop(slot, None)


class API:
    def __init__(self): self.poll_count = 0
    def begin(self):
        return {"device_code": "device-secret", "user_code": "ABCD-EFGH",
                "verification_uri": HOSTED_ORIGIN + "/oauth/device",
                "verification_uri_complete": HOSTED_ORIGIN + "/oauth/device?user_code=ABCD-EFGH",
                "expires_in": 600, "interval": 1}
    def poll(self, device_code):
        assert device_code == "device-secret"
        self.poll_count += 1
        return {"status": "approved", "access_token": "tenant-secret"}


def test_device_credentials_never_enter_state_and_consent_decline_keeps_connection(tmp_path: Path):
    store = Store()
    manager = OnboardingManager(
        tmp_path, api=API(), store=store, capability_check=lambda token: {"ok": token},
        import_start=lambda home: (_ for _ in ()).throw(AssertionError("must not import")),
        opener=lambda url: True,
    )
    started = manager.begin(mode="device", open_browser=False)
    assert started["user_code"] == "ABCD-EFGH"
    raw = (tmp_path / "substrate_wiki/onboarding/state.json").read_text()
    assert "device-secret" not in raw and "tenant-secret" not in raw
    connected = manager.advance()
    assert connected["phase"] == "awaiting_history_consent"
    ready = manager.consent_history(False)
    assert ready["phase"] == "ready" and ready["authenticated"]
    state = json.loads((tmp_path / "substrate_wiki/onboarding/state.json").read_text())
    assert state["history_consent"]["decision"] == "declined"


def test_history_approval_starts_exactly_one_durable_job(tmp_path: Path):
    store = Store()
    store.put("tenant-secret")
    calls = []
    manager = OnboardingManager(
        tmp_path, store=store, capability_check=lambda token: {},
        import_start=lambda home: calls.append(home) or {"job_id": "job-1", "complete": False},
    )
    assert manager.begin()["phase"] == "awaiting_history_consent"
    result = manager.consent_history(True)
    assert result["phase"] == "importing"
    assert calls == [tmp_path.resolve()]
    assert manager.status()["phase"] == "importing"


def test_history_consent_is_durable_before_import_launch(tmp_path: Path):
    store = Store()
    store.put("tenant-secret")

    def launch(_home):
        state = json.loads((tmp_path / "substrate_wiki/onboarding/state.json").read_text())
        assert state["history_consent"]["decision"] == "approved"
        assert state["phase"] == "import_starting"
        return {"job_id": "job-1", "complete": False}

    manager = OnboardingManager(
        tmp_path, store=store, capability_check=lambda token: {}, import_start=launch
    )
    manager.begin()
    assert manager.consent_history(True)["phase"] == "importing"


def test_device_run_prints_complete_email_authorization_url(tmp_path: Path, capsys):
    store = Store()
    manager = OnboardingManager(
        tmp_path,
        api=API(),
        store=store,
        capability_check=lambda token: {"ok": token},
        opener=lambda url: True,
    )
    result = manager.run(mode="device", wait=True, open_browser=False, timeout=5)
    assert result["phase"] == "awaiting_history_consent"
    output = capsys.readouterr().err
    assert (
        f"Open {HOSTED_ORIGIN}/oauth/device?user_code=ABCD-EFGH "
        "to sign in by email and connect Hermes"
    ) in output
    assert f"Open {HOSTED_ORIGIN}/oauth/device and enter" not in output


def test_device_response_constructs_complete_url_when_server_omits_it(monkeypatch):
    client = HostedOAuthClient()
    monkeypatch.setattr(
        client,
        "_post",
        lambda path, values: (
            200,
            {
                "device_code": "secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": HOSTED_ORIGIN + "/oauth/device",
                "expires_in": 600,
            },
        ),
    )
    assert client.begin()["verification_uri_complete"] == (
        HOSTED_ORIGIN + "/oauth/device?user_code=ABCD-EFGH"
    )


def test_device_response_rejects_complete_url_for_a_different_code(monkeypatch):
    client = HostedOAuthClient()
    monkeypatch.setattr(
        client,
        "_post",
        lambda path, values: (
            200,
            {
                "device_code": "secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": HOSTED_ORIGIN + "/oauth/device",
                "verification_uri_complete": (
                    HOSTED_ORIGIN + "/oauth/device?user_code=DIFFERENT"
                ),
                "expires_in": 600,
            },
        ),
    )
    with pytest.raises(OnboardingError, match="invalid_response"):
        client.begin()
