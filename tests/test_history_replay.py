from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(PLUGIN_ROOT))

from substrate_wiki import cli as import_cli  # noqa: E402
from substrate_wiki import worker as import_worker  # noqa: E402
from substrate_wiki.checkpoint import ImportCheckpoint  # noqa: E402
from substrate_wiki.client import SubstrateAPIError  # noqa: E402
from substrate_wiki.events import (  # noqa: E402
    BoundedTextSource,
    CaptureEventBuilder,
    canonical_bytes,
)
from substrate_wiki.history import (  # noqa: E402
    HermesHistoryImporter,
    HermesJSONLHistorySource,
    HermesSQLiteHistorySource,
    HistoryInventory,
    HistorySession,
    _deliver_with_retry,
    _recover_official_export_spills,
    select_history_source,
)
from substrate_wiki.redaction import (  # noqa: E402
    StreamingTextRedactor,
    redact_text,
)


class _ChunkedText:
    def __init__(self, chunks: tuple[str, ...]) -> None:
        self.chunks = chunks

    def iter_text_chunks(self):  # type: ignore[no-untyped-def]
        yield from self.chunks


def _streamed_capture(
    text: str,
    chunks: tuple[str, ...],
    *,
    secrets: tuple[str, ...] = (),
    maximum: int = 16 * 1024,
) -> tuple[str, list[dict[str, Any]]]:
    builder = CaptureEventBuilder(
        {"platform": "cli"},
        secrets=secrets,
        max_capture_bytes=maximum,
    )
    events = list(
        builder.iter_message_events(
            "turn",
            "stream-redaction",
            ({"role": "user", "content": _ChunkedText(chunks)},),
            capture_origin="history_replay",
            deterministic=True,
        )
    )
    rendered = "".join(
        str(message["content"])
        for event in events
        for message in event["messages"]
    )
    assert sum(len(chunk) for chunk in chunks) == len(text)
    return rendered, events


def _checkpoint(tmp_path: Path, source_kind: str, locator: str) -> ImportCheckpoint:
    return ImportCheckpoint.create_or_attach(
        tmp_path / "hermes",
        source_kind=source_kind,
        source_locator=locator,
        agent_id="default",
    )


def _create_state_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                chat_type TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                archived INTEGER NOT NULL DEFAULT 0,
                profile_name TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                reasoning TEXT,
                platform_message_id TEXT,
                active INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        sessions = [
            ("cli-archived", "cli", None, None, None, 1.0, 2.0, 1, "default"),
            ("cron-1", "cron", None, None, None, 3.0, 4.0, 0, None),
            ("group-1", "telegram", "person-1", "group", None, 5.0, 6.0, 0, None),
            ("missing-user", "telegram", None, "private", None, 7.0, 8.0, 0, None),
            ("direct-1", "telegram", "person-2", "private", None, 9.0, 10.0, 0, None),
        ]
        connection.executemany(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            sessions,
        )
        rows = [
            ("cli-archived", "system", "hidden prompt", 1.0, "hidden reasoning"),
            ("cli-archived", "user", "visible archived question", 1.1, None),
            ("cli-archived", "assistant", "visible archived answer", 1.2, "private chain"),
            ("cron-1", "user", "scheduled", 3.1, None),
            ("group-1", "user", "group text", 5.1, None),
            ("missing-user", "user", "ambiguous", 7.1, None),
            ("direct-1", "user", "direct message", 9.1, None),
        ]
        connection.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp, reasoning) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def test_shared_builder_is_deterministic_bounded_and_content_safe() -> None:
    builder = CaptureEventBuilder(
        {"platform": "cli"},
        secrets=("top-secret",),
        max_capture_bytes=16 * 1024,
    )
    messages = [
        {"role": "system", "content": "hidden"},
        {
            "role": "user",
            "content": "top-secret " + ("large visible text " * 3000),
            "reasoning": "never copy this",
        },
        {
            "role": "assistant",
            "content": {"text": "visible", "image": "data:image/png;base64,AAAA"},
            "tool_calls": [{"name": "inspect", "arguments": {"base64": "AAAA"}}],
        },
    ]

    first = builder.message_events(
        "session_end",
        "session-1",
        messages,
        capture_origin="history_replay",
        batch_id="batch-1",
        deterministic=True,
    )
    second = builder.message_events(
        "session_end",
        "session-1",
        messages,
        capture_origin="history_replay",
        batch_id="batch-1",
        deterministic=True,
    )

    assert first == second
    assert len(first) > 1
    assert all(len(canonical_bytes(event)) <= 16 * 1024 for event in first)
    encoded = json.dumps(first)
    assert "top-secret" not in encoded
    assert "never copy this" not in encoded
    assert "hidden" not in encoded
    assert "[BINARY_CONTENT_OMITTED]" in encoded
    assert all(
        set(event) == {"schema_version", "event_id", "kind", "session_id", "created_at", "messages"}
        for event in first
    )


@pytest.mark.parametrize(
    ("text", "secrets", "unsafe"),
    (
        (
            "safe-before::known-secret-value::safe-after",
            ("known-secret-value",),
            ("known-secret-value",),
        ),
        (
            "safe-before Authorization: Bearer abcDEF0123456789._~- safe-after",
            (),
            ("abcDEF0123456789",),
        ),
        (
            'safe-before api_key="assignment-secret-0123456789" safe-after',
            (),
            ("assignment-secret-0123456789",),
        ),
        (
            "safe-before github_pat_ABCdef0123456789 safe-after",
            (),
            ("github_pat_ABCdef0123456789",),
        ),
        (
            "safe-before -----BEGIN PRIVATE KEY-----\n"
            "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
            "-----END PRIVATE KEY----- safe-after",
            (),
            ("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo",),
        ),
    ),
)
def test_streamed_redaction_is_safe_at_every_source_boundary(
    text: str,
    secrets: tuple[str, ...],
    unsafe: tuple[str, ...],
) -> None:
    expected = redact_text(text, secrets)
    for boundary in range(len(text) + 1):
        rendered, events = _streamed_capture(
            text,
            (text[:boundary], text[boundary:]),
            secrets=secrets,
        )
        assert rendered == expected
        persisted_capture = canonical_bytes(events)
        assert all(fragment not in rendered for fragment in unsafe)
        assert all(fragment.encode() not in persisted_capture for fragment in unsafe)
        assert all(len(canonical_bytes(event)) <= 16 * 1024 for event in events)


def test_streamed_redaction_is_chunk_independent_and_resume_deterministic() -> None:
    secret = "resume-secret-0123456789"
    text = (
        ("safe-caf\u00e9-\U0001f642/" * 2_000)
        + f" api_key={secret} "
        + ("durable-tail/" * 2_000)
    )
    layouts = (
        (text,),
        tuple(text[index : index + 1] for index in range(len(text))),
        tuple(text[index : index + 997] for index in range(0, len(text), 997)),
    )
    captures = [
        _streamed_capture(text, chunks, secrets=(secret,))[1] for chunks in layouts
    ]

    assert captures[0] == captures[1] == captures[2]
    assert secret.encode() not in canonical_bytes(captures[0])
    assert len(captures[0]) > 1


def test_streamed_redactor_and_capture_memory_are_bounded_for_large_message() -> None:
    chunk = "large-safe-history-\U0001f642/" * 2_048
    # More than 64 MiB of UTF-8 input, generated repeatably without holding the
    # materialized message in the test process.
    repetitions = 1_536

    redactor = StreamingTextRedactor(())
    released_chars = 0
    tracemalloc.start()
    try:
        for _ in range(repetitions):
            released_chars += sum(len(item) for item in redactor.feed(chunk))
            assert redactor.buffered_chars < 1_000_000
        released_chars += sum(len(item) for item in redactor.finish())
        _current, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert released_chars == len(chunk) * repetitions
    assert peak_bytes < 32 * 1024 * 1024

    capture_repetitions = 24

    class _RepeatedText:
        def iter_text_chunks(self):  # type: ignore[no-untyped-def]
            for _ in range(capture_repetitions):
                yield chunk

    builder = CaptureEventBuilder({"platform": "cli"})
    captured_chars = 0
    event_count = 0
    for event in builder.iter_message_events(
        "turn",
        "bounded-large-redaction",
        ({"role": "user", "content": _RepeatedText()},),
        capture_origin="history_replay",
        deterministic=True,
    ):
        event_count += 1
        captured_chars += sum(
            len(str(message["content"]))
            for message in event["messages"]
        )

    assert captured_chars == len(chunk) * capture_repetitions
    assert event_count > 1


def test_sqlite_adapter_reads_without_writes_and_filters_identity(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    _create_state_db(database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    source = HermesSQLiteHistorySource(database)
    checkpoint = _checkpoint(tmp_path, source.source_kind, source.source_locator)
    counts = source.discover(checkpoint)
    sessions = list(checkpoint.sessions())

    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert counts["discovered"] == 5
    assert counts["skipped"] == 1
    assert counts["quarantined"] == 2
    assert [session.external_id for session in sessions] == [
        "cli-archived",
        "direct-1",
    ]
    assert sessions[0].subject_id == "owner"
    messages = list(source.iter_messages(sessions[0], start=0))
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
    ]
    assert "reasoning" not in json.dumps(messages)
    checkpoint.close()


def test_sqlite_large_message_is_read_and_emitted_in_bounded_slices(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    content = "bounded-history-" * 140_000
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, timestamp REAL NOT NULL
            );
            INSERT INTO sessions VALUES ('large', 'cli', 1.0);
            """
        )
        connection.execute(
            "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ("large", "user", content, 1.0),
        )
        connection.commit()
    finally:
        connection.close()

    source = HermesSQLiteHistorySource(database)
    checkpoint = _checkpoint(tmp_path, source.source_kind, source.source_locator)
    source.discover(checkpoint)
    message = next(source.iter_messages(next(checkpoint.sessions()), start=0))

    deferred = message["content"]
    assert isinstance(deferred, BoundedTextSource)
    chunks = list(deferred.iter_text_chunks())
    assert chunks
    assert max(len(chunk) for chunk in chunks) <= 1024 * 1024
    assert "".join(chunks) == content

    builder = CaptureEventBuilder({"platform": "cli"})
    events = list(
        builder.iter_message_events(
            "turn", "large", (message,), deterministic=True
        )
    )
    assert all(len(canonical_bytes(event)) <= 256 * 1024 for event in events)
    fragments = [
        item
        for event in events
        for item in event["messages"]
    ]
    assert "".join(str(item["content"]) for item in fragments) == content
    assert {item["fragment"]["encoding"] for item in fragments} == {"utf8-content"}
    checkpoint.close()


def test_adapters_do_not_use_fetchall_or_unbounded_file_reads() -> None:
    source = (PLUGIN_ROOT / "substrate_wiki" / "history.py").read_text(encoding="utf-8")
    assert "fetchall(" not in source
    assert ".readline(" not in source
    assert ".read()" not in source
    assert "export_all(" not in source
    assert "buffered.extend(" not in source


def test_official_jsonl_export_shape_is_supported(tmp_path: Path) -> None:
    export = tmp_path / "sessions.jsonl"
    export.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "id": "cli-export",
                        "source": "cli",
                        "archived": 1,
                        "messages": [{"role": "user", "content": "exported"}],
                    }
                ),
                json.dumps(
                    {
                        "id": "group-export",
                        "source": "telegram",
                        "user_id": "person",
                        "chat_type": "group",
                        "messages": [{"role": "user", "content": "ambiguous"}],
                    }
                ),
                "not-json",
            )
        ),
        encoding="utf-8",
    )

    source = HermesJSONLHistorySource(export)
    checkpoint = _checkpoint(tmp_path, source.source_kind, source.source_locator)
    counts = source.discover(checkpoint)
    sessions = list(checkpoint.sessions())

    assert counts["discovered"] == 3
    assert [session.external_id for session in sessions] == ["cli-export"]
    assert counts["quarantined"] == 2
    assert [item["content"] for item in source.iter_messages(sessions[0], start=0)] == [
        "exported"
    ]
    checkpoint.close()


def test_official_export_survives_acknowledged_pause_and_resumes_same_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home = tmp_path / "hermes"
    export = (
        hermes_home
        / "substrate_wiki"
        / "imports"
        / "spill"
        / "official-export-test.jsonl"
    )
    export.parent.mkdir(parents=True, mode=0o700)
    export.write_text(
        json.dumps(
            {
                "id": "official-session",
                "source": "cli",
                "messages": [{"role": "user", "content": "durable export fact"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    export.chmod(0o600)
    source = HermesJSONLHistorySource(export)
    checkpoint = ImportCheckpoint.create_or_attach(
        hermes_home,
        source_kind="hermes-official-export",
        source_locator=str(export.resolve()),
        agent_id="default",
    )
    source.discover(checkpoint)
    initial = checkpoint.status()
    job_id = str(initial["job_id"])
    batch_id = str(initial["batch_id"])
    checkpoint.close()

    stop_holder: dict[str, Any] = {}

    def install_stop_handlers(stop_event: Any) -> dict[Any, Any]:
        stop_holder["event"] = stop_event
        return {}

    class ReplayClient:
        def __init__(self, *, stop_after: int | None, complete: bool) -> None:
            self.stop_after = stop_after
            self.complete = complete
            self.event_ids: list[str] = []

        @staticmethod
        def capabilities() -> dict[str, Any]:
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
            }

        def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            del method, path
            event_id = str(kwargs["body"]["event_id"])
            self.event_ids.append(event_id)
            if self.stop_after is not None and len(self.event_ids) == self.stop_after:
                stop_holder["event"].set()
            return {"duplicate": False}

        def import_status(self, selected_batch: str) -> dict[str, Any]:
            assert selected_batch == batch_id
            return {
                "batch_id": selected_batch,
                "processed": 1,
                "failed": 0,
                "failed_windows": 0,
                "complete": self.complete,
            }

    first_client = ReplayClient(stop_after=2, complete=False)
    monkeypatch.setattr(import_worker, "_install_stop_handlers", install_stop_handlers)
    monkeypatch.setattr(
        import_worker.SubstrateClient,
        "from_env",
        classmethod(lambda cls, **kwargs: first_client),
    )
    monkeypatch.setattr(import_worker.subprocess, "run", lambda *args, **kwargs: None)

    paused = import_worker.run_job(hermes_home, job_id)

    assert paused["job_id"] == job_id
    assert paused["batch_id"] == batch_id
    assert paused["complete"] is False
    assert paused["checkpointed"] == 2
    assert export.is_file()

    second_client = ReplayClient(stop_after=None, complete=True)
    monkeypatch.setattr(import_worker, "_install_stop_handlers", lambda event: {})
    monkeypatch.setattr(
        import_worker.SubstrateClient,
        "from_env",
        classmethod(lambda cls, **kwargs: second_client),
    )

    completed = import_worker.run_job(hermes_home, job_id)

    assert completed["job_id"] == job_id
    assert completed["batch_id"] == batch_id
    assert completed["complete"] is True
    assert not set(first_client.event_ids) & set(second_client.event_ids)
    assert not export.exists()


def test_official_export_attaches_before_refreshing_active_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home = tmp_path / "hermes"

    class FakeSessionDB:
        rows = [
            {
                "id": "official-session",
                "source": "cli",
                "messages": [{"role": "user", "content": "immutable fact"}],
            }
        ]
        opened = 0

        def __init__(self, *, db_path: Path, read_only: bool) -> None:
            assert db_path == hermes_home / "state.db"
            assert read_only is True
            jobs = hermes_home / "substrate_wiki" / "imports" / "jobs"
            assert any(jobs.glob("*/checkpoint.db"))
            type(self).opened += 1

        def iter_export(self) -> Any:
            yield from type(self).rows

        def close(self) -> None:
            return None

    class CapabilityClient:
        @staticmethod
        def capabilities() -> dict[str, Any]:
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
            }

        @staticmethod
        def import_status(batch_id: str) -> dict[str, Any]:
            del batch_id
            return {}

    monkeypatch.setitem(
        sys.modules, "hermes_state", SimpleNamespace(SessionDB=FakeSessionDB)
    )
    first_source = select_history_source(hermes_home)
    assert FakeSessionDB.opened == 0
    first_importer = HermesHistoryImporter(
        hermes_home=hermes_home,
        client=CapabilityClient(),  # type: ignore[arg-type]
        source=first_source,
    )
    first_checkpoint = first_importer.prepare()
    first_status = first_checkpoint.status()
    export = Path(first_checkpoint.job()["source_locator"])
    first_digest = hashlib.sha256(export.read_bytes()).hexdigest()
    first_checkpoint.close()
    assert FakeSessionDB.opened == 1
    os.utime(export, (1.0, 1.0))

    FakeSessionDB.rows = [
        {
            "id": "new-session-that-must-wait-for-another-job",
            "source": "cli",
            "messages": [{"role": "user", "content": "new mutable history"}],
        }
    ]
    second_source = select_history_source(hermes_home)
    second_importer = HermesHistoryImporter(
        hermes_home=hermes_home,
        client=CapabilityClient(),  # type: ignore[arg-type]
        source=second_source,
    )
    second_checkpoint = second_importer.prepare()
    second_status = second_checkpoint.status()

    assert second_status["job_id"] == first_status["job_id"]
    assert second_status["batch_id"] == first_status["batch_id"]
    assert hashlib.sha256(export.read_bytes()).hexdigest() == first_digest
    assert FakeSessionDB.opened == 1
    second_checkpoint.close()


def test_official_export_recovery_removes_only_expired_orphans(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "hermes"
    spill = hermes_home / "substrate_wiki" / "imports" / "spill"
    spill.mkdir(parents=True, mode=0o700)
    expired = spill / "official-export-orphan.jsonl"
    fresh = spill / "official-export-fresh.jsonl"
    temporary = spill / ".official-export-orphan.jsonl.1.1.tmp"
    unrelated = spill / "operator-note.txt"
    for path in (expired, fresh, temporary, unrelated):
        path.write_text("{}\n", encoding="utf-8")
        path.chmod(0o600)
    os.utime(expired, (1.0, 1.0))
    os.utime(temporary, (1.0, 1.0))
    os.utime(fresh, (999.0, 999.0))
    os.utime(unrelated, (1.0, 1.0))

    active = _recover_official_export_spills(
        hermes_home, now=1_000.0, max_orphan_age_seconds=100.0
    )

    assert active == []
    assert not expired.exists()
    assert not temporary.exists()
    assert fresh.is_file()
    assert unrelated.is_file()


def test_import_cancel_removes_job_owned_official_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home = tmp_path / "hermes"
    export = (
        hermes_home
        / "substrate_wiki"
        / "imports"
        / "spill"
        / "official-export-cancel.jsonl"
    )
    export.parent.mkdir(parents=True, mode=0o700)
    export.write_text("{}\n", encoding="utf-8")
    export.chmod(0o600)
    checkpoint = ImportCheckpoint.create_or_attach(
        hermes_home,
        source_kind="hermes-official-export",
        source_locator=str(export.resolve()),
        agent_id="default",
    )
    before = checkpoint.status()
    checkpoint.close()
    stopped: list[str] = []
    monkeypatch.setattr(
        import_cli,
        "stop_service",
        lambda home, job_id: stopped.append(job_id),
    )

    result = import_cli._cancel(
        SimpleNamespace(yes=True, job_id=before["job_id"]), hermes_home
    )

    assert result["job_id"] == before["job_id"]
    assert result["batch_id"] == before["batch_id"]
    assert result["state"] == "cancelled"
    assert stopped == [before["job_id"]]
    assert not export.exists()


def test_oversized_jsonl_record_streams_message_without_materializing_record(
    tmp_path: Path,
) -> None:
    export = tmp_path / "large-session.jsonl"
    encoded_piece = "line\\nsmile\\u263a-"
    decoded_piece = "line\nsmile☺-"
    repetitions = 100_000
    with export.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write('{"id":"large-jsonl","source":"cli","messages":[')
        stream.write('{"role":"user","content":"')
        for _ in range(repetitions):
            stream.write(encoded_piece)
        stream.write('"}]}\n')

    source = HermesJSONLHistorySource(export)
    checkpoint = _checkpoint(tmp_path, source.source_kind, source.source_locator)
    counts = source.discover(checkpoint)
    assert counts == {"discovered": 1, "eligible": 1, "skipped": 0, "quarantined": 0}

    iterator = source.iter_messages(next(checkpoint.sessions()), start=0)
    message = next(iterator)
    deferred = message["content"]
    assert isinstance(deferred, BoundedTextSource)
    chunks = list(deferred.iter_text_chunks())
    assert chunks
    assert max(len(chunk) for chunk in chunks) <= 64 * 1024
    assert "".join(chunks) == decoded_piece * repetitions

    builder = CaptureEventBuilder({"platform": "cli"})
    events = list(
        builder.iter_message_events(
            "turn", "large-jsonl", (message,), deterministic=True
        )
    )
    fragments = [item for event in events for item in event["messages"]]
    assert "".join(str(item["content"]) for item in fragments) == decoded_piece * repetitions
    assert all(len(canonical_bytes(event)) <= 256 * 1024 for event in events)
    iterator.close()
    checkpoint.close()


class _ReplayClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.requests.append((path, kwargs))
        return {"duplicate": False}

    def capabilities(self) -> dict[str, Any]:
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
        }

    def import_status(self, batch_id: str) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "processed": 1,
            "pending_review": 0,
            "failed": 0,
            "complete": True,
        }


def test_importer_resumes_from_checkpoint_and_uses_standard_endpoints(tmp_path: Path) -> None:
    inventory = HistoryInventory(
        sessions=[
            HistorySession(
                external_id="session-a",
                source="cli",
                user_id="",
                subject_id="owner",
                messages=[
                    {"role": "user", "content": "hello", "timestamp": 1.0},
                    {"role": "assistant", "content": "hi", "timestamp": 2.0},
                ],
                metadata={"archived": 1},
            )
        ],
        discovered=1,
    )
    first_client = _ReplayClient()
    first = HermesHistoryImporter(
        hermes_home=tmp_path,
        client=first_client,  # type: ignore[arg-type]
        inventory=inventory,
    )
    status = first.run(wait=True)

    assert status["complete"] is True
    assert [path for path, _ in first_client.requests] == [
        "/api/v1/hermes/turns",
        "/api/v1/hermes/completed-sessions",
    ]
    event_ids = [request["body"]["event_id"] for _, request in first_client.requests]
    assert len(event_ids) == len(set(event_ids))
    assert all(
        not request["body"].get("messages")
        for path, request in first_client.requests
        if path == "/api/v1/hermes/completed-sessions"
    )


def test_importer_refuses_server_without_stream_v2(tmp_path: Path) -> None:
    class OldServer(_ReplayClient):
        def capabilities(self) -> dict[str, Any]:
            return {"max_event_bytes": 262_144, "history_replay": {"protocol": "legacy"}}

    importer = HermesHistoryImporter(
        hermes_home=tmp_path,
        client=OldServer(),  # type: ignore[arg-type]
        inventory=HistoryInventory(),
    )
    with pytest.raises(SubstrateAPIError, match="server_upgrade_required"):
        importer.prepare()


def test_delivery_retries_only_transient_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        attempts = 0

        def request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.attempts += 1
            if self.attempts == 1:
                raise SubstrateAPIError("transport_error")
            return {"stored": True}

    client = Client()
    monkeypatch.setattr("substrate_wiki.history.time.sleep", lambda delay: None)
    result = _deliver_with_retry(
        client,  # type: ignore[arg-type]
        {"kind": "turn", "event_id": "event-1"},
    )
    assert result == {"stored": True}
    assert client.attempts == 2

    class Unauthorized:
        def request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise SubstrateAPIError("http_401")

    with pytest.raises(SubstrateAPIError):
        _deliver_with_retry(
            Unauthorized(),  # type: ignore[arg-type]
            {"kind": "turn", "event_id": "event-2"},
        )
