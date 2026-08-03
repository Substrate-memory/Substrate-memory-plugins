"""Content-free durable state for resumable Hermes history imports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .spool import secure_atomic_json_write

PROTOCOL = "stream-v2"
CHECKPOINT_VERSION = 2
TERMINAL_STATES = {"complete", "complete_with_failures", "cancelled"}
_REMOTE_COUNTER_FIELDS = (
    "failed",
    "failed_windows",
    "pending_resolution",
    "pending_review",
    "processed",
    "processed_windows",
    "projected_entities",
    "projection_pending",
    "published_claims",
    "stub_count",
)
_MAX_COUNTER = (1 << 63) - 1
_ERROR_CLASS_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}")


def _now() -> float:
    return time.time()


def _private_directory(path: Path) -> Path:
    if path.exists() and path.is_symlink():
        raise OSError("import state directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path, 0o700)
    return path


@dataclass(frozen=True, slots=True)
class SessionDescriptor:
    external_id: str
    source: str
    subject_id: str
    user_hash: str
    ordering: int
    message_high_water: int
    locator: str = ""


class ImportCheckpoint:
    """Small SQLite job database containing identifiers and counters only."""

    def __init__(self, path: Path) -> None:
        if path.exists() and path.is_symlink():
            raise OSError("checkpoint must not be a symlink")
        _private_directory(path.parent)
        self.path = path
        self.connection = sqlite3.connect(path, timeout=30.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()
        if os.name == "posix":
            os.chmod(path, 0o600)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> ImportCheckpoint:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS job (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                checkpoint_version INTEGER NOT NULL,
                job_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                protocol TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_locator TEXT NOT NULL,
                source_locator_hash TEXT NOT NULL,
                profile_hash TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_progress_at REAL NOT NULL,
                discovered INTEGER NOT NULL DEFAULT 0,
                eligible INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                quarantined INTEGER NOT NULL DEFAULT 0,
                delivered INTEGER NOT NULL DEFAULT 0,
                deduplicated INTEGER NOT NULL DEFAULT 0,
                checkpointed INTEGER NOT NULL DEFAULT 0,
                processed_windows INTEGER NOT NULL DEFAULT 0,
                failed_windows INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                pending_review INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                projected_entities INTEGER NOT NULL DEFAULT 0,
                published_claims INTEGER NOT NULL DEFAULT 0,
                stub_count INTEGER NOT NULL DEFAULT 0,
                pending_resolution INTEGER NOT NULL DEFAULT 0,
                projection_pending INTEGER NOT NULL DEFAULT 0,
                peak_rss_bytes INTEGER NOT NULL DEFAULT 0,
                error_class TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS sessions (
                external_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                user_hash TEXT NOT NULL,
                source_order INTEGER NOT NULL,
                message_high_water INTEGER NOT NULL,
                locator TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'pending',
                next_message INTEGER NOT NULL DEFAULT 0,
                session_digest TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_order_idx
                ON sessions(state, source_order, external_id);
            CREATE TABLE IF NOT EXISTS acknowledgements (
                event_id TEXT PRIMARY KEY,
                external_id TEXT NOT NULL,
                message_boundary_start INTEGER NOT NULL,
                message_boundary_end INTEGER NOT NULL,
                phase TEXT NOT NULL,
                duplicate INTEGER NOT NULL,
                retry_count INTEGER NOT NULL,
                acknowledged_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS legacy_batches (
                batch_id TEXT PRIMARY KEY,
                selected INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                delivered INTEGER NOT NULL DEFAULT 0,
                checkpoint_time REAL NOT NULL DEFAULT 0
            );
            """
        )
        # v1.3 attaches to the existing v1.2 content-free checkpoint.  Add only
        # aggregate server counters in place; never replace or copy the database.
        columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(job)")
        }
        for name in (
            "failed_windows",
            "projected_entities",
            "published_claims",
            "stub_count",
            "pending_resolution",
            "projection_pending",
        ):
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE job ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )
        self.connection.commit()

    @classmethod
    def create_or_attach(
        cls,
        hermes_home: Path,
        *,
        source_kind: str,
        source_locator: str,
        agent_id: str,
        batch_id: str | None = None,
        legacy_batches: list[dict[str, Any]] | None = None,
    ) -> ImportCheckpoint:
        imports = _private_directory(hermes_home / "substrate_wiki" / "imports")
        jobs = _private_directory(imports / "jobs")
        key = hashlib.sha256(
            f"{hermes_home.resolve()}\0{source_kind}\0{source_locator}".encode()
        ).hexdigest()[:24]
        pointer = imports / f"active-{key}.json"
        try:
            value = json.loads(pointer.read_text(encoding="utf-8"))
            active_id = str(value.get("job_id") or "") if isinstance(value, dict) else ""
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            active_id = ""
        if active_id:
            candidate = jobs / active_id / "checkpoint.db"
            if candidate.is_file() and not candidate.is_symlink():
                checkpoint = cls(candidate)
                if checkpoint.job().get("state") not in TERMINAL_STATES:
                    return checkpoint
                checkpoint.close()

        job_id = uuid.uuid4().hex
        selected_batch = batch_id or uuid.uuid4().hex
        checkpoint = cls(_private_directory(jobs / job_id) / "checkpoint.db")
        moment = _now()
        checkpoint.connection.execute(
            """INSERT INTO job (
                singleton, checkpoint_version, job_id, batch_id, protocol,
                source_kind, source_locator, source_locator_hash, profile_hash, agent_id,
                state, created_at, updated_at, last_progress_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'created', ?, ?, ?)""",
            (
                CHECKPOINT_VERSION,
                job_id,
                selected_batch,
                PROTOCOL,
                source_kind,
                source_locator,
                hashlib.sha256(source_locator.encode()).hexdigest(),
                hashlib.sha256(os.fspath(hermes_home.resolve()).encode()).hexdigest(),
                agent_id or "default",
                moment,
                moment,
                moment,
            ),
        )
        for item in legacy_batches or []:
            legacy_id = str(item.get("batch_id") or "")[:128]
            if not legacy_id:
                continue
            checkpoint.connection.execute(
                """INSERT OR REPLACE INTO legacy_batches
                   (batch_id, selected, processed, delivered, checkpoint_time)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    legacy_id,
                    int(legacy_id == selected_batch),
                    max(0, int(item.get("processed", 0))),
                    max(0, int(item.get("delivered", 0))),
                    float(item.get("checkpoint_time", 0.0)),
                ),
            )
        checkpoint.connection.commit()
        secure_atomic_json_write(pointer, {"job_id": job_id})
        return checkpoint

    def job(self) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM job WHERE singleton = 1").fetchone()
        if row is None:
            raise RuntimeError("checkpoint has no job")
        return dict(row)

    def set_state(self, state: str, *, error_class: str = "") -> None:
        moment = _now()
        self.connection.execute(
            "UPDATE job SET state=?, error_class=?, updated_at=?, last_progress_at=? WHERE singleton=1",
            (state, error_class[:128], moment, moment),
        )
        self.connection.commit()

    def set_inventory(self, *, discovered: int, eligible: int, skipped: int, quarantined: int) -> None:
        moment = _now()
        self.connection.execute(
            """UPDATE job SET discovered=?, eligible=?, skipped=?, quarantined=?,
               state='ready', updated_at=?, last_progress_at=? WHERE singleton=1""",
            (discovered, eligible, skipped, quarantined, moment, moment),
        )
        self.connection.commit()

    def add_session(self, session: SessionDescriptor) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO sessions (
                external_id, source, subject_id, user_hash, source_order,
                message_high_water, locator, state, next_message, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
            (
                session.external_id,
                session.source,
                session.subject_id,
                session.user_hash,
                session.ordering,
                session.message_high_water,
                session.locator,
                _now(),
            ),
        )

    def finish_discovery(self) -> None:
        self.connection.commit()

    def sessions(self, *, include_complete: bool = False) -> Iterator[SessionDescriptor]:
        condition = "1=1" if include_complete else "state != 'complete'"
        cursor = self.connection.execute(
            f"SELECT * FROM sessions WHERE {condition} ORDER BY source_order, external_id"  # noqa: S608
        )
        for row in cursor:
            yield SessionDescriptor(
                external_id=str(row["external_id"]),
                source=str(row["source"]),
                subject_id=str(row["subject_id"]),
                user_hash=str(row["user_hash"]),
                ordering=int(row["source_order"]),
                message_high_water=int(row["message_high_water"]),
                locator=str(row["locator"]),
            )

    def next_message(self, external_id: str) -> int:
        row = self.connection.execute(
            "SELECT next_message FROM sessions WHERE external_id=?", (external_id,)
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def session_progress(self, external_id: str) -> tuple[int, str]:
        row = self.connection.execute(
            "SELECT next_message, session_digest FROM sessions WHERE external_id=?",
            (external_id,),
        ).fetchone()
        return (int(row[0]), str(row[1])) if row is not None else (0, "")

    def acknowledged(self, event_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM acknowledgements WHERE event_id=?", (event_id,)
        ).fetchone()
        return row is not None

    def acknowledge(
        self,
        *,
        event_id: str,
        external_id: str,
        boundary_start: int,
        boundary_end: int,
        phase: str,
        duplicate: bool,
        retry_count: int = 0,
        session_digest: str = "",
    ) -> None:
        moment = _now()
        with self.connection:
            inserted = self.connection.execute(
                """INSERT OR IGNORE INTO acknowledgements (
                    event_id, external_id, message_boundary_start, message_boundary_end,
                    phase, duplicate, retry_count, acknowledged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    external_id,
                    boundary_start,
                    boundary_end,
                    phase,
                    int(duplicate),
                    retry_count,
                    moment,
                ),
            ).rowcount
            if inserted:
                self.connection.execute(
                    """UPDATE job SET delivered=delivered+1,
                       deduplicated=deduplicated+?, checkpointed=checkpointed+1,
                       state='running', updated_at=?, last_progress_at=? WHERE singleton=1""",
                    (int(duplicate), moment, moment),
                )
            if phase == "message":
                self.connection.execute(
                    """UPDATE sessions SET next_message=MAX(next_message, ?),
                       session_digest=CASE WHEN ? != '' THEN ? ELSE session_digest END,
                       state='running', updated_at=? WHERE external_id=?""",
                    (boundary_end, session_digest, session_digest, moment, external_id),
                )
            elif phase == "session_end":
                self.connection.execute(
                    "UPDATE sessions SET state='complete', updated_at=? WHERE external_id=?",
                    (moment, external_id),
                )

    def mark_completed_sessions(self, external_ids: set[str]) -> None:
        with self.connection:
            for external_id in external_ids:
                self.connection.execute(
                    "UPDATE sessions SET state='complete', updated_at=? WHERE external_id=?",
                    (_now(), external_id),
                )

    def legacy_batch_ids(self) -> list[str]:
        return [
            str(row[0])
            for row in self.connection.execute(
                "SELECT batch_id FROM legacy_batches ORDER BY selected DESC, batch_id"
            )
        ]

    def update_remote(self, status: dict[str, Any]) -> None:
        """Persist only supplied, content-free aggregate server status fields.

        Status responses can be temporarily partial during a rolling server upgrade.
        Missing fields therefore retain their last durable value instead of being
        reset to zero.  Error classes are restricted to the public sanitized-code
        grammar before they can reach the local checkpoint or CLI output.
        """
        assignments: list[str] = []
        values: list[int | str] = []
        for field in _REMOTE_COUNTER_FIELDS:
            if field not in status or isinstance(status[field], bool):
                continue
            try:
                value = min(_MAX_COUNTER, max(0, int(status[field])))
            except (OverflowError, TypeError, ValueError):
                continue
            assignments.append(f"{field}=?")
            values.append(value)
        if "error_class" in status:
            raw_error = status.get("error_class")
            error_class = (
                raw_error
                if isinstance(raw_error, str)
                and (not raw_error or _ERROR_CLASS_PATTERN.fullmatch(raw_error))
                else "server_error"
            )
            assignments.append("error_class=?")
            values.append(error_class)
        if status.get("complete") is True:
            current = self.job()
            failed = status.get("failed", current["failed"])
            failed_windows = status.get("failed_windows", current["failed_windows"])
            try:
                has_failures = int(failed) > 0 or int(failed_windows) > 0
            except (TypeError, ValueError):
                has_failures = True
            assignments.append("state=?")
            values.append("complete_with_failures" if has_failures else "complete")
        if assignments:
            values.extend([int(_now()), int(_now())])
            self.connection.execute(
                f"UPDATE job SET {', '.join(assignments)}, updated_at=?, last_progress_at=? WHERE singleton=1",  # noqa: S608
                values,
            )
            self.connection.commit()

    def update_peak_rss(self, value: int) -> None:
        self.connection.execute(
            "UPDATE job SET peak_rss_bytes=MAX(peak_rss_bytes, ?), updated_at=? WHERE singleton=1",
            (max(0, int(value)), _now()),
        )
        self.connection.commit()

    def status(self) -> dict[str, Any]:
        row = self.job()
        return {
            "job_id": row["job_id"],
            "batch_id": row["batch_id"],
            "state": row["state"],
            "legacy_batch_ids": self.legacy_batch_ids(),
            "discovered": int(row["discovered"]),
            "eligible": int(row["eligible"]),
            "skipped": int(row["skipped"]),
            "quarantined": int(row["quarantined"]),
            "delivered": int(row["delivered"]),
            "deduplicated": int(row["deduplicated"]),
            "checkpointed": int(row["checkpointed"]),
            "processed_windows": int(row["processed_windows"]),
            "failed_windows": int(row["failed_windows"]),
            "processed": int(row["processed"]),
            "pending_review": int(row["pending_review"]),
            "failed": int(row["failed"]),
            "projected_entities": int(row["projected_entities"]),
            "published_claims": int(row["published_claims"]),
            "stub_count": int(row["stub_count"]),
            "pending_resolution": int(row["pending_resolution"]),
            "projection_pending": int(row["projection_pending"]),
            "peak_rss_bytes": int(row["peak_rss_bytes"]),
            "last_progress_at": float(row["last_progress_at"]),
            "complete": str(row["state"]) in {"complete", "complete_with_failures"},
            "error_class": str(row["error_class"]),
        }


def discover_legacy_checkpoints(imports_root: Path) -> list[dict[str, Any]]:
    """Read v1.1 JSON checkpoints without changing them or exposing content."""
    found: list[dict[str, Any]] = []
    if not imports_root.is_dir() or imports_root.is_symlink():
        return found
    for path in imports_root.glob("*.json"):
        if path.name.startswith("active-") or path.is_symlink():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            batch_id = str(value.get("batch_id") or path.stem)[:128]
            completed = value.get("completed_sessions")
            completed_ids = {
                str(item) for item in completed if isinstance(item, (str, int))
            } if isinstance(completed, list) else set()
            found.append(
                {
                    "batch_id": batch_id,
                    "completed_sessions": completed_ids,
                    "checkpoint_time": path.stat().st_mtime,
                }
            )
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
    return found
