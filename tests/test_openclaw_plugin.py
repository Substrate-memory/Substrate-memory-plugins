"""Tests for the OpenClaw Substrate plugin (offline, no network)."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "openclaw"
CORE_DIR = PLUGIN_DIR / "substrate_core"
REF_DIR = Path(__file__).resolve().parents[1] / "plugins" / "substrate"

import _hostload  # noqa: E402

_ld = _hostload.begin("openclaw", [CORE_DIR, Path(__file__).resolve().parents[1] / "plugins"])
vendored_contract = _ld.core("contract")
hosthome = _ld.core("hosthome")
vendored_client = _ld.core("client")
vendored_onboarding = _ld.core("onboarding")
runtime = _ld.core("runtime")
hermes_plugin = _ld.hermes("plugin")
_ld.commit()


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBSTRATE_DEVELOPMENT_MODE", "1")
    monkeypatch.setenv("SUBSTRATE_HOME", str(tmp_path / "oc-home"))
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    monkeypatch.setenv("SUBSTRATE_API_URL", "http://127.0.0.1:1")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_valid():
    manifest = json.loads((PLUGIN_DIR / "openclaw.plugin.json").read_text())
    assert manifest["id"] == "substrate"
    assert manifest["contracts"]["tools"] == [
        "memory_search",
        "memory_expand",
        "memory_evidence",
    ]
    schema = manifest["configSchema"]
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict)
    # No plaintext-secret fallback: device onboarding + 0600 token file is
    # the supported path, so openclaw.json must not carry an apiKey option.
    assert "apiKey" not in json.dumps(schema)
    assert "python" in schema["properties"]
    package = json.loads((PLUGIN_DIR / "package.json").read_text())
    assert package["openclaw"]["extensions"] == ["./index.js"]
    assert package.get("dependencies", {}) == {}
    assert (PLUGIN_DIR / "index.js").exists()


# ---------------------------------------------------------------------------
# Tool schema parity with the Hermes reference plugin
# ---------------------------------------------------------------------------

EXPECTED_SEARCH = {
    "name": "memory_search",
    "description": (
        "Search Substrate memory. "
        "Use the intended action in the query before irreversible operations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to recall or the intended action."},
            "kinds": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

EXPECTED_EXPAND = {
    "name": "memory_expand",
    "description": "Expand a Substrate memory or page handle into its bounded detail.",
    "parameters": {
        "type": "object",
        "properties": {"handle": {"type": "string", "pattern": "^[mp]:[0-9a-f]{8,64}$"}},
        "required": ["handle"],
        "additionalProperties": False,
    },
}

EXPECTED_EVIDENCE = {
    "name": "memory_evidence",
    "description": "Get evidence excerpts for a Substrate memory handle.",
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {"type": "string", "pattern": "^[mp]:[0-9a-f]{8,64}$"},
            "raw": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        },
        "required": ["handle"],
        "additionalProperties": False,
    },
}


def _load_js_tool_schemas() -> dict:
    """Import the actual TOOL_SCHEMAS objects registered by index.js via node."""
    script = (
        "import(url).then((m) => {"
        " console.log(JSON.stringify(m.TOOL_SCHEMAS));"
        "}).catch((e) => { console.error(String(e)); process.exit(1); })"
    )
    target = (PLUGIN_DIR / "index.js").as_uri()
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script.replace("url", json.dumps(target))],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_tool_schemas_match_hermes_reference():
    assert hermes_plugin.MEMORY_SEARCH_SCHEMA == EXPECTED_SEARCH
    assert hermes_plugin.MEMORY_EXPAND_SCHEMA == EXPECTED_EXPAND
    assert hermes_plugin.MEMORY_EVIDENCE_SCHEMA == EXPECTED_EVIDENCE
    js_schemas = _load_js_tool_schemas()
    assert set(js_schemas) == {"memory_search", "memory_expand", "memory_evidence"}
    for tool_name, expected in (
        ("memory_search", EXPECTED_SEARCH),
        ("memory_expand", EXPECTED_EXPAND),
        ("memory_evidence", EXPECTED_EVIDENCE),
    ):
        js_schema = dict(js_schemas[tool_name])
        # `label` is a display-only OpenClaw affordance with no Python
        # counterpart; everything else must deep-equal the Hermes reference.
        js_schema.pop("label", None)
        assert js_schema == expected, tool_name
    assert hermes_plugin.STATIC_MEMORY_PROMPT == runtime.STATIC_MEMORY_PROMPT


def test_static_prompt_matches_js_adapter():
    text = (PLUGIN_DIR / "index.js").read_text()
    start = text.find("const STATIC_MEMORY_PROMPT")
    end = text.find('";', start) + 1
    assert end > start
    block = text[start:end]
    segments = re.findall(r'"((?:[^"\\]|\\.)*)"', block)
    assert len(segments) >= 2
    assert "".join(segments) == runtime.STATIC_MEMORY_PROMPT


def test_js_adapter_is_async_and_secret_free():
    text = (PLUGIN_DIR / "index.js").read_text()
    # Gateway event loop must never block: no spawnSync on the turn/tool path.
    assert "spawnSync" not in text
    # No plaintext-secret config fallback in the adapter.
    assert "apiKey" not in text
    # The `python` manifest key must be wired through to the bridge.
    assert "cfg.python" in text


# ---------------------------------------------------------------------------
# Vendored core fidelity
# ---------------------------------------------------------------------------


def test_vendored_copies_byte_identical():
    for name in (
        "contract.py",
        "ca/isrg-root-x1.pem",
        "ca/isrg-root-x2.pem",
        "contract/envelope-fixtures.json",
    ):
        assert _sha(CORE_DIR / name) == _sha(REF_DIR / name), name
    assert vendored_contract.FIXTURE_SHA256 == _sha(CORE_DIR / "contract/envelope-fixtures.json")


def test_host_home_patch_is_minimal_and_marked():
    # Budgets cover the host-home resolution patch plus the credential-location
    # fix (0600 token-file mirror in onboarding, owner-only <home>/.env
    # fallback in the client): still a small, clearly-marked diff.
    # onboarding.py routes active_home() directly through hosthome (no
    # _profile_homes import, no Hermes walk), which costs a few marked lines.
    budgets = {"client.py": 80, "onboarding.py": 34}
    for name, budget in budgets.items():
        ref_lines = (REF_DIR / name).read_text().splitlines()
        new_lines = (CORE_DIR / name).read_text().splitlines()
        assert "HOST-HOME" in (CORE_DIR / name).read_text()
        changed = [
            line
            for line in difflib.unified_diff(ref_lines, new_lines, n=0)
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
        assert 0 < len(changed) <= budget, (name, len(changed))


def test_wire_constants_unchanged():

    assert vendored_onboarding.CLIENT_ID == "substrate-hermes"
    assert vendored_onboarding.SCOPES == "capture retrieve"
    assert vendored_onboarding.ENV_KEY == "SUBSTRATE_API_KEY"
    assert vendored_client._MAX_TOKEN_BYTES == 16_384
    assert (
        vendored_onboarding.DEFAULT_ORIGIN
        == "https://vm-substrate-ar-01.taile961d2.ts.net:10000"
    )
    assert vendored_contract.CONTRACT_VERSION == 1
    assert vendored_contract.SCHEMA_VERSION == 3


def test_hosthome_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_HOME", str(tmp_path / "custom"))
    assert hosthome.openclaw_home() == (tmp_path / "custom").resolve()
    monkeypatch.delenv("SUBSTRATE_HOME")
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "state"))
    assert hosthome.openclaw_home() == (tmp_path / "state").resolve()


# ---------------------------------------------------------------------------
# Redaction + secret hygiene
# ---------------------------------------------------------------------------


def test_redaction():
    assert runtime._redact_text("Authorization: Bearer short") == "Authorization: [REDACTED]"
    assert runtime._redact_text("api_key=supersecret123") == "api_key=[REDACTED]"
    assert runtime._redact_text("see sk_test1234 here") == "see [REDACTED] here"
    safe = runtime._safe_value({"Authorization": "short", "nested": {"token": "short"}})
    assert safe == {"Authorization": "[REDACTED]", "nested": {"token": "[REDACTED]"}}
    assert "short" not in json.dumps(safe)


def test_no_secret_in_tool_errors(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "testkey123")
    for text in (
        runtime.memory_search({"query": "x"}),
        runtime.memory_expand({"handle": "m:44a1b02e"}),
        runtime.memory_evidence({"handle": "m:44a1b02e"}),
    ):
        assert "testkey123" not in text


# ---------------------------------------------------------------------------
# Fail-closed behavior (offline)
# ---------------------------------------------------------------------------


def test_retrieval_failure_returns_empty_context(monkeypatch):

    class _Dead:
        def post_json(self, *args, **kwargs):
            raise vendored_client.ClientError("transport_error")

    monkeypatch.setattr(runtime.SubstrateClient, "from_env", classmethod(lambda cls: _Dead()))
    assert runtime.get_turn_context("s-1", "hello", []) is None


def test_tool_transport_errors_are_bounded(monkeypatch):

    class _Dead:
        api_key = "testkey123"

        def post_json(self, *args, **kwargs):
            raise vendored_client.ClientError("timeout")

    monkeypatch.setattr(runtime.SubstrateClient, "from_env", classmethod(lambda cls: _Dead()))
    assert json.loads(runtime.memory_search({"query": "hi"})) == {"error": "timeout"}
    assert json.loads(runtime.memory_search({"query": 123})) == {"error": "invalid_request"}
    assert json.loads(runtime.memory_expand({"handle": "nope"})) == {"error": "invalid_request"}
    assert json.loads(runtime.memory_evidence({"handle": "m:44a1b02e", "raw": "yes"})) == {
        "error": "invalid_request"
    }


def test_onboarding_notice_surfaces_clickable_url():
    status = {
        "status": "authorization_pending",
        "verification_uri_complete": "https://memory.example/oauth/device?user_code=X",
        "user_code": "X",
        "expires_in": 900,
        "agent_name": "Test Agent",
    }
    notice = runtime._onboarding_notice(status)
    assert "https://memory.example/oauth/device?user_code=X" in notice
    assert "paste" in notice


def test_tool_onboarding_result_surfaces_url(monkeypatch):

    class _NoKey:
        api_key = ""

    monkeypatch.setattr(
        runtime.SubstrateClient, "from_env", classmethod(lambda cls: _NoKey())
    )
    monkeypatch.setattr(
        vendored_onboarding,
        "ensure_started",
        lambda **kwargs: {
            "status": "authorization_pending",
            "verification_uri_complete": "https://memory.example/oauth/device?user_code=Y",
            "user_code": "Y",
            "expires_in": 900,
            "agent_name": "Agent",
        },
    )
    # runtime imported onboarding at module load; patch the same object.
    monkeypatch.setattr(
        runtime.onboarding, "ensure_started", vendored_onboarding.ensure_started
    )
    result = json.loads(runtime.memory_search({"query": "hello"}))
    assert result["status"] == "authorization_required"
    assert result["verification_uri_complete"] == "https://memory.example/oauth/device?user_code=Y"


# ---------------------------------------------------------------------------
# Capture + session envelopes validate
# ---------------------------------------------------------------------------


def _sample_history():
    return [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current question"},
    ]


def test_capture_envelope_validates():
    envelope = runtime._capture_envelope(
        "current question",
        "current answer",
        session_id="session-1",
        messages=_sample_history(),
        sender_id="person-1",
    )
    assert envelope is not None
    vendored_contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    assert envelope["kind"] == "capture_turn"
    assert envelope["offset"]["end"] > envelope["offset"]["start"]


def test_session_envelope_validates():
    envelope = runtime._session_envelope("session-1", "end", platform="openclaw")
    assert envelope is not None
    vendored_contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    assert envelope["payload"]["session_complete"] is True


def test_end_session_dedupes_and_never_raises(monkeypatch):
    seen = []
    monkeypatch.setattr(
        runtime._CAPTURE_WORKER, "enqueue", lambda envelope: seen.append(envelope)
    )
    runtime._SENT_SESSIONS.clear()
    runtime.end_session("s-dedupe", boundary="end", platform="openclaw")
    runtime.end_session("s-dedupe", boundary="end", platform="openclaw")
    assert len(seen) == 1
    runtime.end_session("", boundary="end")
    runtime.end_session("s-bad", boundary="nope")


# ---------------------------------------------------------------------------
# Bridge CLI (offline subprocess, fail-closed)
# ---------------------------------------------------------------------------


def _bridge(command, payload, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "testkey123")
    proc = subprocess.run(
        [sys.executable, str(CORE_DIR / "bridge.py")],
        input=json.dumps({"command": command, "payload": payload}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    response = json.loads(proc.stdout)
    assert "testkey123" not in proc.stdout
    return response


def test_bridge_turn_context_offline_returns_empty(monkeypatch):
    response = _bridge(
        "turn_context",
        {"session_id": "s-1", "user_message": "hi", "conversation_history": []},
        monkeypatch,
    )
    assert response == {"ok": True, "context": None}


def test_bridge_tool_errors_bounded_and_secret_free(monkeypatch):
    response = _bridge("search", {"args": {"query": "hi"}}, monkeypatch)
    assert response == {"ok": False, "error": "transport_error"}
    response = _bridge("search", {"args": {"query": 123}}, monkeypatch)
    assert response == {"ok": False, "error": "invalid_request"}
    response = _bridge("expand", {"args": {"handle": "m:44a1b02e"}}, monkeypatch)
    assert response == {"ok": False, "error": "transport_error"}
    response = _bridge("bogus", {}, monkeypatch)
    assert response == {"ok": False, "error": "invalid_request"}


def test_bridge_capture_and_session_offline(monkeypatch):
    response = _bridge(
        "capture",
        {
            "user_content": "hi",
            "assistant_content": "hello",
            "session_id": "s-1",
            "messages": _sample_history(),
        },
        monkeypatch,
    )
    assert response == {"ok": True, "posted": False}
    response = _bridge(
        "session", {"session_id": "s-1", "boundary": "end", "platform": "openclaw"},
        monkeypatch,
    )
    assert response == {"ok": True, "posted": False}
    response = _bridge("session", {"session_id": "s-1", "boundary": "nope"}, monkeypatch)
    assert response == {"ok": False, "error": "invalid_request"}


def test_bridge_rejects_bad_framing(monkeypatch):
    proc = subprocess.run(
        [sys.executable, str(CORE_DIR / "bridge.py")],
        input="not json",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2


# --- credential-location regression (live parity defect) ---------------------

def _oc_roundtrip_token(suffix):
    return "roundtrip-oc-" + suffix + "-" + "0123456789abcdef" * 2


def test_onboarding_persists_token_file_found_by_client(monkeypatch, tmp_path, capsys, caplog):
    home = tmp_path / "cred-home"
    monkeypatch.setenv("SUBSTRATE_HOME", str(home))
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    token = _oc_roundtrip_token("file")
    monkeypatch.setattr(vendored_onboarding, "token_is_valid", lambda origin, tok: tok == token)
    vendored_onboarding._save_state(home, {"phase": "pending"})
    manager = vendored_onboarding.OnboardingManager(home, "https://127.0.0.1:9/")
    assert manager._complete({"access_token": token, "token_type": "Bearer"}) is False
    credential = home / "substrate" / "credentials" / "access-token"
    env_file = home / ".env"
    assert credential.is_file() and env_file.is_file()
    assert stat.S_IMODE(os.stat(credential).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(credential.parent).st_mode) == 0o700
    assert credential.read_text(encoding="utf-8").strip() == token
    os.environ.pop("SUBSTRATE_API_KEY", None)
    try:
        assert vendored_client._stored_api_key() == token
    finally:
        os.environ.pop("SUBSTRATE_API_KEY", None)
    state = vendored_onboarding._load_state(home)
    assert token not in json.dumps(state)
    assert token not in json.dumps(manager.describe(state))
    captured = capsys.readouterr()
    assert token not in captured.out and token not in captured.err
    assert token not in caplog.text


def test_stored_key_falls_back_to_dotenv(monkeypatch, tmp_path, capsys, caplog):
    home = tmp_path / "env-home"
    monkeypatch.setenv("SUBSTRATE_HOME", str(home))
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    token = _oc_roundtrip_token("env")
    vendored_onboarding.write_env_key(home, token)
    credential = home / "substrate" / "credentials" / "access-token"
    if credential.exists():
        credential.unlink()
    assert vendored_client._stored_api_key() == token
    env_file = home / ".env"
    os.chmod(env_file, 0o640)
    assert vendored_client._stored_api_key() == ""
    os.chmod(env_file, 0o600)
    assert vendored_client._stored_api_key() == token
    env_file.unlink()
    real = home / "real.env"
    real.write_text("SUBSTRATE_API_KEY=" + token + "\n", encoding="utf-8")
    os.chmod(real, 0o600)
    os.symlink(real, env_file)
    assert vendored_client._stored_api_key() == ""
    env_file.unlink()
    vendored_onboarding.write_env_key(home, token)
    assert vendored_client._stored_api_key() == token
    captured = capsys.readouterr()
    assert token not in captured.out and token not in captured.err
    assert token not in caplog.text
