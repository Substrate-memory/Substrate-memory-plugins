from __future__ import annotations

import json
import os
import queue
import stat
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(PLUGIN_ROOT))

from substrate_wiki import SubstrateWikiProvider  # noqa: E402
from substrate_wiki.client import SubstrateAPIError, SubstrateClient  # noqa: E402
from substrate_wiki.spool import DurableSpool  # noqa: E402


class FakeClient:
    def __init__(self) -> None:
        self.searches: list[tuple[str, int]] = []
        self.memory_searches: list[tuple[str, int, dict[str, Any]]] = []
        self.queries: list[tuple[str, bool]] = []
        self.delivered: list[dict[str, Any]] = []
        self.fail = False
        self.fail_category = "transport_error"
        self.request_started = threading.Event()
        self.block = threading.Event()
        self.should_block = False
        self.block_timeout = 2.0

    def search(self, query: str, *, limit: int = 8) -> dict[str, Any]:
        self.searches.append((query, limit))
        return {"results": [{"text": "cached", "citation": "topics/cached.md"}]}

    def memory_search(
        self, query: str, *, limit: int = 8, scope: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.memory_searches.append((query, limit, scope or {}))
        return {
            "results": [
                {
                    "memory_card": "cached",
                    "path": "entities/project/cached--a1b2c3d4.md",
                    "canonical_path": "entities/project/cached--a1b2c3d4.md",
                    "page_type": "entity",
                    "entity_id": "entity-cached",
                    "entity_type": "project",
                    "quality_version": 2,
                }
            ]
        }

    def read_page(self, path: str) -> dict[str, Any]:
        return {"path": path}

    def query_wiki(self, question: str, *, save_as_synthesis: bool = False) -> dict[str, Any]:
        self.queries.append((question, save_as_synthesis))
        return {"answer": question, "citations": ["topics/a.md"]}

    def ingest(self, content: str, **kwargs: Any) -> dict[str, Any]:
        return {"job_id": "job-1"}

    def job_status(self, job_id: str) -> dict[str, Any]:
        return {"job_id": job_id, "status": "done"}

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.request_started.set()
        if self.should_block:
            self.block.wait(self.block_timeout)
        if self.fail:
            raise SubstrateAPIError(self.fail_category)
        self.delivered.append({"method": method, "path": path, **kwargs})
        return {}


def wait_until(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


@pytest.fixture
def provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SubstrateWikiProvider, FakeClient]:
    monkeypatch.setenv("HERMES_API_URL", "https://wiki.example.test")
    monkeypatch.setenv("HERMES_API_KEY", "secret-value")
    fake = FakeClient()
    monkeypatch.setattr("substrate_wiki.SubstrateClient.from_env", lambda **kwargs: fake)
    instance = SubstrateWikiProvider()
    instance.initialize("session-a", hermes_home=str(tmp_path))
    yield instance, fake
    instance.shutdown()


def test_v0182_identity_direct_schemas_and_json_string_results(
    provider: tuple[SubstrateWikiProvider, FakeClient],
) -> None:
    instance, _ = provider
    assert instance.name == "substrate_wiki"
    schemas = instance.get_tool_schemas()
    assert {schema["name"] for schema in schemas} == {
        "wiki_search",
        "wiki_read",
        "wiki_query",
        "wiki_ingest",
        "wiki_job_status",
    }
    assert all("function" not in schema for schema in schemas)
    result = instance.handle_tool_call("wiki_search", {"query": "x", "limit": 99})
    assert isinstance(result, str)
    assert json.loads(result)["results"][0]["citation"] == "topics/cached.md"
    assert json.loads(instance.handle_tool_call("wiki_read", {})) == {"error": "invalid_arguments"}


def test_config_persists_url_but_not_key_and_environment_url_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = SubstrateWikiProvider()
    instance.save_config(
        {"api_url": "https://config.example.test", "api_key": "must-not-persist"}, str(tmp_path)
    )
    config = tmp_path / "substrate_wiki" / "config.json"
    raw = config.read_text(encoding="utf-8")
    assert "https://app.trysubstrate.co" in raw
    assert "must-not-persist" not in raw
    monkeypatch.setenv("HERMES_API_URL", "https://env.example.test")
    monkeypatch.setenv("HERMES_API_KEY", "environment-only")
    with pytest.raises(SubstrateAPIError, match="unsafe_hosted_origin_override"):
        instance.initialize("s", hermes_home=str(tmp_path))


def test_prefetch_is_cache_only_and_queue_uses_one_worker(
    provider: tuple[SubstrateWikiProvider, FakeClient],
) -> None:
    instance, fake = provider
    worker = instance._prefetch_worker
    assert instance.prefetch("topic", session_id="s") == ""
    assert fake.memory_searches == []
    instance.queue_prefetch("topic", session_id="session-a")
    wait_until(lambda: len(fake.memory_searches) == 1)
    cached = instance.prefetch("topic", session_id="session-a")
    assert "entities/project/cached--a1b2c3d4.md" in cached
    assert instance.prefetch("different follow-up", session_id="session-a") == cached
    assert instance.prefetch("different follow-up", session_id="other-session") == ""
    for number in range(10):
        instance.queue_prefetch(f"q-{number}", session_id="s")
    assert instance._prefetch_worker is worker


def test_prefetch_exact_query_precedes_latest_session_fallback(
    provider: tuple[SubstrateWikiProvider, FakeClient],
) -> None:
    instance, _ = provider
    now = time.monotonic() + 60
    exact_key = instance._cache_key("repeat", "session-a")
    session_key = instance._session_cache_key("session-a")
    with instance._cache_lock:
        instance._prefetch_cache[exact_key] = (now, "exact")
        instance._latest_prefetch_cache[session_key] = (now, "latest")
    assert instance.prefetch("repeat", session_id="session-a") == "exact"
    assert instance.prefetch("new", session_id="session-a") == "latest"


def test_only_completed_dialogue_turns_are_uploaded(
    provider: tuple[SubstrateWikiProvider, FakeClient],
) -> None:
    instance, fake = provider
    messages = [{"role": "user", "content": "first"}]
    assert instance.on_pre_compress(messages) == ""
    instance.on_pre_compress(messages)
    instance.on_memory_write(
        "write",
        "MEMORY.md",
        "candidate",
        metadata={"session_id": "memory-session", "provenance": "native", "secret": "omit"},
    )
    instance.sync_turn("ignored", "ignored", runtime_context={"role": "subagent"})
    instance.on_session_switch(
        new_session_id="session-b", parent_session_id="parent", reset=True, rewound=False
    )
    instance.sync_turn("new", "answer")
    wait_until(lambda: len(fake.delivered) == 1)
    bodies = [call["body"] for call in fake.delivered]
    assert bodies[0]["session_id"] == "session-b"
    assert bodies[0]["messages"] == [
        {"index": 0, "role": "user", "content": "new"},
        {"index": 1, "role": "assistant", "content": "answer"},
    ]
    assert set(bodies[0]) == {
        "schema_version", "event_id", "kind", "session_id", "created_at", "messages"
    }


def test_sender_marks_each_in_memory_item_done_once_on_success_and_failure(
    provider: tuple[SubstrateWikiProvider, FakeClient],
) -> None:
    instance, fake = provider
    fake.fail = True
    instance.sync_turn("one", "answer")
    wait_until(lambda: instance.status_snapshot()["counters"]["delivery_failed"] >= 1)
    assert len(instance._spool) == 1
    fake.fail = False
    instance._wake.set()
    wait_until(lambda: len(fake.delivered) == 1)
    assert len(instance._spool) == 0


def test_spool_permissions_escape_symlink_and_corruption(tmp_path: Path) -> None:
    spool = DurableSpool(tmp_path / "spool", max_items=4, max_bytes=4096)
    path = spool.append({"secret": "redacted", "n": 1})
    if os.name == "posix":
        assert stat.S_IMODE(spool.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        spool.load(outside)
    path.write_bytes(b"\xffnot-json")
    with pytest.raises(ValueError, match="corrupt"):
        spool.load(path)
    spool.quarantine(path)
    assert spool.oldest() is None
    assert len(list((spool.root / "corrupt").glob("*.bad"))) == 1
    if hasattr(os, "symlink"):
        link = spool.root / "99999999999999999999-link.json"
        try:
            link.symlink_to(outside)
        except OSError:
            pass
        else:
            assert spool.oldest() is None


def test_client_rejects_wrong_content_type_oversize_and_non_json_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Headers(dict[str, str]):
        pass

    class Response:
        def __init__(self, body: bytes, content_type: str) -> None:
            self.body = body
            self.headers = Headers({"Content-Type": content_type})

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return self.body[:size]

    class Opener:
        response: Response

        def open(self, request: Any, *, timeout: float) -> Response:
            return self.response

    opener = Opener()
    monkeypatch.setattr("substrate_wiki.client.build_opener", lambda *handlers: opener)
    client = SubstrateClient("https://wiki.example.test", "secret", max_response_bytes=1024)
    opener.response = Response(b"{}", "text/plain")
    with pytest.raises(SubstrateAPIError, match="invalid_content_type"):
        client.search("x")
    opener.response = Response(b"{" + b"x" * 2000, "application/json")
    with pytest.raises(SubstrateAPIError, match="response_too_large"):
        client.search("x")
    opener.response = Response(b'"scalar"', "application/json; charset=utf-8")
    with pytest.raises(SubstrateAPIError, match="invalid_response"):
        client.search("x")
    oversized = {
        "results": [
            {"title": "x" * 5000, "text": "y" * 70000, "citation": "c" * 5000, "extra": "omit"}
            for _ in range(40)
        ]
    }
    opener.response = Response(json.dumps(oversized).encode(), "application/json")
    client.max_response_bytes = 8 * 1024 * 1024
    shaped = client.search("x")
    assert len(shaped["results"]) == 25
    assert len(shaped["results"][0]["title"]) == 2048
    assert len(shaped["results"][0]["text"]) == 65536
    assert "extra" not in shaped["results"][0]


def test_saved_url_is_available_to_fresh_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = SubstrateWikiProvider()
    provider.save_config({"api_url": "https://saved.example.test"}, str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.setenv("HERMES_API_KEY", "env-only-key")
    assert SubstrateWikiProvider().is_available()


def test_agent_context_scope_and_initialized_non_primary_suppression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_API_URL", "https://wiki.example.test")
    monkeypatch.setenv("HERMES_API_KEY", "key")
    fake = FakeClient()
    monkeypatch.setattr("substrate_wiki.SubstrateClient.from_env", lambda **kwargs: fake)
    primary = SubstrateWikiProvider()
    primary.initialize(
        "s",
        hermes_home=str(tmp_path / "primary"),
        agent_context="primary",
        agent_identity="Main",
        agent_workspace="workspace",
        user_id="user",
        platform="cli",
        agent_id="agent-1",
    )
    primary.sync_turn("u", "a")
    wait_until(lambda: len(fake.delivered) == 1)
    body = fake.delivered[0]["body"]
    assert body["session_id"] == "s"
    assert not ({"scope", "agent_id", "agent_identity", "agent_workspace", "user_id", "platform"} & set(body))
    snapshot = primary.status_snapshot()
    assert "session" not in json.dumps(snapshot).lower()
    assert "https://" not in json.dumps(snapshot)
    primary.shutdown()

    suppressed = SubstrateWikiProvider()
    suppressed.initialize("s", hermes_home=str(tmp_path / "cron"), agent_context="cron")
    suppressed.sync_turn("u", "a")
    suppressed.on_memory_write("write", "x", "y")
    assert suppressed.status_snapshot()["counters"]["suppressed"] == 2
    assert suppressed._events.empty()
    suppressed.shutdown()


def test_sync_turn_uses_only_user_and_assistant_arguments(
    provider: tuple[SubstrateWikiProvider, FakeClient],
) -> None:
    instance, fake = provider
    instance.sync_turn("repeat-user", "repeat-assistant")
    wait_until(lambda: len(fake.delivered) == 1)
    first = fake.delivered[0]["body"]
    assert first["messages"] == [
        {"index": 0, "role": "user", "content": "repeat-user"},
        {"index": 1, "role": "assistant", "content": "repeat-assistant"},
    ]
    instance.sync_turn("next-user", "next-assistant")
    wait_until(lambda: len(fake.delivered) == 2)
    assert [message["index"] for message in fake.delivered[1]["body"]["messages"]] == [2, 3]


def test_late_prefetch_from_old_session_is_not_published(
    provider: tuple[SubstrateWikiProvider, FakeClient],
) -> None:
    instance, fake = provider
    gate = threading.Event()

    def delayed(
        query: str, *, limit: int = 8, scope: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        del query, limit, scope
        gate.wait(2)
        return {
            "results": [
                {
                    "text": "old",
                    "path": "entities/person/old--deadbeef.md",
                    "page_type": "entity",
                    "entity_type": "person",
                }
            ]
        }

    fake.memory_search = delayed  # type: ignore[method-assign]
    instance.queue_prefetch("old", session_id="session-a")
    instance.on_session_switch(new_session_id="session-b")
    gate.set()
    wait_until(lambda: instance._prefetch_jobs.unfinished_tasks == 0)
    assert instance.prefetch("old", session_id="session-a") == ""


def test_capture_is_json_safe_and_does_not_use_custom_repr(
    provider: tuple[SubstrateWikiProvider, FakeClient], tmp_path: Path
) -> None:
    instance, fake = provider

    class SecretObject:
        def __repr__(self) -> str:
            return "leaked-secret-repr"

    instance.sync_turn(
        {"bytes": b"secret", "path": tmp_path / "page", "set": {"a", "b"}},
        SecretObject(),
    )
    wait_until(lambda: len(fake.delivered) == 1)
    rendered = json.dumps(fake.delivered[0]["body"])
    assert "leaked-secret-repr" not in rendered
    assert "[NON_TEXT_CONTENT_OMITTED]" in rendered


def test_strict_boolean_and_safe_url_prefix(
    provider: tuple[SubstrateWikiProvider, FakeClient],
) -> None:
    instance, fake = provider
    assert json.loads(
        instance.handle_tool_call("wiki_query", {"question": "q", "save_as_synthesis": "false"})
    ) == {"error": "invalid_arguments"}
    assert fake.queries == []
    assert SubstrateClient.is_allowed_base_url("https://wiki.example.test/substrate")
    assert not SubstrateClient.is_allowed_base_url("https://wiki.example.test/a//b")
    assert not SubstrateClient.is_allowed_base_url("https://wiki.example.test/a/../b")
    assert not SubstrateClient.is_allowed_base_url("https://wiki.example.test/a/%2e%2e/b")


def test_capture_state_commits_only_after_durable_admission(
    provider: tuple[SubstrateWikiProvider, FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, fake = provider
    assert instance._spool is not None
    fake.fail = True
    fake.should_block = True
    fake.block_timeout = 30.0
    original_append = instance._spool.append
    monkeypatch.setattr(
        instance._spool, "append", lambda event: (_ for _ in ()).throw(OSError("full"))
    )
    instance.sync_turn("must retry", "answer")
    assert instance.status_snapshot()["counters"]["dropped"] >= 1
    monkeypatch.setattr(instance._spool, "append", original_append)
    instance.sync_turn("must retry", "answer")
    try:
        assert fake.request_started.wait(3.0)
        paths = list(instance._spool.root.glob("*.json"))
        assert len(paths) == 1
        event = instance._spool.load(paths[0])
        assert event["messages"] == [
            {"index": 0, "role": "user", "content": "must retry"},
            {"index": 1, "role": "assistant", "content": "answer"},
        ]
    finally:
        fake.block.set()
    wait_until(lambda: instance.status_snapshot()["counters"]["delivery_failed"] >= 1)


def test_session_end_does_not_emit_a_second_payload(
    provider: tuple[SubstrateWikiProvider, FakeClient],
) -> None:
    instance, fake = provider
    messages = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]
    instance.sync_turn("one", "two")
    instance.on_session_end(messages)
    wait_until(lambda: len(fake.delivered) == 1)
    assert fake.delivered[0]["body"]["kind"] == "turn"


def test_spool_claim_protects_inflight_oldest_from_trim(tmp_path: Path) -> None:
    spool = DurableSpool(tmp_path / "spool", max_items=2, max_bytes=4096)
    first = spool.append({"event_id": "first"})
    spool.append({"event_id": "second"})
    assert spool.claim_oldest() == first
    third = spool.append({"event_id": "third"})
    assert first.is_file()
    assert third.is_file()
    spool.release(first)


def test_auth_failure_remains_replayable(
    provider: tuple[SubstrateWikiProvider, FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, fake = provider
    fake.fail = True
    fake.fail_category = "http_401"
    monkeypatch.setattr(instance, "_wait_for_retry", lambda delay: instance._stop.wait(0.01))
    instance.sync_turn("u", "a")
    wait_until(lambda: instance.status_snapshot()["counters"]["delivery_failed"] >= 1)
    assert len(instance._spool) == 1
    assert instance.status_snapshot()["counters"]["quarantined"] == 0
    fake.fail = False
    instance._wake.set()
    wait_until(lambda: len(fake.delivered) == 1)
    assert len(instance._spool) == 0


def test_retry_delay_grows_caps_honors_rate_limit_and_auth_floor(
    provider: tuple[SubstrateWikiProvider, FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _ = provider
    monkeypatch.setattr(instance._retry_random, "uniform", lambda low, high: 1.0)
    assert instance._retry_delay("transport_error", 1) == 1
    assert instance._retry_delay("http_500", 4) == 8
    assert instance._retry_delay("http_500", 100) == 256
    assert instance._retry_delay("http_401", 1) == 30
    assert instance._retry_delay("http_403", 2) == 60
    assert instance._retry_delay("http_429", 1, retry_after=45) == 45
    assert instance._retry_delay("http_429", 1, retry_after=10000) == 300


def test_delivery_backoff_grows_and_resets_after_success(
    provider: tuple[SubstrateWikiProvider, FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, fake = provider
    delays: list[float] = []

    def wait_for_retry(delay: float) -> bool:
        delays.append(delay)
        if len(delays) == 2:
            fake.fail = False
        return instance._stop.is_set()

    monkeypatch.setattr(instance._retry_random, "uniform", lambda low, high: 1.0)
    monkeypatch.setattr(instance, "_wait_for_retry", wait_for_retry)
    fake.fail = True
    fake.fail_category = "http_500"
    instance.sync_turn("one", "answer")
    wait_until(lambda: len(fake.delivered) == 1)
    assert delays == [1, 2]
    assert instance._delivery_failure_streak == 0


def test_shutdown_interrupts_long_retry_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_API_URL", "https://wiki.example.test")
    monkeypatch.setenv("HERMES_API_KEY", "secret-value")
    fake = FakeClient()
    fake.fail = True
    fake.fail_category = "http_401"
    monkeypatch.setattr("substrate_wiki.SubstrateClient.from_env", lambda **kwargs: fake)
    instance = SubstrateWikiProvider()
    instance.initialize("session-a", hermes_home=str(tmp_path))
    instance.sync_turn("u", "a")
    wait_until(lambda: instance.status_snapshot()["counters"]["delivery_failed"] >= 1)
    started = time.monotonic()
    instance.shutdown()
    assert time.monotonic() - started < 1
    assert len(instance._spool) == 1


def test_malformed_and_permanent_spool_head_is_quarantined(
    provider: tuple[SubstrateWikiProvider, FakeClient],
) -> None:
    instance, fake = provider
    assert instance._spool is not None
    instance._spool.append({"kind": "unknown", "event_id": "bad"})
    instance._wake.set()
    wait_until(lambda: instance.status_snapshot()["counters"]["quarantined"] >= 1)
    fake.fail = True
    fake.fail_category = "http_400"
    instance.sync_turn("u", "a")
    wait_until(lambda: instance.status_snapshot()["counters"]["permanent_dropped"] >= 2)
    assert instance._worker is not None and instance._worker.is_alive()


def test_prefetch_cache_is_bounded_and_expired_entries_are_evicted(
    provider: tuple[SubstrateWikiProvider, FakeClient],
) -> None:
    instance, _ = provider
    now = time.monotonic()
    with instance._cache_lock:
        instance._prefetch_cache = {f"key-{index}": (now + 60, "value") for index in range(200)}
        instance._prefetch_cache["expired"] = (now - 1, "old")
        instance._latest_prefetch_cache = OrderedDict(
            (f"session-{index}", (now + 60, "value")) for index in range(40)
        )
        expired_session = instance._session_cache_key("expired-session")
        instance._latest_prefetch_cache[expired_session] = (now - 1, "old")
        instance._evict_prefetch_locked()
        while len(instance._prefetch_cache) > 128:
            instance._prefetch_cache.pop(next(iter(instance._prefetch_cache)))
        while len(instance._latest_prefetch_cache) > 32:
            instance._latest_prefetch_cache.popitem(last=False)
        assert "expired" not in instance._prefetch_cache
        assert expired_session not in instance._latest_prefetch_cache
        assert len(instance._prefetch_cache) <= 128
        assert len(instance._latest_prefetch_cache) <= 32
    assert instance.prefetch("follow-up", session_id="expired-session") == ""


def test_prefetch_accepts_only_canonical_entity_path_and_job_status_keeps_id() -> None:
    cited = SubstrateWikiProvider._cited_prefetch(
        {
            "results": [
                {"path": "topics/cached.md", "title": "Topic", "page_type": "topic"},
                {
                    "canonical_path": "entities/project/legacy--d4e5f6a7.md",
                    "snippet": "Legacy full-body evidence",
                    "page_type": "entity",
                    "entity_type": "project",
                },
                {
                    "path": "entities/project/cached--a1b2c3d4.md",
                    "canonical_path": "entities/project/cached--a1b2c3d4.md",
                    "title": "Cached page",
                    "memory_card": "Cached project profile",
                    "page_type": "entity",
                    "entity_id": "entity-cached",
                    "entity_type": "project",
                    "quality_version": 2,
                },
                {
                    "path": "entities/service/legacy--deadbeef.md",
                    "canonical_path": "entities/service/durable-worker--b2c3d4e5.md",
                    "title": "Durable worker",
                    "memory_card": "Stable service profile",
                    "page_type": "entity",
                    "entity_id": "entity-durable-worker",
                    "entity_type": "service",
                    "quality_version": 2,
                },
                {
                    "canonical_path": "entities/product/retyped--c3d4e5f6.md",
                    "title": "Retyped project",
                    "memory_card": "Canonical path remains immutable after retyping.",
                    "page_type": "entity",
                    "entity_id": "entity-retyped",
                    "entity_type": "project",
                    "roles": ["project", "product"],
                    "quality_version": 2,
                },
            ]
        }
    )
    assert "topics/cached" not in cited
    assert "legacy--d4e5f6a7" not in cited
    assert "Legacy full-body evidence" not in cited
    assert "entities/project/cached--a1b2c3d4.md" in cited
    assert "entities/service/durable-worker--b2c3d4e5.md" in cited
    assert "entities/service/legacy--deadbeef.md" not in cited
    assert "entities/product/retyped--c3d4e5f6.md" in cited
    shaped = SubstrateClient._shape_response(
        "/api/v1/hermes/wiki/job-status",
        {
            "id": "job-1",
            "status": "failed",
            "attempts": 1,
            "max_attempts": 3,
            "error": "could not connect to the public URL",
            "error_detail": {
                "code": "url_connect_failed",
                "retryable": True,
                "private": "omit",
            },
        },
    )
    assert shaped["id"] == "job-1"
    assert shaped["attempts"] == 1
    assert shaped["error_detail"] == {"code": "url_connect_failed", "retryable": True}


def test_prefetch_collapses_duplicate_canonical_entity_ids() -> None:
    common = {
        "canonical_path": "entities/project/cached--a1b2c3d4.md",
        "page_type": "entity",
        "entity_id": "entity-cached",
        "entity_type": "project",
        "quality_version": 2,
    }
    cited = SubstrateWikiProvider._cited_prefetch(
        {
            "results": [
                {**common, "memory_card": "Canonical card"},
                {**common, "memory_card": "Duplicate card must be collapsed"},
            ]
        }
    )

    assert "Canonical card" in cited
    assert "Duplicate card must be collapsed" not in cited
    assert (
        SubstrateWikiProvider._cited_prefetch(
            {"results": [{**common, "entity_id": "", "memory_card": "Unidentified"}]}
        )
        == ""
    )


def test_query_response_shaping_keeps_bounded_persistence_contract() -> None:
    shaped = SubstrateClient._shape_response(
        "/api/v1/hermes/wiki/query",
        {
            "answer": "answer",
            "insufficient_context": False,
            "saved": False,
            "synthesis_path": None,
            "error": {
                "code": "synthesis_persistence_failed",
                "message": "The answer was generated but could not be saved.",
                "retryable": True,
                "traceback": "omit",
            },
        },
    )

    assert shaped["saved"] is False
    assert shaped["synthesis_path"] is None
    assert shaped["error"] == {
        "code": "synthesis_persistence_failed",
        "message": "The answer was generated but could not be saved.",
        "retryable": True,
    }


def test_wiki_read_schema_documents_path_or_slug(provider) -> None:
    instance, _ = provider
    schema = next(item for item in instance.get_tool_schemas() if item["name"] == "wiki_read")

    assert "path or legacy slug" in schema["description"]
    assert (
        "notes/hermes.md or notes/hermes"
        in schema["parameters"]["properties"]["path"]["description"]
    )


def test_redaction_sanitizes_secret_mapping_keys_without_collisions() -> None:
    from substrate_wiki.redaction import redact

    secret = "very-secret-hermes-key"
    sanitized = redact({secret: "first", "api_key": "second"}, (secret,))
    rendered = json.dumps(sanitized)
    assert secret not in rendered
    assert set(sanitized) == {"[REDACTED]", "[REDACTED]#2"}


def test_configured_secret_scan_prioritizes_provider_values_and_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from substrate_wiki import redaction

    environment = {
        "UNRELATED": "not-sensitive",
        "HERMES_API_KEY": "hermes-required-value",
        "MINIMAX_API_KEY": "minimax-required-value",
        "NVIDIA_API_KEY": "nvidia-required-value",
        **{
            f"EXTRA_{index:03d}_API_KEY": f"extra-sensitive-value-{index:03d}"
            for index in range(redaction._MAX_CONFIGURED_SECRETS - 3)
        },
    }
    monkeypatch.setattr(redaction.os, "environ", environment)

    values = redaction.configured_secret_values()

    assert len(values) == redaction._MAX_CONFIGURED_SECRETS
    assert environment["HERMES_API_KEY"] in values
    assert environment["MINIMAX_API_KEY"] in values
    assert environment["NVIDIA_API_KEY"] in values
    assert values == tuple(sorted(values, key=lambda item: (-len(item), item)))
    assert sum(len(value.encode("utf-8")) for value in values) <= (
        redaction._MAX_CONFIGURED_SECRET_BYTES
    )


def test_configured_secret_scan_fails_closed_without_logging_overflow_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from substrate_wiki import redaction

    count_values = {
        f"PROVIDER_{index:03d}_API_KEY": f"count-overflow-value-{index:03d}"
        for index in range(redaction._MAX_CONFIGURED_SECRETS + 1)
    }
    monkeypatch.setattr(redaction.os, "environ", count_values)
    with pytest.raises(ValueError) as count_error:
        redaction.configured_secret_values()
    assert str(count_error.value) == "configured secrets exceed redaction bounds"
    assert not any(value in str(count_error.value) for value in count_values.values())

    byte_values = {
        f"PROVIDER_{index}_API_KEY": (chr(ord("a") + index) * 15_000) for index in range(5)
    }
    monkeypatch.setattr(redaction.os, "environ", byte_values)
    with pytest.raises(ValueError) as byte_error:
        redaction.configured_secret_values()
    assert str(byte_error.value) == "configured secrets exceed redaction bounds"
    assert not any(value in str(byte_error.value) for value in byte_values.values())


def test_prefetch_queue_is_bounded(provider: tuple[SubstrateWikiProvider, FakeClient]) -> None:
    instance, _ = provider
    instance._prefetch_jobs = queue.Queue(maxsize=1)
    instance._prefetch_jobs.put_nowait(("existing", "s"))
    instance.queue_prefetch("discarded", session_id="s")
    assert instance._prefetch_jobs.qsize() == 1
