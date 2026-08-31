from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

PLUGIN_ROOT = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(PLUGIN_ROOT))

from substrate_wiki import SubstrateWikiProvider, register  # noqa: E402
from substrate_wiki.client import SubstrateAPIError, SubstrateClient  # noqa: E402
from substrate_wiki.redaction import redact, redact_text  # noqa: E402
from substrate_wiki.spool import DurableSpool  # noqa: E402


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.delivered: list[dict[str, Any]] = []
        self.fail = False
        self.block = threading.Event()
        self.should_block = False
        self.block_timeout = 2.0

    def search(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("search", args, kwargs))
        return {"results": ["hit"]}

    def memory_search(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("memory_search", args, kwargs))
        return {
            "results": [
                {
                    "memory_card": "hit",
                    "path": "entities/project/hit--a1b2c3d4.md",
                    "canonical_path": "entities/project/hit--a1b2c3d4.md",
                    "page_type": "entity",
                    "entity_id": "entity-hit",
                    "entity_type": "project",
                    "quality_version": 2,
                }
            ]
        }

    def read_page(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("read_page", args, kwargs))
        return {"path": args[0]}

    def query_wiki(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("query_wiki", args, kwargs))
        return {"answer": "cited"}

    def ingest(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("ingest", args, kwargs))
        return {"job_id": "job-1"}

    def job_status(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("job_status", args, kwargs))
        return {"status": "succeeded"}

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if self.should_block:
            self.block.wait(self.block_timeout)
        if self.fail:
            raise SubstrateAPIError("transport_error")
        self.delivered.append({"method": method, "path": path, **kwargs})
        return {}


def wait_until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def assert_returns_while_network_is_blocked(
    callback: Callable[[], Any], release_network: threading.Event
) -> None:
    """Prove callback completion without a scheduler-sensitive wall-clock budget."""
    completed = threading.Event()
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            callback()
        except BaseException as exc:  # pragma: no cover - reraised in the test thread
            errors.append(exc)
        finally:
            completed.set()

    caller = threading.Thread(target=invoke, name="blocked-network-caller", daemon=True)
    caller.start()
    returned_before_network = completed.wait(5.0)
    if not returned_before_network:
        release_network.set()
    caller.join(timeout=2.0)
    assert returned_before_network, "callback waited for the blocked network sender"
    if errors:
        raise errors[0]


@pytest.fixture
def configured_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_API_URL", "https://wiki.example.test")
    monkeypatch.setenv("HERMES_API_KEY", "very-secret-hermes-key")


def make_provider(tmp_path: Path, configured_env: None, monkeypatch: pytest.MonkeyPatch) -> tuple[SubstrateWikiProvider, FakeClient]:
    fake = FakeClient()
    monkeypatch.setattr("substrate_wiki.SubstrateClient.from_env", lambda **kwargs: fake)
    provider = SubstrateWikiProvider()
    provider.initialize("session-1", hermes_home=str(tmp_path))
    return provider, fake


def test_registers_provider_and_availability_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Any] = []

    class Context:
        def register_memory_provider(self, provider: Any) -> None:
            captured.append(provider)

    register(Context())
    assert isinstance(captured[0], SubstrateWikiProvider)
    assert captured[0].name == "substrate_wiki"
    assert captured[0].is_available()
    monkeypatch.setenv("HERMES_API_URL", "https://other.example.test")
    assert not captured[0].is_available()
    monkeypatch.setenv("HERMES_API_URL", "https://app.trysubstrate.co")
    assert captured[0].is_available()


@pytest.mark.parametrize(
    ("base_url", "allowed"),
    [
        ("https://wiki.example.test", True),
        ("http://wiki.example.test", False),
        ("http://localhost:8000", True),
        ("http://localhost.:8000", True),
        ("http://127.0.0.1:8000", True),
        ("http://[::1]:8000", True),
        ("http://0.0.0.0:8000", False),
        ("ftp://wiki.example.test", False),
        ("https://user:password@wiki.example.test", False),
    ],
)
def test_client_rejects_insecure_or_credentialed_base_urls(base_url: str, allowed: bool) -> None:
    if allowed:
        assert SubstrateClient(base_url, "key").base_url == base_url.rstrip("/")
    else:
        with pytest.raises(SubstrateAPIError) as caught:
            SubstrateClient(base_url, "secret-bearer-value")
        assert "secret-bearer-value" not in str(caught.value)


def test_availability_rejects_non_loopback_http(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SubstrateWikiProvider()
    monkeypatch.setenv("HERMES_API_KEY", "key")
    monkeypatch.setenv("HERMES_API_URL", "http://wiki.example.test")
    assert not provider.is_available()
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8000")
    assert not provider.is_available()


def test_contract_surface_and_tool_schemas() -> None:
    provider = SubstrateWikiProvider()
    required = {
        "initialize",
        "get_tool_schemas",
        "handle_tool_call",
        "get_config_schema",
        "save_config",
        "shutdown",
        "prefetch",
        "queue_prefetch",
        "sync_turn",
        "on_pre_compress",
        "on_session_end",
    }
    assert all(callable(getattr(provider, name)) for name in required)
    names = {schema["name"] for schema in provider.get_tool_schemas()}
    assert names == {"wiki_search", "wiki_read", "wiki_query", "wiki_ingest", "wiki_job_status"}


def test_client_maps_tools_to_dedicated_machine_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        headers = {"Content-Type": "application/json"}

        def read(self, size: int = -1) -> bytes:
            return b"{}"[:size]

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            calls.append((request, timeout))
            return Response()

    monkeypatch.setattr("substrate_wiki.client.build_opener", lambda *handlers: Opener())
    client = SubstrateClient("https://wiki.example.test", "key")
    client.search("alpha", limit=3)
    client.read_page("topics/a b.md")
    client.query_wiki("Why?", save_as_synthesis=True)
    client.ingest("source", title="Title")
    client.job_status("job/1")

    assert [(call.full_url, call.get_method()) for call, _ in calls] == [
        ("https://wiki.example.test/api/v1/hermes/wiki/search", "POST"),
        ("https://wiki.example.test/api/v1/hermes/wiki/read", "POST"),
        ("https://wiki.example.test/api/v1/hermes/wiki/query", "POST"),
        ("https://wiki.example.test/api/v1/hermes/wiki/ingest", "POST"),
        ("https://wiki.example.test/api/v1/hermes/wiki/job-status?job_id=job%2F1", "GET"),
    ]
    assert json.loads(calls[0][0].data) == {"q": "alpha", "limit": 3}
    assert json.loads(calls[1][0].data) == {"path": "topics/a b.md"}


def test_redirect_is_not_followed_and_authorization_stays_at_origin() -> None:
    destination_headers: list[str | None] = []

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            destination_headers.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        do_POST = do_GET

        def log_message(self, format: str, *args: Any) -> None:
            return None

    destination = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)
    destination_thread = threading.Thread(target=destination.serve_forever, daemon=True)
    destination_thread.start()
    redirect_url = f"http://127.0.0.1:{destination.server_port}/stolen"

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", redirect_url)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, format: str, *args: Any) -> None:
            return None

    origin = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
    origin_thread.start()
    try:
        client = SubstrateClient(
            f"http://127.0.0.1:{origin.server_port}", "redirect-secret-bearer"
        )
        with pytest.raises(SubstrateAPIError, match="http_302") as caught:
            client.search("redirect")
        assert "redirect-secret-bearer" not in str(caught.value)
        assert destination_headers == []
    finally:
        origin.shutdown()
        destination.shutdown()
        origin.server_close()
        destination.server_close()
        origin_thread.join(timeout=2)
        destination_thread.join(timeout=2)


def test_http_errors_hide_response_body_and_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "response-body-secret"

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Any:
            raise HTTPError(
                request.full_url,
                500,
                "Bearer leaked-key",
                Message(),
                BytesIO(secret.encode()),
            )

    monkeypatch.setattr("substrate_wiki.client.build_opener", lambda *handlers: Opener())
    client = SubstrateClient("https://wiki.example.test", "request-bearer-secret")
    with pytest.raises(SubstrateAPIError) as caught:
        client.search("alpha")
    rendered = str(caught.value)
    assert rendered == "http_500"
    assert caught.value.retry_after is None
    assert secret not in rendered
    assert "request-bearer-secret" not in rendered
    assert "leaked-key" not in rendered


def test_http_429_exposes_only_sanitized_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    retry_after = "17"

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Any:
            headers = Message()
            headers["Retry-After"] = retry_after
            headers["X-Secret"] = "must-not-surface"
            raise HTTPError(
                request.full_url,
                429,
                "secret reason",
                headers,
                BytesIO(b"secret response body"),
            )

    monkeypatch.setattr("substrate_wiki.client.build_opener", lambda *handlers: Opener())
    client = SubstrateClient("https://wiki.example.test", "request-secret")
    with pytest.raises(SubstrateAPIError) as caught:
        client.search("alpha")
    assert str(caught.value) == "http_429"
    assert caught.value.retry_after == 17
    assert "must-not-surface" not in str(caught.value)
    assert "request-secret" not in str(caught.value)


def test_retry_after_http_date_and_malformed_values() -> None:
    from substrate_wiki.client import _retry_after_seconds

    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    future = format_datetime(now + timedelta(seconds=45), usegmt=True)
    assert _retry_after_seconds({"Retry-After": future}, now=now) == 45
    assert _retry_after_seconds({"Retry-After": "not-a-delay"}, now=now) is None
    assert _retry_after_seconds({"Retry-After": "-1"}, now=now) is None


def test_tools_route_to_client(tmp_path: Path, configured_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    provider, fake = make_provider(tmp_path, configured_env, monkeypatch)
    try:
        assert json.loads(provider.handle_tool_call("wiki_search", {"query": "alpha", "limit": 99})) == {"results": ["hit"]}
        assert json.loads(provider.handle_tool_call("wiki_read", {"path": "topics/a.md"})) == {"path": "topics/a.md"}
        assert json.loads(provider.handle_tool_call("wiki_query", {"question": "Why?"})) == {"answer": "cited"}
        assert json.loads(provider.handle_tool_call("wiki_ingest", {"content": "source"})) == {"job_id": "job-1"}
        assert json.loads(provider.handle_tool_call("wiki_job_status", {"job_id": "job-1"})) == {"status": "succeeded"}
        assert fake.calls[0] == ("search", ("alpha",), {"limit": 25})
        assert json.loads(provider.handle_tool_call("wiki_read", {})) == {"error": "invalid_arguments"}
        assert "error" in json.loads(provider.handle_tool_call("not_a_tool", {}))
    finally:
        provider.shutdown()


def test_config_state_stays_under_hermes_home_and_excludes_secrets(
    tmp_path: Path, configured_env: None
) -> None:
    provider = SubstrateWikiProvider()
    provider.save_config(
        {
            "api_url": "https://must-not-be-saved.example",
            "api_key": "must-not-be-saved",
            "spool_max_items": 12,
        },
        str(tmp_path),
    )
    path = tmp_path / "substrate_wiki" / "config.json"
    values = json.loads(path.read_text(encoding="utf-8"))
    assert values["spool_max_items"] == 12
    assert values["api_url"] == "https://app.trysubstrate.co"
    assert "api_key" not in values
    assert '"api_key"' not in path.read_text(encoding="utf-8")
    assert provider.get_config_schema() == []


def test_sync_turn_returns_without_waiting_for_network_and_redacts(
    tmp_path: Path, configured_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, fake = make_provider(tmp_path, configured_env, monkeypatch)
    fake.should_block = True
    fake.block_timeout = 30.0
    fake_bearer = "abcdefghijkl" + "mnop"
    authorization_header = ": ".join(("Authorization", "Bearer " + fake_bearer))
    assert_returns_while_network_is_blocked(
        lambda: provider.sync_turn(
            authorization_header,
            "api_key=plain-secret and very-secret-hermes-key",
        ),
        fake.block,
    )
    fake.block.set()
    wait_until(lambda: len(fake.delivered) == 1)
    event = fake.delivered[0]["body"]
    rendered = json.dumps(event)
    assert fake_bearer not in rendered
    assert "plain-secret" not in rendered
    assert "very-secret-hermes-key" not in rendered
    assert rendered.count("[REDACTED]") >= 1
    assert fake.delivered[0]["idempotency_key"] == event["event_id"]
    provider.shutdown()


def test_lifecycle_hooks_return_without_waiting_for_network(
    tmp_path: Path, configured_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, fake = make_provider(tmp_path, configured_env, monkeypatch)
    fake.should_block = True
    fake.block_timeout = 30.0
    try:
        assert_returns_while_network_is_blocked(
            lambda: (
                provider.on_pre_compress([{"role": "user", "content": "x"}]),
                provider.on_session_end([{"role": "assistant", "content": "y"}]),
                provider.on_memory_write("write", "MEMORY.md", "candidate"),
            ),
            fake.block,
        )
        fake.block.set()
        assert fake.delivered == []
    finally:
        fake.block.set()
        provider.shutdown()


def test_offline_events_spool_and_replay_oldest_first(
    tmp_path: Path, configured_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, fake = make_provider(tmp_path, configured_env, monkeypatch)
    fake.fail = True
    provider.sync_turn("first", "answer one")
    provider.sync_turn("second", "answer two")
    spool_dir = tmp_path / "substrate_wiki" / "spool"
    wait_until(lambda: len(list(spool_dir.glob("*.json"))) >= 1)
    fake.fail = False
    provider._wake.set()
    wait_until(lambda: len(fake.delivered) == 2, timeout=4)
    assert [call["body"]["messages"][0]["content"] for call in fake.delivered] == ["first", "second"]
    assert len({call["idempotency_key"] for call in fake.delivered}) == 2
    wait_until(lambda: not list(spool_dir.glob("*.json")))
    provider.shutdown()


def test_spool_is_bounded_and_discards_oldest(tmp_path: Path) -> None:
    spool = DurableSpool(tmp_path / "spool", max_items=2, max_bytes=4096)
    spool.append({"n": 1})
    time.sleep(0.001)
    spool.append({"n": 2})
    time.sleep(0.001)
    spool.append({"n": 3})
    files = sorted((tmp_path / "spool").glob("*.json"))
    assert len(files) == 2
    assert [json.loads(path.read_text(encoding="utf-8"))["n"] for path in files] == [2, 3]


def test_prefetch_is_cached_and_lifecycle_hooks_are_delivered(
    tmp_path: Path, configured_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, fake = make_provider(tmp_path, configured_env, monkeypatch)
    try:
        assert provider.prefetch("topic") == ""
        provider.queue_prefetch("topic")
        wait_until(lambda: provider.prefetch("topic") != "")
        assert "entities/project/hit--a1b2c3d4.md" in provider.prefetch("topic")
        provider.on_pre_compress([{"role": "user", "content": "x"}])
        provider.on_session_end([{"role": "assistant", "content": "y"}])
        provider.on_memory_write("write", "MEMORY.md", "candidate")
        assert fake.delivered == []
    finally:
        provider.shutdown()


def test_recursive_redaction_handles_headers_patterns_and_exact_secrets() -> None:
    value = {
        "Authorization": "Bearer top-secret",
        "nested": ["password=hunter2", "exact-value", {"api_key": "xyz"}],
    }
    rendered = json.dumps(redact(value, ("exact-value",)))
    assert "top-secret" not in rendered
    assert "hunter2" not in rendered
    assert "exact-value" not in rendered
    assert "xyz" not in rendered


def test_redaction_covers_structured_partial_and_url_credentials() -> None:
    stripe_marker = "«redacted:" + "sk" + "_live_…»"
    samples = {
        "json": '{"client_secret":"fake-high-entropy-value-123456"}',
        "yaml": "refresh_token: fake-refresh-value-123456",
        "shell": "export SIGNING_KEY='fake-signing-value-123456'",
        "url": "https://demo-user:fake-pass-123456@example.test/path",
        "provider": "«redacted:github_pat_…»",
        "masked_provider": "sk-••••••••1234",
        "stripe_provider": stripe_marker,
        "jwt": "eyJfak...re12",
        "partial": "api_key=«redacted:sk-…»",
    }

    rendered = json.dumps(redact(samples, ()))

    for unsafe in (
        "fake-high-entropy-value-123456",
        "fake-refresh-value-123456",
        "fake-signing-value-123456",
        "fake-pass-123456",
        "«redacted:github_pat_…»",
        "sk-••••••••1234",
        stripe_marker,

        "sk-fakepartial123456",
    ):
        assert unsafe not in rendered
    assert redact_text("skills remain useful", ()) == "skills remain useful"


def test_redaction_covers_signed_urls_non_http_userinfo_and_partial_private_keys() -> None:
    samples = {
        "database": "postgresql://db-user:databasePassword_987654321@db.invalid/app",
        "sas": (
            "https://blob.invalid/item?sv=2023-11-03&sp=r"
            "&sig=sasSignature_987654321"
        ),
        "aws": (
            "https://bucket.invalid/item?X-Amz-Credential="
            "AKIAEXAMPLEONLY1234%2Fscope&X-Amz-Security-Token="
            "temporarySession_987654321&X-Amz-Signature="
            "0123456789abcdef0123456789abcdef"
        ),
        "private_key": (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAAexampleonly987654321"
        ),
    }

    rendered = json.dumps(redact(samples, ()))

    for unsafe in (
        "db-user",
        "databasePassword_987654321",
        "sasSignature_987654321",
        "AKIAEXAMPLEONLY1234%2Fscope",
        "temporarySession_987654321",
        "0123456789abcdef0123456789abcdef",
        "b3BlbnNzaC1rZXktdjEAAAAAexampleonly987654321",
    ):
        assert unsafe not in rendered


def test_redaction_preserves_many_message_boundaries() -> None:
    messages = [{"role": "user", "content": "x"} for _ in range(8_200)]

    sanitized = redact(messages, ())

    assert len(sanitized) == len(messages)
    assert sanitized[0] == {"role": "user", "content": "x"}
    assert sanitized[-1] == {"role": "user", "content": "x"}


def test_redaction_bounds_pathological_container_amplification() -> None:
    values = [{"value": index} for index in range(20_000)]

    sanitized = redact(values, ())

    assert len(sanitized) == 16_384
    assert sanitized[-1] == {"value": 16_383}


def test_hosted_custody_refuses_arbitrary_legacy_origin(tmp_path: Path, monkeypatch) -> None:
    from substrate_wiki.client import SubstrateAPIError, SubstrateClient
    from substrate_wiki.credentials import credential_store

    credential_store(tmp_path).put("hosted-tenant-secret")
    monkeypatch.setenv("HERMES_API_URL", "https://attacker.example")
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    with pytest.raises(SubstrateAPIError, match="unsafe_hosted_origin_override"):
        SubstrateClient.from_env(hermes_home=tmp_path, hosted_default=True)
