#!/usr/bin/env python3
"""Measure a privacy-safe stream-v2 Hermes migration baseline.

The transfer path uses the production importer and SQLite/JSONL readers. Hosted
provider behavior is never invoked: downstream provider, queue, resolution,
projection and summary work is an explicitly labeled deterministic simulator.
Only aggregate counters and timings are retained.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import queue
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest import mock

from substrate_wiki.history import (
    HermesHistoryImporter,
    HermesJSONLHistorySource,
    HermesSQLiteHistorySource,
)

ROOT = Path(__file__).resolve().parents[1]

PHASE_NAMES = (
    "discovery",
    "source_reading",
    "normalization_redaction",
    "request_encoding",
    "network_wait",
    "server_persistence",
    "queue_delay",
    "provider_extraction",
    "entity_resolution",
    "projection_indexing",
    "summary_reduction",
)
_CONTENT_FIELDS = {
    "content",
    "messages",
    "message",
    "prompt",
    "prompts",
    "transcript",
    "transcripts",
    "secret",
    "secrets",
    "password",
    "api_key",
}
_IDENTIFIER_FIELDS = {
    "agent_id",
    "batch_id",
    "event_id",
    "job_id",
    "message_id",
    "session_id",
    "source_id",
    "source_locator",
    "user_id",
}
_SECRET_VALUE_MARKERS = (
    "api_key=",
    "authorization:",
    "bearer ",
    "ghp_",
    "password=",
    "sk-",
)
_CANARY = "sk-BENCHMARKCANARY1234567890"


class PhaseMetrics:
    def __init__(self) -> None:
        self.values = {name: 0.0 for name in PHASE_NAMES}
        self._lock = threading.Lock()

    def add(self, name: str, elapsed: float) -> None:
        with self._lock:
            self.values[name] += max(0.0, elapsed)

    def measured(self, name: str):  # type: ignore[no-untyped-def]
        metrics = self

        class Timer:
            def __enter__(self) -> None:
                self.started = time.perf_counter()

            def __exit__(self, *_args: object) -> None:
                metrics.add(name, time.perf_counter() - self.started)

        return Timer()


class InstrumentedSource:
    def __init__(self, source: Any, metrics: PhaseMetrics) -> None:
        self.source = source
        self.metrics = metrics
        self.source_kind = str(source.source_kind)
        self.source_locator = str(source.source_locator)
        self._yielded_at: float | None = None
        self._lock = threading.Lock()

    def discover(self, checkpoint: Any) -> dict[str, int]:
        with self.metrics.measured("discovery"):
            return self.source.discover(checkpoint)

    def iter_messages(self, session: Any, *, start: int) -> Iterator[dict[str, Any]]:
        iterator = iter(self.source.iter_messages(session, start=start))
        while True:
            began = time.perf_counter()
            try:
                value = next(iterator)
            except StopIteration:
                self.metrics.add("source_reading", time.perf_counter() - began)
                return
            self.metrics.add("source_reading", time.perf_counter() - began)
            with self._lock:
                self._yielded_at = time.perf_counter()
            yield value

    def take_normalization_elapsed(self) -> float:
        with self._lock:
            yielded_at, self._yielded_at = self._yielded_at, None
        return 0.0 if yielded_at is None else time.perf_counter() - yielded_at


class AggregateSink:
    """Content-free durable sink plus deterministic downstream simulator queue."""

    def __init__(
        self,
        database: Path,
        source: InstrumentedSource,
        metrics: PhaseMetrics,
        *,
        network_latency: float,
    ) -> None:
        self.source = source
        self.metrics = metrics
        self.network_latency = network_latency
        self.connection = sqlite3.connect(database, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            "CREATE TABLE received(sequence INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, encoded_bytes INTEGER NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE projected(sequence INTEGER PRIMARY KEY, resolution_digest TEXT NOT NULL)"
        )
        self.connection.commit()
        self.requests = 0
        self.retries = 0
        self.encoded_bytes = 0
        self.redaction_failures = 0
        self.redacted_events = 0
        self.duplicate_acks = 0
        self.replay_event: dict[str, Any] | None = None
        self.queue: list[tuple[int, float]] = []
        self.work_queue: queue.Queue[tuple[int, float] | None] = queue.Queue()
        self.max_queue_depth = 0
        self._lock = threading.Lock()

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
        self.metrics.add("normalization_redaction", self.source.take_normalization_elapsed())
        event = kwargs.get("body")
        if not isinstance(event, dict):
            raise TypeError("benchmark sink requires a mapping event")
        with self.metrics.measured("request_encoding"):
            encoded = json.dumps(
                event, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        with self.metrics.measured("network_wait"):
            if self.network_latency:
                time.sleep(self.network_latency)
        kind = str(event.get("kind") or "unknown")
        event_id = str(event.get("event_id") or hashlib.sha256(encoded).hexdigest())
        with self.metrics.measured("server_persistence"):
            with self._lock:
                if self.connection.execute(
                    "SELECT 1 FROM received WHERE event_id = ?", (event_id,)
                ).fetchone():
                    self.duplicate_acks += 1
                    return {"duplicate": True}
                if _CANARY.encode() in encoded:
                    self.redaction_failures += 1
                if b"[REDACTED]" in encoded:
                    self.redacted_events += 1
                self.requests += 1
                sequence = self.requests
                self.encoded_bytes += len(encoded)
                self.connection.execute(
                    "INSERT INTO received(sequence, event_id, kind, encoded_bytes) VALUES (?, ?, ?, ?)",
                    (sequence, event_id, kind, len(encoded)),
                )
                self.connection.commit()
                if kind == "session_end":
                    if self.replay_event is None:
                        self.replay_event = dict(event)
                    item = (sequence, time.perf_counter())
                    self.queue.append(item)
                    self.work_queue.put(item)
                    self.max_queue_depth = max(self.max_queue_depth, self.work_queue.qsize())
        return {"duplicate": False}

    def import_status(self, batch_id: str) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "processed": len(self.queue),
            "processed_windows": len(self.queue),
            "pending_review": 0,
            "failed": 0,
            "complete": True,
        }

    def close(self) -> None:
        self.connection.close()

    def finish_transfer(self) -> None:
        self.work_queue.put(None)

    def durable_state(self) -> dict[str, Any]:
        """Fingerprint every durable or queued side effect after workers settle."""
        with self._lock:
            received = self.connection.execute(
                "SELECT sequence, event_id, kind, encoded_bytes FROM received ORDER BY sequence"
            ).fetchall()
            projected = self.connection.execute(
                "SELECT sequence, resolution_digest FROM projected ORDER BY sequence"
            ).fetchall()
            received_digest = hashlib.sha256(
                json.dumps(received, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            projected_digest = hashlib.sha256(
                json.dumps(projected, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            with self.work_queue.mutex:
                work_queue_items = tuple(
                    None if item is None else item[0] for item in self.work_queue.queue
                )
            return {
                "duplicate_acks": self.duplicate_acks,
                "encoded_bytes": self.encoded_bytes,
                "max_queue_depth": self.max_queue_depth,
                "projected_digest": projected_digest,
                "projected_rows": len(projected),
                "queue_items": tuple(sequence for sequence, _created_at in self.queue),
                "received_digest": received_digest,
                "received_rows": len(received),
                "redacted_events": self.redacted_events,
                "redaction_failures": self.redaction_failures,
                "requests": self.requests,
                "work_queue_items": work_queue_items,
            }


def _message(case_id: str, session_index: int, message_index: int, size: int) -> str:
    prefix = (
        f"synthetic fixture={case_id} session={session_index:08d} item={message_index:04d} "
        f"api_key={_CANARY} "
    )
    if len(prefix) >= size:
        return prefix[:size]
    return prefix + ("x" * (size - len(prefix)))


def _generate_fixture(case: Mapping[str, Any], output: Path) -> None:
    sessions = int(case["sessions"])
    messages_per_session = int(case["messages_per_session"])
    ordinary_size = int(case["message_bytes"])
    oversized = int(case.get("oversized_message_bytes", 0))
    case_id = str(case["id"])
    if case["source"] == "sqlite":
        connection = sqlite3.connect(output)
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
                """
            )
            for session_index in range(sessions):
                session_id = f"baseline-{case_id}-{session_index:08d}"
                connection.execute(
                    "INSERT INTO sessions VALUES (?, 'cli', NULL, NULL, ?)",
                    (session_id, float(session_index)),
                )
                for message_index in range(messages_per_session):
                    size = (
                        oversized
                        if oversized and session_index == message_index == 0
                        else ordinary_size
                    )
                    connection.execute(
                        "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                        (
                            session_id,
                            "user" if message_index % 2 == 0 else "assistant",
                            _message(case_id, session_index, message_index, size),
                            float(message_index),
                        ),
                    )
            connection.commit()
        finally:
            connection.close()
        return
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for session_index in range(sessions):
            messages = []
            for message_index in range(messages_per_session):
                size = (
                    oversized
                    if oversized and session_index == message_index == 0
                    else ordinary_size
                )
                messages.append(
                    {
                        "role": "user" if message_index % 2 == 0 else "assistant",
                        "content": _message(case_id, session_index, message_index, size),
                        "timestamp": float(message_index),
                    }
                )
            json.dump(
                {
                    "id": f"baseline-{case_id}-{session_index:08d}",
                    "source": "cli",
                    "messages": messages,
                },
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            stream.write("\n")


def _current_rss_bytes() -> int:
    """Return current process RSS, avoiding inherited lifetime high-water marks."""

    status_path = Path("/proc/self/status")
    try:
        for line in status_path.read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (IndexError, OSError, ValueError):
        pass
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        resident_pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[1])
        return resident_pages * page_size
    except (IndexError, OSError, ValueError):
        return 0


class _PeakRSSSampler:
    """Sample current RSS for one isolated case worker."""

    def __init__(self, *, interval_seconds: float = 0.005) -> None:
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_until_stopped, daemon=True)
        self.peak_bytes = _current_rss_bytes()

    def _sample(self) -> None:
        self.peak_bytes = max(self.peak_bytes, _current_rss_bytes())

    def _sample_until_stopped(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._sample()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> int:
        self._sample()
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._sample()
        return self.peak_bytes


def _payload_bytes(case: Mapping[str, Any]) -> int:
    sessions = int(case["sessions"])
    messages = int(case["messages_per_session"])
    ordinary = sessions * messages * int(case["message_bytes"])
    oversized = int(case.get("oversized_message_bytes", 0))
    return ordinary - (int(case["message_bytes"]) if oversized else 0) + oversized


def _run_downstream(
    sink: AggregateSink,
    metrics: PhaseMetrics,
    *,
    concurrency: int,
    expected_windows: int,
    provider: Mapping[str, Any],
    started: float,
) -> tuple[float, float, dict[str, Any]]:
    first_usable: list[float] = []
    queue_ages: list[float] = []
    provider_requests = 0
    provider_retries = 0
    provider_lock = threading.Lock()
    worker_threads: set[int] = set()
    active_workers = 0
    max_in_flight = 0
    startup_parties = min(concurrency, expected_windows)
    startup_barrier = threading.Barrier(startup_parties)
    tasks_started = 0
    latency = float(provider["observed_latency_seconds"])
    retry_every = int(provider["retry_every_requests"])
    summary_every = int(provider["summary_every_windows"])

    def process(item: tuple[int, float]) -> None:
        nonlocal active_workers, max_in_flight, provider_requests, provider_retries, tasks_started
        sequence, enqueued_at = item
        with provider_lock:
            worker_threads.add(threading.get_ident())
            active_workers += 1
            max_in_flight = max(max_in_flight, active_workers)
            task_number = tasks_started
            tasks_started += 1
        try:
            if task_number < startup_parties:
                try:
                    startup_barrier.wait(timeout=1.0)
                except threading.BrokenBarrierError:
                    pass
            age = time.perf_counter() - enqueued_at
            with provider_lock:
                queue_ages.append(age)
            metrics.add("queue_delay", age)
            with metrics.measured("provider_extraction"):
                time.sleep(latency)
                retry = retry_every > 0 and sequence % retry_every == 0
                if retry:
                    time.sleep(latency)
            with provider_lock:
                provider_requests += 1
                provider_retries += int(retry)
            with metrics.measured("entity_resolution"):
                digest = hashlib.sha256(f"synthetic-resolution:{sequence}".encode()).hexdigest()
            with metrics.measured("projection_indexing"):
                with sink._lock:
                    sink.connection.execute(
                        "INSERT INTO projected(sequence, resolution_digest) VALUES (?, ?)",
                        (sequence, digest),
                    )
                    sink.connection.commit()
            if sequence % summary_every == 0:
                with metrics.measured("summary_reduction"):
                    hashlib.sha256(f"synthetic-summary:{sequence}".encode()).digest()
            with provider_lock:
                if not first_usable:
                    first_usable.append(time.perf_counter() - started)
        finally:
            with provider_lock:
                active_workers -= 1

    futures: list[concurrent.futures.Future[None]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        while True:
            item = sink.work_queue.get()
            if item is None:
                break
            futures.append(executor.submit(process, item))
        for future in futures:
            future.result()
    if provider_requests:
        with metrics.measured("summary_reduction"):
            hashlib.sha256(f"synthetic-final-summary:{provider_requests}".encode()).digest()
    fully_ready = time.perf_counter() - started
    rpm = int(provider["modeled_requests_per_minute"])
    cost = float(provider["modeled_cost_usd_per_request"])
    modeled_provider_seconds = provider_requests * 60.0 / rpm if rpm else 0.0
    return (
        first_usable[0] if first_usable else fully_ready,
        fully_ready,
        {
            "requests": provider_requests,
            "retries": provider_retries,
            "observed_time_seconds": round(metrics.values["provider_extraction"], 6),
            "modeled_quota_seconds": round(modeled_provider_seconds, 6),
            "modeled_cost_usd": round(provider_requests * cost, 6),
            "quota_requests_per_minute": rpm,
            "max_queue_depth": sink.max_queue_depth,
            "max_queue_age_seconds": round(max(queue_ages, default=0.0), 6),
            "worker_threads_used": len(worker_threads),
            "max_in_flight": max_in_flight,
        },
    )


def _run_case(
    case: Mapping[str, Any],
    concurrency: int,
    provider: Mapping[str, Any],
    fixture: Path,
) -> dict[str, Any]:
    rss_sampler = _PeakRSSSampler()
    rss_sampler.start()
    metrics = PhaseMetrics()
    base_source = (
        HermesSQLiteHistorySource(fixture)
        if case["source"] == "sqlite"
        else HermesJSONLHistorySource(fixture)
    )
    source = InstrumentedSource(base_source, metrics)
    sink = AggregateSink(
        fixture.parent / "server.db",
        source,
        metrics,
        network_latency=float(provider["observed_latency_seconds"]),
    )
    importer = HermesHistoryImporter(
        hermes_home=fixture.parent / "hermes-home",
        client=sink,  # type: ignore[arg-type]
        source=source,
        agent_id="baseline-agent",
    )
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    terminal_failures = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as controller:
            downstream_future = controller.submit(
                _run_downstream,
                sink,
                metrics,
                concurrency=concurrency,
                expected_windows=int(case["sessions"]),
                provider=provider,
                started=wall_started,
            )
            try:
                importer.run(wait=False)
                transferred = time.perf_counter() - wall_started
                if sink.replay_event is None:
                    raise RuntimeError("benchmark importer emitted no replayable session_end event")
            finally:
                sink.finish_transfer()
            first_usable, fully_ready, downstream = downstream_future.result()
            before_replay = sink.durable_state()
            replay = sink.request("POST", "/api/v1/hermes/replay-probe", body=sink.replay_event)
            after_replay = sink.durable_state()
            expected_after_replay = dict(before_replay)
            expected_after_replay["duplicate_acks"] += 1
            duplicate_side_effects = int(expected_after_replay != after_replay)
            duplicate_acks = int(
                replay.get("duplicate") is True
                and after_replay["duplicate_acks"] - before_replay["duplicate_acks"] == 1
            )
    except Exception:
        terminal_failures = 1
        raise
    finally:
        if importer.checkpoint is not None:
            importer.checkpoint.close()
        peak_rss_bytes = rss_sampler.stop()
    cpu_seconds = time.process_time() - cpu_started
    source_bytes = fixture.stat().st_size
    payload_bytes = _payload_bytes(case)
    windows = int(case["sessions"])
    wall_seconds = fully_ready
    projected = int(sink.connection.execute("SELECT COUNT(*) FROM projected").fetchone()[0])
    sink.close()
    events_per_second = sink.requests / transferred if transferred else 0.0
    mib_per_second = payload_bytes / (1024 * 1024) / transferred if transferred else 0.0
    windows_per_minute = windows * 60.0 / fully_ready if fully_ready else 0.0
    result = {
        "case_id": str(case["id"]),
        "fixture_kind": str(case["fixture_kind"]),
        "concurrency": concurrency,
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "source_file_bytes": source_bytes,
        "input_bytes": payload_bytes,
        "windows": windows,
        "events": sink.requests,
        "wall_time_seconds": round(wall_seconds, 6),
        "cpu_seconds": round(cpu_seconds, 6),
        "peak_rss_bytes": peak_rss_bytes,
        "events_per_second": round(events_per_second, 3),
        "mib_per_second": round(mib_per_second, 3),
        "windows_per_minute": round(windows_per_minute, 3),
        "requests": sink.requests,
        "retries": sink.retries + int(downstream["retries"]),
        "terminal_failures": terminal_failures,
        "phase_seconds": {name: round(metrics.values[name], 6) for name in PHASE_NAMES},
        "traces": [
            {"phase": name, "seconds": round(metrics.values[name], 6)} for name in PHASE_NAMES
        ],
        "lifecycle_seconds": {
            "transferred": round(transferred, 6),
            "first_usable": round(first_usable, 6),
            "fully_ready": round(fully_ready, 6),
        },
        "queue": {
            "max_depth": int(downstream["max_queue_depth"]),
            "max_age_seconds": downstream["max_queue_age_seconds"],
        },
        "provider": {
            key: downstream[key]
            for key in (
                "requests",
                "retries",
                "observed_time_seconds",
                "modeled_quota_seconds",
                "modeled_cost_usd",
                "quota_requests_per_minute",
                "worker_threads_used",
                "max_in_flight",
            )
        },
        "integrity": {
            "expected_windows": windows,
            "projected_windows": projected,
            "redacted_events": sink.redacted_events,
            "redaction_failures": sink.redaction_failures,
            "idempotency_replays": 1,
            "idempotency_replay_kind": "session_end",
            "idempotency_state_components": sorted(before_replay),
            "duplicate_acks": duplicate_acks,
            "duplicate_side_effects": duplicate_side_effects,
            "complete": (
                projected == windows
                and sink.redacted_events > 0
                and sink.redaction_failures == 0
                and duplicate_acks == 1
                and duplicate_side_effects == 0
            ),
        },
        "quality": {
            "projection_failures": 0,
            "terminal_failures": terminal_failures,
        },
    }
    return result


def evaluate_budget(run: Mapping[str, Any], budget: Mapping[str, Any]) -> list[str]:
    """Return content-free failures for one measured run and one explicit budget."""

    lifecycle = run["lifecycle_seconds"]
    integrity = run["integrity"]
    quality = run["quality"]
    provider = run["provider"]
    expected = max(1, int(integrity["expected_windows"]))
    integrity_ratio = int(integrity["projected_windows"]) / expected
    observed = {
        "transferred_seconds": float(lifecycle["transferred"]),
        "first_usable_seconds": float(lifecycle["first_usable"]),
        "fully_ready_seconds": float(lifecycle["fully_ready"]),
        "peak_rss_bytes": float(run["peak_rss_bytes"]),
        "cpu_seconds": float(run["cpu_seconds"]),
        "terminal_failures": float(run["terminal_failures"]),
        "projection_failures": float(quality["projection_failures"]),
        "redaction_failures": float(integrity["redaction_failures"]),
        "provider_retries": float(provider["retries"]),
        "modeled_cost_usd": float(provider["modeled_cost_usd"]),
        "modeled_quota_seconds": float(provider["modeled_quota_seconds"]),
    }
    failures = [
        f"{name}={value} exceeds {budget['max_' + name]}"
        for name, value in observed.items()
        if value > float(budget["max_" + name])
    ]
    if integrity_ratio < float(budget["min_integrity_ratio"]):
        failures.append(
            f"integrity_ratio={integrity_ratio:.6f} below {budget['min_integrity_ratio']}"
        )
    if int(integrity.get("redacted_events", 0)) < int(budget.get("min_redacted_events", 1)):
        failures.append(
            f"redacted_events={integrity.get('redacted_events', 0)} below "
            f"{budget.get('min_redacted_events', 1)}"
        )
    if int(integrity.get("duplicate_acks", 0)) < int(budget.get("min_duplicate_acks", 1)):
        failures.append("duplicate_acks below required idempotency proof")
    if int(integrity.get("duplicate_side_effects", 1)) > int(
        budget.get("max_duplicate_side_effects", 0)
    ):
        failures.append("duplicate_side_effects exceeds zero")
    if not integrity.get("complete"):
        failures.append("integrity_complete=false")
    return failures


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported benchmark manifest")
    if manifest.get("protocol") != "stream-v2":
        raise ValueError("benchmark manifest must select stream-v2")
    if manifest.get("concurrency") != [1, 2, 3, 4]:
        raise ValueError("benchmark manifest must cover concurrency 1-4")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("benchmark manifest must define cases")
    case_id_pattern = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
    for case in cases:
        if not isinstance(case, dict) or not case_id_pattern.fullmatch(str(case.get("id", ""))):
            raise ValueError("benchmark case id must be a safe lowercase slug")
    return manifest


def validate_receipt(receipt: Any) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = str(key).casefold()
                if normalized_key in _CONTENT_FIELDS or normalized_key in _IDENTIFIER_FIELDS:
                    raise ValueError("benchmark receipts must remain content-free")
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            normalized_value = value.casefold()
            if _CANARY.casefold() in normalized_value or any(
                marker in normalized_value for marker in _SECRET_VALUE_MARKERS
            ):
                raise ValueError("benchmark receipts must remain content-free")

    visit(receipt)
    if not isinstance(receipt, dict) or "runs" not in receipt:
        raise ValueError("benchmark receipt is incomplete")
    for run in receipt["runs"]:
        if set(run.get("phase_seconds", {})) != set(PHASE_NAMES):
            raise ValueError("benchmark receipt has incomplete phase instrumentation")
        lifecycle = run.get("lifecycle_seconds", {})
        if not (
            0 <= lifecycle.get("transferred", -1) < lifecycle.get("fully_ready", -1)
            and 0 <= lifecycle.get("first_usable", -1) < lifecycle.get("fully_ready", -1)
            and lifecycle.get("transferred") != lifecycle.get("first_usable")
        ):
            raise ValueError("benchmark lifecycle boundaries are invalid")


def _tolerant_max(runs: list[Mapping[str, Any]], path: tuple[str, ...]) -> float:
    values = []
    for run in runs:
        value: Any = run
        for component in path:
            value = value[component]
        values.append(float(value))
    return round(max(values) * 1.5 + 2.0, 6)


def derive_budget(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Derive per-case beta ceilings from the complete retained concurrency matrix."""

    validate_receipt(receipt)
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for run in receipt["runs"]:
        grouped.setdefault(str(run["case_id"]), []).append(run)
    if not grouped:
        raise ValueError("cannot derive a budget from an empty receipt")
    case_budgets: dict[str, dict[str, Any]] = {}
    for case_id, runs in sorted(grouped.items()):
        if sorted(int(run["concurrency"]) for run in runs) != [1, 2, 3, 4]:
            raise ValueError(f"budget evidence must cover concurrency 1-4 exactly: {case_id}")

        case_budgets[case_id] = {
            "evidence_runs": 4,
            "max_transferred_seconds": _tolerant_max(runs, ("lifecycle_seconds", "transferred")),
            "max_first_usable_seconds": _tolerant_max(runs, ("lifecycle_seconds", "first_usable")),
            "max_fully_ready_seconds": _tolerant_max(runs, ("lifecycle_seconds", "fully_ready")),
            "max_peak_rss_bytes": 256 * 1024 * 1024,
            "max_cpu_seconds": _tolerant_max(runs, ("cpu_seconds",)),
            "max_terminal_failures": 0,
            "max_projection_failures": 0,
            "max_redaction_failures": 0,
            "min_redacted_events": min(int(run["integrity"]["redacted_events"]) for run in runs),
            "min_duplicate_acks": 1,
            "max_duplicate_side_effects": 0,
            "min_integrity_ratio": 1.0,
            "max_provider_retries": max(int(run["provider"]["retries"]) for run in runs),
            "max_modeled_cost_usd": max(float(run["provider"]["modeled_cost_usd"]) for run in runs),
            "max_modeled_quota_seconds": max(
                float(run["provider"]["modeled_quota_seconds"]) for run in runs
            ),
        }
    return {
        "schema_version": 1,
        "protocol": receipt["protocol"],
        "profile": receipt["profile"],
        "manifest_sha256": receipt["manifest_sha256"],
        "harness_sha256": receipt["harness_sha256"],
        "receipt_sha256": "",
        "derivation": {
            "evidence_runs": len(receipt["runs"]),
            "required_concurrency": [1, 2, 3, 4],
            "latency_cpu_multiplier": 1.5,
            "host_jitter_seconds": 2.0,
            "peak_rss_ceiling_bytes": 256 * 1024 * 1024,
        },
        "cases": case_budgets,
    }


def verify_budget(receipt: Mapping[str, Any], budget: Mapping[str, Any]) -> list[str]:
    """Verify derivation custody and every measured run against its case budget."""

    expected = derive_budget(receipt)
    receipt_bytes = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    expected["receipt_sha256"] = hashlib.sha256(receipt_bytes).hexdigest()
    failures = [] if budget == expected else ["budget artifact does not match canonical derivation"]
    case_budgets = budget.get("cases", {})
    if not isinstance(case_budgets, dict):
        return failures + ["budget cases must be a mapping"]
    for run in receipt["runs"]:
        case_budget = case_budgets.get(run["case_id"])
        if not isinstance(case_budget, dict):
            failures.append(f"missing budget for case {run['case_id']}")
            continue
        failures.extend(
            f"{run['case_id']}/c{run['concurrency']}: {failure}"
            for failure in evaluate_budget(run, case_budget)
        )
    return failures


def _receipt(manifest: Mapping[str, Any], manifest_path: Path, profile: str) -> dict[str, Any]:
    cases = {str(case["id"]): case for case in manifest["cases"]}
    selected = manifest["profiles"].get(profile)
    if not isinstance(selected, list) or not selected:
        raise ValueError(f"unknown or empty benchmark profile: {profile}")
    runs: list[dict[str, Any]] = []
    with (
        tempfile.TemporaryDirectory(prefix="substrate-migration-baseline-") as directory,
        mock.patch(
            "socket.socket",
            side_effect=RuntimeError("network access is disabled for this benchmark"),
        ),
    ):
        root = Path(directory)
        child_env = dict(os.environ)
        child_env["SUBSTRATE_BENCHMARK_NETWORK_DENIED"] = "1"
        for case_id in selected:
            case = cases[str(case_id)]
            for concurrency in manifest["concurrency"]:
                case_root = root / f"{case_id}-c{concurrency}"
                case_root.mkdir()
                fixture = case_root / (
                    "state.db" if case["source"] == "sqlite" else "official-export.jsonl"
                )
                subprocess.run(
                    [
                        sys.executable,
                        os.fspath(Path(__file__).resolve()),
                        "--generate-case",
                        str(case_id),
                        "--manifest",
                        os.fspath(manifest_path),
                        "--output",
                        os.fspath(fixture),
                    ],
                    check=True,
                    env=child_env,
                )
                row_path = case_root / "run.json"
                worker = subprocess.run(
                    [
                        sys.executable,
                        os.fspath(Path(__file__).resolve()),
                        "--run-case",
                        str(case_id),
                        "--concurrency",
                        str(concurrency),
                        "--manifest",
                        os.fspath(manifest_path),
                        "--fixture",
                        os.fspath(fixture),
                        "--output",
                        os.fspath(row_path),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=child_env,
                )
                if worker.returncode != 0:
                    raise RuntimeError(f"case worker failed: {case_id}/c{concurrency}")
                row = json.loads(row_path.read_text(encoding="utf-8"))
                if not isinstance(row, dict):
                    raise ValueError(f"case worker returned invalid row: {case_id}/c{concurrency}")
                runs.append(row)
    return {
        "schema_version": 1,
        "protocol": "stream-v2",
        "profile": profile,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest(),
        "hosted_calls": 0,
        "representative_provider": {
            "mode": "deterministic_simulation",
            "network_access": False,
            "quota_requests_per_minute": manifest["representative_provider"][
                "modeled_requests_per_minute"
            ],
            "cost_usd_per_request": manifest["representative_provider"][
                "modeled_cost_usd_per_request"
            ],
        },
        "runs": runs,
    }


def _install_network_denial() -> None:
    def deny_socket(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("network access is disabled for this benchmark")

    socket.socket = deny_socket  # type: ignore[assignment,misc]


def _probe_partial_failure() -> None:
    class ProbeSink:
        def __init__(self) -> None:
            self.work_queue: queue.Queue[tuple[int, float] | None] = queue.Queue()
            self.work_queue.put((1, time.perf_counter()))
            self.work_queue.put(None)
            self.max_queue_depth = 1
            self._lock = threading.Lock()
            self.connection = sqlite3.connect(":memory:", check_same_thread=False)
            self.connection.execute(
                "CREATE TABLE projected(sequence INTEGER PRIMARY KEY, resolution_digest TEXT NOT NULL)"
            )

    sink = ProbeSink()
    _run_downstream(
        sink,  # type: ignore[arg-type]
        PhaseMetrics(),
        concurrency=4,
        expected_windows=4,
        provider={
            "observed_latency_seconds": 0.0,
            "retry_every_requests": 0,
            "summary_every_windows": 100,
            "modeled_requests_per_minute": 60,
            "modeled_cost_usd_per_request": 0.0,
        },
        started=time.perf_counter(),
    )
    sink.connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "benchmarks" / "hermes-migration-manifest.json",
    )
    parser.add_argument("--profile", default="ci")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generate-case", help=argparse.SUPPRESS)
    parser.add_argument("--run-case", help=argparse.SUPPRESS)
    parser.add_argument("--concurrency", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--fixture", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--probe-network", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--probe-partial-failure", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.environ.get("SUBSTRATE_BENCHMARK_NETWORK_DENIED") == "1":
        _install_network_denial()
    if args.probe_network:
        socket.socket()
        return 0
    if args.probe_partial_failure:
        _probe_partial_failure()
        return 0
    manifest = load_manifest(args.manifest)
    if args.generate_case:
        cases = {str(case["id"]): case for case in manifest["cases"]}
        case = cases.get(args.generate_case)
        if case is None:
            parser.error("unknown fixture case")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        _generate_fixture(case, args.output)
        return 0
    if args.run_case:
        cases = {str(case["id"]): case for case in manifest["cases"]}
        case = cases.get(args.run_case)
        if case is None:
            parser.error("unknown fixture case")
        if args.concurrency not in manifest["concurrency"]:
            parser.error("unsupported concurrency")
        if args.fixture is None or not args.fixture.is_file():
            parser.error("fixture is required")
        row = _run_case(
            case,
            int(args.concurrency),
            manifest["representative_provider"],
            args.fixture,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    receipt = _receipt(manifest, args.manifest, args.profile)
    validate_receipt(receipt)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": os.fspath(args.output),
                "profile": args.profile,
                "runs": len(receipt["runs"]),
                "hosted_calls": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
