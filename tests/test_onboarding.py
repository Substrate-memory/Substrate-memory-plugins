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


def test_transient_oauth_poll_failure_preserves_grant_and_retries(tmp_path: Path):
    class TransientPollAPI(API):
        def poll(self, device_code):
            assert device_code == "device-secret"
            self.poll_count += 1
            if self.poll_count == 1:
                raise OnboardingError("transport_error")
            return {"status": "approved", "access_token": "tenant-secret"}

    store = Store()
    api = TransientPollAPI()
    manager = OnboardingManager(
        tmp_path, api=api, store=store, capability_check=lambda token: {"ok": token}
    )
    manager.begin(mode="device", open_browser=False)

    pending = manager.advance()
    assert pending["phase"] == "authorization_pending"
    assert pending["oauth_poll_failure"] == "transport_error"
    assert store.get("onboarding-device") == "device-secret"
    assert store.get() == ""

    connected = manager.advance()
    assert api.poll_count == 2
    assert connected["phase"] == "awaiting_history_consent"
    assert "oauth_poll_failure" not in connected
    assert store.get() == "tenant-secret"
    assert store.get("onboarding-device") == ""


def test_permanent_oauth_poll_failure_remains_fail_closed(tmp_path: Path):
    class InvalidPollAPI(API):
        def poll(self, device_code):
            raise OnboardingError("invalid_response")

    store = Store()
    manager = OnboardingManager(tmp_path, api=InvalidPollAPI(), store=store)
    manager.begin(mode="device", open_browser=False)

    with pytest.raises(OnboardingError, match="invalid_response"):
        manager.advance()
    assert store.get("onboarding-device") == "device-secret"
    assert store.get() == ""


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


def _valid_hosted_capabilities() -> dict[str, object]:
    return {
        "provider": "substrate_wiki",
        "capture_schema_versions": [2],
        "max_event_bytes": 262_144,
        "history_replay": {
            "protocol": "stream-v2",
            "min_plugin_version": "1.2.0",
            "content_free_completion": True,
            "incremental_windows": True,
            "status_version": 2,
        },
        "entity_memory": {
            "protocol": "entity-wiki-v1",
            "min_plugin_version": "1.3.0",
            "search_endpoint": "/api/v1/hermes/memory/search",
            "canonical_wiki_pages": True,
            "entity_page_type": "entity",
        },
        "entity_quality": {
            "protocol": "entity-quality-v2",
            "min_plugin_version": "1.4.0",
            "memory_card": True,
            "quality_version": 2,
            "canonical_redirects": True,
        },
    }


@pytest.mark.parametrize(
    ("status", "expected"),
    [(503, "http_503"), (400, "invalid_response")],
)
def test_oauth_error_body_cannot_control_failure_category(
    monkeypatch, status: int, expected: str
):
    client = HostedOAuthClient()
    hostile = "server-controlled credential sk_live_must_not_escape"
    monkeypatch.setattr(
        client, "_post", lambda path, values: (status, {"error": hostile})
    )

    with pytest.raises(OnboardingError) as caught:
        client.poll("device-secret")
    assert caught.value.category == expected
    assert hostile not in str(caught.value)


def test_capability_check_retries_one_tenant_cold_start(tmp_path: Path, monkeypatch):
    from substrate_wiki.client import SubstrateAPIError, SubstrateClient
    from substrate_wiki import onboarding

    calls = []

    def capabilities(_client):
        calls.append(True)
        if len(calls) == 1:
            raise SubstrateAPIError("timeout")
        return _valid_hosted_capabilities()

    monkeypatch.setattr(SubstrateClient, "capabilities", capabilities)
    monkeypatch.setattr(onboarding.time, "sleep", lambda _seconds: None)
    store = Store()
    manager = OnboardingManager(tmp_path, api=API(), store=store, opener=lambda _url: True)

    manager.begin(mode="device", open_browser=False)
    result = manager.advance()

    assert len(calls) == 2
    assert result["phase"] == "awaiting_history_consent"
    assert result["authenticated"] is True
    assert store.get() == "tenant-secret"
    assert store.get("onboarding-device") == ""


def test_permanent_capability_failure_is_not_retried_and_is_diagnostic(
    tmp_path: Path, monkeypatch
):
    from substrate_wiki.client import SubstrateAPIError, SubstrateClient

    calls = []

    def capabilities(_client):
        calls.append(True)
        raise SubstrateAPIError("server_upgrade_required")

    monkeypatch.setattr(SubstrateClient, "capabilities", capabilities)
    store = Store()
    manager = OnboardingManager(tmp_path, api=API(), store=store, opener=lambda _url: True)

    manager.begin(mode="device", open_browser=False)
    result = manager.advance()

    assert len(calls) == 1
    assert result["phase"] == "failed"
    assert result["error_class"] == "capability_check_failed"
    assert result["capability_failure"] == "server_upgrade_required"
    assert result["authenticated"] is False
    assert store.get() == ""
