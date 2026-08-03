"""Dedicated history-import worker entry point used by the systemd user unit."""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import FrameType
from typing import Any

from .checkpoint import TERMINAL_STATES, ImportCheckpoint
from .client import SubstrateClient
from .history import (
    ConversationReplaySource,
    HermesHistoryImporter,
    HermesJSONLHistorySource,
    HermesSQLiteHistorySource,
    remove_official_export_spill,
)


def checkpoint_path(hermes_home: Path, job_id: str) -> Path:
    if not job_id or not job_id.isalnum() or len(job_id) > 128:
        raise ValueError("invalid job id")
    return hermes_home / "substrate_wiki" / "imports" / "jobs" / job_id / "checkpoint.db"


def _install_stop_handlers(
    stop_event: threading.Event,
) -> dict[signal.Signals, Any]:
    """Turn systemd stop into a durable acknowledgement-boundary pause."""
    if os.name != "posix" or threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[signal.Signals, Any] = {}

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop_event.set()

    for selected in (signal.SIGTERM, signal.SIGINT):
        previous[selected] = signal.getsignal(selected)
        signal.signal(selected, request_stop)
    return previous


def _restore_stop_handlers(previous: dict[signal.Signals, Any]) -> None:
    for selected, handler in previous.items():
        signal.signal(selected, handler)


def run_job(hermes_home: Path, job_id: str) -> dict[str, object]:
    checkpoint = ImportCheckpoint(checkpoint_path(hermes_home, job_id))
    stop_event = threading.Event()
    previous_handlers = _install_stop_handlers(stop_event)
    source_kind = ""
    locator = Path()
    try:
        job = checkpoint.job()
        source_kind = str(job["source_kind"])
        locator = Path(str(job["source_locator"]))
        source: ConversationReplaySource
        if source_kind == "hermes-sqlite":
            source = HermesSQLiteHistorySource(locator)
        elif source_kind in {"hermes-jsonl", "hermes-official-export"}:
            source = HermesJSONLHistorySource(locator)
        else:
            raise ValueError("unsupported durable source kind")
        client = SubstrateClient.from_env(timeout=30.0)
        importer = HermesHistoryImporter(
            hermes_home=hermes_home,
            client=client,
            source=source,
            agent_id=str(job["agent_id"]),
            stop_requested=stop_event.is_set,
        )
        importer.checkpoint = checkpoint
        capabilities = client.capabilities()
        replay = capabilities.get("history_replay")
        if not (
            isinstance(replay, dict)
            and capabilities.get("provider") == "substrate_wiki"
            and 2 in capabilities.get("capture_schema_versions", [])
            and replay.get("protocol") == "stream-v2"
            and replay.get("min_plugin_version") == "1.2.0"
            and replay.get("content_free_completion") is True
            and replay.get("incremental_windows") is True
            and int(replay.get("status_version", 0)) == 2
            and int(capabilities.get("max_event_bytes", 0)) == 262_144
        ):
            checkpoint.set_state("failed", error_class="server_upgrade_required")
            raise RuntimeError("server_upgrade_required")
        status = importer.run(wait=True)
        if status.get("complete") and os.name == "posix":
            from .supervisor import unit_instance_name

            subprocess.run(
                ("systemctl", "--user", "disable", unit_instance_name(hermes_home, job_id)),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        return status
    finally:
        _restore_stop_handlers(previous_handlers)
        # The official export is the durable replay source, not disposable
        # scratch space while a job can still be resumed.  In particular, a
        # systemd SIGTERM is converted into a pause at an acknowledgement
        # boundary; deleting the spill here would leave the unchanged
        # checkpoint pointing at a missing source on restart.  Remove it only
        # after a genuinely terminal completion/cancellation.  Failed jobs are
        # intentionally resumable and therefore retain the source as well.
        terminal = False
        try:
            terminal = str(checkpoint.job().get("state") or "") in TERMINAL_STATES
        except (RuntimeError, sqlite3.Error):
            terminal = False
        if source_kind == "hermes-official-export" and terminal:
            remove_official_export_spill(
                hermes_home,
                source_kind=source_kind,
                source_locator=os.fspath(locator),
            )
        checkpoint.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    try:
        run_job(args.hermes_home.resolve(), args.job_id)
    except Exception:  # noqa: BLE001 - systemd receives only an exit code
        return 1
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
