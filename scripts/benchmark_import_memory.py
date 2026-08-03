#!/usr/bin/env python3
"""Generate history larger than RAM budgets and verify bounded importer RSS."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(REPOSITORY_ROOT / "src"))

from substrate_wiki.history import (  # noqa: E402
    HermesHistoryImporter,
    HermesJSONLHistorySource,
    HermesSQLiteHistorySource,
)


class SinkClient:
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
        del method, path, kwargs
        return {"duplicate": False}

    def import_status(self, batch_id: str) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "processed_windows": 1,
            "processed": 1,
            "pending_review": 0,
            "failed": 0,
            "complete": True,
        }


def _create_history(path: Path, *, size_mib: int, single_message_mib: int) -> None:
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
            INSERT INTO sessions VALUES ('memory-benchmark', 'cli', NULL, NULL, 1.0);
            """
        )
        if single_message_mib:
            content = "x" * (single_message_mib * 1024 * 1024)
            connection.execute(
                "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                ("memory-benchmark", "user", content, 1.0),
            )
        else:
            content = "x" * (1024 * 1024)
            connection.executemany(
                "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (
                    ("memory-benchmark", "user" if index % 2 == 0 else "assistant", content, index)
                    for index in range(size_mib)
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _create_jsonl_history(path: Path, *, size_mib: int, single_message_mib: int) -> None:
    message_mib = single_message_mib or 1
    message_count = 1 if single_message_mib else size_mib
    content = "x" * (message_mib * 1024 * 1024)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write('{"id":"memory-benchmark","source":"cli","messages":[')
        for index in range(message_count):
            if index:
                stream.write(",")
            role = "user" if index % 2 == 0 else "assistant"
            stream.write(f'{{"role":"{role}","content":"')
            stream.write(content)
            stream.write(f'","timestamp":{index}}}')
        stream.write("]}\n")


def _peak_rss_bytes() -> int:
    if sys.platform == "win32":
        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("page_fault_count", ctypes.c_ulong),
                ("peak_working_set_size", ctypes.c_size_t),
                ("working_set_size", ctypes.c_size_t),
                ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                ("quota_paged_pool_usage", ctypes.c_size_t),
                ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                ("quota_non_paged_pool_usage", ctypes.c_size_t),
                ("pagefile_usage", ctypes.c_size_t),
                ("peak_pagefile_usage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = ctypes.windll.kernel32.GetCurrentProcess  # type: ignore[attr-defined]
        get_current_process.argtypes = []
        get_current_process.restype = ctypes.c_void_p
        get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo  # type: ignore[attr-defined]
        get_process_memory_info.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ProcessMemoryCounters),
            ctypes.c_ulong,
        ]
        get_process_memory_info.restype = ctypes.c_int
        success = get_process_memory_info(
            get_current_process(),
            ctypes.byref(counters),
            counters.cb,
        )
        return int(counters.peak_working_set_size) if success else 0
    try:
        import resource

        getrusage = getattr(resource, "getrusage", None)
        rusage_self = getattr(resource, "RUSAGE_SELF", None)
        if not callable(getrusage) or rusage_self is None:
            return 0
        value = int(getrusage(rusage_self).ru_maxrss)
        return value if sys.platform == "darwin" else value * 1024
    except ImportError:
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=2048)
    parser.add_argument("--single-message-mib", type=int, default=0)
    parser.add_argument("--limit-mib", type=int, default=256)
    parser.add_argument("--source", choices=("sqlite", "jsonl"), default="sqlite")
    parser.add_argument("--generate-only", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.generate_only is not None:
        creator = _create_history if args.source == "sqlite" else _create_jsonl_history
        creator(
            args.generate_only,
            size_mib=max(1, args.size_mib),
            single_message_mib=max(0, args.single_message_mib),
        )
        return 0
    with tempfile.TemporaryDirectory(prefix="substrate-memory-benchmark-") as directory:
        home = Path(directory) / "hermes"
        home.mkdir()
        history_path = home / ("state.db" if args.source == "sqlite" else "history.jsonl")
        # Fixture creation can transiently copy a large SQLite parameter. Run
        # it in a short-lived child so this process's ru_maxrss measures the
        # importer itself rather than test-data generation.
        subprocess.run(
            [
                sys.executable,
                os.fspath(Path(__file__).resolve()),
                "--generate-only",
                os.fspath(history_path),
                "--size-mib",
                str(max(1, args.size_mib)),
                "--single-message-mib",
                str(max(0, args.single_message_mib)),
                "--source",
                args.source,
            ],
            check=True,
        )
        source = (
            HermesSQLiteHistorySource(history_path)
            if args.source == "sqlite"
            else HermesJSONLHistorySource(history_path)
        )
        importer = HermesHistoryImporter(
            hermes_home=home,
            client=SinkClient(),  # type: ignore[arg-type]
            source=source,
        )
        try:
            status = importer.run(wait=True)
            peak = _peak_rss_bytes()
        finally:
            if importer.checkpoint is not None:
                importer.checkpoint.close()
    result = {
        "complete": bool(status.get("complete")),
        "peak_rss_bytes": peak,
        "limit_bytes": args.limit_mib * 1024 * 1024,
        "history_mib": args.single_message_mib or args.size_mib,
        "single_message": bool(args.single_message_mib),
        "source": args.source,
    }
    print(json.dumps(result, sort_keys=True))
    if not result["complete"]:
        return 1
    if peak and peak > result["limit_bytes"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
