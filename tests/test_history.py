from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(PLUGIN_ROOT))

from substrate_wiki.checkpoint import ImportCheckpoint  # noqa: E402
from substrate_wiki.client import SubstrateAPIError  # noqa: E402
from substrate_wiki.history import (  # noqa: E402
    HermesHistoryImporter,
    HermesSQLiteHistorySource,
)


def _database(path: Path, messages: int = 3) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT,
                chat_type TEXT, started_at REAL NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, timestamp REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO sessions VALUES ('session-a', 'cli', NULL, NULL, 1.0);
            """
        )
        connection.executemany(
            "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (
                ("session-a", "user" if index % 2 == 0 else "assistant", f"message-{index}", index)
                for index in range(messages)
            ),
        )
        connection.commit()
    finally:
        connection.close()


class Client:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.fail_after = fail_after
        self.requests: list[dict[str, Any]] = []

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

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        del method, path
        if self.fail_after is not None and len(self.requests) >= self.fail_after:
            raise SubstrateAPIError("http_400")
        self.requests.append(kwargs["body"])
        return {"duplicate": False}

    def import_status(self, batch_id: str) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "processed_windows": 1,
            "failed_windows": 0,
            "processed": 1,
            "pending_review": 0,
            "failed": 0,
            "projected_entities": 4,
            "published_claims": 9,
            "stub_count": 3,
            "pending_resolution": 2,
            "projection_pending": 0,
            "complete": True,
        }


def test_checkpoint_is_content_free_and_stable_when_state_db_changes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    database = home / "state.db"
    _database(database, messages=2)
    client = Client()
    importer = HermesHistoryImporter(
        hermes_home=home,
        client=client,  # type: ignore[arg-type]
        source=HermesSQLiteHistorySource(database),
    )
    checkpoint = importer.prepare()
    original_batch = importer.batch_id
    connection = sqlite3.connect(database)
    connection.execute("INSERT INTO sessions VALUES ('new-session', 'cli', NULL, NULL, 2.0)")
    connection.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES ('new-session', 'user', 'new private content', 2.0)"
    )
    connection.commit()
    connection.close()
    status = importer.run(wait=True)
    checkpoint_path = checkpoint.path
    checkpoint.close()

    assert status["batch_id"] == original_batch
    assert status["eligible"] == 1
    assert status["failed_windows"] == 0
    assert status["projected_entities"] == 4
    assert status["published_claims"] == 9
    assert status["stub_count"] == 3
    assert status["pending_resolution"] == 2
    assert status["projection_pending"] == 0
    raw = checkpoint_path.read_bytes()
    assert b"message-0" not in raw
    assert b"new private content" not in raw
    assert b"HERMES_API_KEY" not in raw


def test_resume_skips_every_acknowledged_event(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    database = home / "state.db"
    _database(database, messages=3)
    first_client = Client(fail_after=1)
    first = HermesHistoryImporter(
        hermes_home=home,
        client=first_client,  # type: ignore[arg-type]
        source=HermesSQLiteHistorySource(database),
    )
    with pytest.raises(SubstrateAPIError):
        first.run(wait=True)
    first_ids = {str(item["event_id"]) for item in first_client.requests}
    assert len(first_ids) == 1
    if first.checkpoint is not None:
        first.checkpoint.close()

    resumed_client = Client()
    resumed = HermesHistoryImporter(
        hermes_home=home,
        client=resumed_client,  # type: ignore[arg-type]
        source=HermesSQLiteHistorySource(database),
    )
    status = resumed.run(wait=True)
    resumed_ids = {str(item["event_id"]) for item in resumed_client.requests}
    assert status["complete"] is True
    assert first_ids.isdisjoint(resumed_ids)
    assert status["checkpointed"] == status["delivered"]
    if resumed.checkpoint is not None:
        resumed.checkpoint.close()


def test_graceful_stop_after_acknowledgement_resumes_same_job_and_batch(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    database = home / "state.db"
    _database(database, messages=2)

    class PausingClient(Client):
        stop_requested = False

        def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            result = super().request(method, path, **kwargs)
            self.stop_requested = True
            return result

    pausing_client = PausingClient()
    paused = HermesHistoryImporter(
        hermes_home=home,
        client=pausing_client,  # type: ignore[arg-type]
        source=HermesSQLiteHistorySource(database),
        stop_requested=lambda: pausing_client.stop_requested,
    )
    paused_status = paused.run(wait=True)
    original_job = paused_status["job_id"]
    original_batch = paused_status["batch_id"]
    assert paused_status["complete"] is False
    assert paused_status["checkpointed"] == 1
    if paused.checkpoint is not None:
        paused.checkpoint.close()

    resumed = HermesHistoryImporter(
        hermes_home=home,
        client=Client(),  # type: ignore[arg-type]
        source=HermesSQLiteHistorySource(database),
    )
    resumed_status = resumed.run(wait=True)
    assert resumed_status["job_id"] == original_job
    assert resumed_status["batch_id"] == original_batch
    assert resumed_status["complete"] is True
    if resumed.checkpoint is not None:
        resumed.checkpoint.close()


def test_v13_checkpoint_migration_preserves_job_and_partial_remote_status(
    tmp_path: Path,
) -> None:
    checkpoint = ImportCheckpoint.create_or_attach(
        tmp_path,
        source_kind="hermes-sqlite",
        source_locator=str(tmp_path / "state.db"),
        agent_id="default",
    )
    original_job = checkpoint.status()["job_id"]
    path = checkpoint.path
    checkpoint.close()
    with sqlite3.connect(path) as connection:
        for column in (
            "failed_windows",
            "projected_entities",
            "published_claims",
            "stub_count",
            "pending_resolution",
            "projection_pending",
        ):
            connection.execute(f"ALTER TABLE job DROP COLUMN {column}")

    with ImportCheckpoint(path) as migrated:
        assert migrated.status()["job_id"] == original_job
        assert migrated.status()["projected_entities"] == 0
        migrated.update_remote(
            {
                "processed_windows": 12,
                "failed_windows": 2,
                "processed": 4,
                "pending_review": 7,
                "failed": 0,
                "projected_entities": 8,
                "published_claims": 21,
                "stub_count": 5,
                "pending_resolution": 3,
                "projection_pending": 6,
                "error_class": "provider_exhausted",
            }
        )
        migrated.update_remote({"processed": 5})
        status = migrated.status()
        assert status["processed"] == 5
        assert status["processed_windows"] == 12
        assert status["projected_entities"] == 8
        assert status["projection_pending"] == 6
        assert status["error_class"] == "provider_exhausted"

        migrated.update_remote({"error_class": "secret internal provider response"})
        assert migrated.status()["error_class"] == "server_error"
        migrated.update_remote({"complete": True, "failed": 0})
        assert migrated.status()["state"] == "complete_with_failures"

    assert b"secret internal provider response" not in path.read_bytes()


def test_checkpoint_schema_contains_no_content_columns(tmp_path: Path) -> None:
    checkpoint = ImportCheckpoint.create_or_attach(
        tmp_path,
        source_kind="hermes-sqlite",
        source_locator=str(tmp_path / "state.db"),
        agent_id="default",
    )
    tables = {
        str(row[0])
        for row in checkpoint.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    columns = {
        str(row[1]).casefold()
        for table in tables
        for row in checkpoint.connection.execute(f"PRAGMA table_info({table})")
    }
    assert not columns & {
        "content",
        "prompt",
        "conclusion",
        "tool_result",
        "credential",
        "secret",
    }
    checkpoint.close()


def test_systemd_installer_declares_separate_bounded_cgroup() -> None:
    installer = (Path(__file__).parents[1] / "scripts" / "install_hermes_plugin.py").read_text(
        encoding="utf-8"
    )
    for setting in (
        "MemoryHigh=224M",
        "MemoryMax=256M",
        "OOMPolicy=stop",
        "Restart=on-failure",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
    ):
        assert setting in installer
    assert "HERMES_API_KEY=" not in installer
    assert "hermes-gateway" not in installer
