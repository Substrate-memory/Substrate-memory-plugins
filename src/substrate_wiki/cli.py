"""Managed OOM-safe history import commands for Substrate Wiki."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .checkpoint import ImportCheckpoint
from .client import SubstrateClient
from .onboarding import OnboardingError
from .history import (
    HermesHistoryImporter,
    load_hermes_inventory,
    remove_official_export_spill,
    select_history_source,
)
from .supervisor import service_restart_count, start_service, stop_service
from .worker import checkpoint_path


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except (ImportError, TypeError, ValueError):
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _fallback_url(home: Path) -> str:
    path = home / "substrate_wiki" / "config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return ""
    candidate = value.get("api_url") if isinstance(value, dict) else None
    return str(candidate).rstrip("/") if isinstance(candidate, str) else ""


def _active_agent_id() -> str:
    configured = os.environ.get("HERMES_PROFILE") or os.environ.get("HERMES_AGENT_ID")
    if configured:
        return configured
    try:
        from hermes_cli.profiles import get_active_profile_name

        return str(get_active_profile_name() or "default")
    except (ImportError, OSError, TypeError, ValueError):
        return "default"


def _job_paths(home: Path) -> list[Path]:
    jobs = home / "substrate_wiki" / "imports" / "jobs"
    if not jobs.is_dir() or jobs.is_symlink():
        return []
    return sorted(jobs.glob("*/checkpoint.db"), key=lambda item: item.stat().st_mtime, reverse=True)


def _open_job(home: Path, job_id: str | None) -> ImportCheckpoint:
    if job_id:
        return ImportCheckpoint(checkpoint_path(home, job_id))
    for path in _job_paths(home):
        checkpoint = ImportCheckpoint(path)
        if checkpoint.job()["state"] not in {"complete", "complete_with_failures", "cancelled"}:
            return checkpoint
        checkpoint.close()
    paths = _job_paths(home)
    if not paths:
        raise FileNotFoundError("no import job")
    return ImportCheckpoint(paths[0])


def _wait_for_job(home: Path, job_id: str) -> dict[str, Any]:
    while True:
        with ImportCheckpoint(checkpoint_path(home, job_id)) as checkpoint:
            status = checkpoint.status()
        if status["complete"] or status["state"] in {"cancelled", "failed"}:
            status["import_service_restart_count"] = service_restart_count(home, job_id)
            return status
        time.sleep(2.0)


def _import_history(args: argparse.Namespace, home: Path) -> dict[str, Any]:
    export_path = Path(args.input).resolve() if args.input else None
    if not args.yes:
        inventory = load_hermes_inventory(home, export_path=export_path)
        return {
            "discovered": inventory.discovered,
            "eligible": inventory.eligible,
            "skipped": inventory.skipped,
            "quarantined": inventory.quarantined,
            "dry_run": True,
        }
    client = SubstrateClient.from_env(fallback_url=_fallback_url(home), timeout=30.0, hermes_home=home, hosted_default=True)
    importer = HermesHistoryImporter(
        hermes_home=home,
        client=client,
        source=select_history_source(home, export_path=export_path),
        agent_id=_active_agent_id(),
    )
    checkpoint = importer.prepare()
    status = checkpoint.status()
    job_id = str(status["job_id"])
    checkpoint.close()
    status["import_service"] = start_service(home, job_id)
    return _wait_for_job(home, job_id) if args.wait else status


def _status(args: argparse.Namespace, home: Path) -> dict[str, Any]:
    with _open_job(home, args.job_id) as checkpoint:
        status = checkpoint.status()
    status["import_service_restart_count"] = service_restart_count(
        home, str(status["job_id"])
    )
    return status


def _resume(args: argparse.Namespace, home: Path) -> dict[str, Any]:
    if not args.yes:
        raise ValueError("--yes is required")
    with _open_job(home, args.job_id) as checkpoint:
        status = checkpoint.status()
        if status["state"] == "cancelled":
            checkpoint.set_state("ready")
        job_id = str(status["job_id"])
    start_service(home, job_id)
    return _wait_for_job(home, job_id) if args.wait else _status(args, home)


def _cancel(args: argparse.Namespace, home: Path) -> dict[str, Any]:
    if not args.yes:
        raise ValueError("--yes is required")
    with _open_job(home, args.job_id) as checkpoint:
        job = checkpoint.job()
        checkpoint.set_state("cancelled")
        result = checkpoint.status()
    stop_service(home, str(result["job_id"]))
    remove_official_export_spill(
        home,
        source_kind=str(job["source_kind"]),
        source_locator=str(job["source_locator"]),
    )
    return result


def _onboarding(args: argparse.Namespace, home: Path) -> dict[str, Any]:
    from .onboarding import OnboardingManager

    manager = OnboardingManager(home)
    result = manager.begin(mode=args.mode, open_browser=not args.no_browser)
    if result.get("phase") == "authorization_pending":
        print(
            f"Open {result.get('verification_uri_complete')} to sign in by email "
            f"and connect Hermes. One-time code: {result.get('user_code')}",
            file=sys.stderr,
            flush=True,
        )
    deadline = time.monotonic() + max(0.0, args.timeout)
    while args.wait and result.get("phase") == "authorization_pending" and time.monotonic() < deadline:
        time.sleep(5)
        result = manager.advance()
    if result.get("phase") == "awaiting_history_consent":
        decision = args.history
        if decision == "ask" and sys.stdin.isatty():
            print(
                "Upload all eligible past Hermes conversations to Substrate? [y/n]: ",
                end="",
                file=sys.stderr,
                flush=True,
            )
            answer = sys.stdin.readline().strip().casefold()
            if answer in {"y", "yes"}:
                decision = "approve"
            elif answer in {"n", "no"}:
                decision = "decline"
        if decision in {"approve", "decline"}:
            result = manager.consent_history(decision == "approve")
    return result


def _onboarding_status(_args: argparse.Namespace, home: Path) -> dict[str, Any]:
    from .onboarding import OnboardingManager

    return OnboardingManager(home).status()


def _onboarding_repair(args: argparse.Namespace, home: Path) -> dict[str, Any]:
    from .onboarding import OnboardingManager

    if not args.yes:
        raise ValueError("--yes is required")
    return OnboardingManager(home).repair(wait=args.wait)


def substrate_command(args: argparse.Namespace) -> None:
    home = _hermes_home().resolve()
    command = getattr(args, "substrate_command", None)
    try:
        if command == "onboard":
            result = _onboarding(args, home)
        elif command == "onboarding-status":
            result = _onboarding_status(args, home)
        elif command == "onboarding-repair":
            result = _onboarding_repair(args, home)
        elif command == "import-history":
            result = _import_history(args, home)
        elif command == "import-status":
            result = _status(args, home)
        elif command == "import-resume":
            result = _resume(args, home)
        elif command == "import-cancel":
            result = _cancel(args, home)
        else:
            raise ValueError("unknown command")
    except Exception as exc:  # noqa: BLE001 - content-free CLI failure contract
        result = {"error_class": type(exc).__name__, "complete": False}
        if isinstance(exc, OnboardingError):
            result["error_category"] = exc.category
        print(json.dumps(result, sort_keys=True) if args.json else f"Substrate command failed: {type(exc).__name__}")
        raise SystemExit(1) from None
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")


def register_cli(subparser: argparse.ArgumentParser) -> None:
    commands = subparser.add_subparsers(dest="substrate_command")

    onboard = commands.add_parser("onboard", help="Sign in to the fixed hosted service")
    onboard.add_argument("--mode", choices=("auto", "browser", "device"), default="auto")
    onboard.add_argument("--wait", action="store_true")
    onboard.add_argument("--timeout", type=float, default=900.0)
    onboard.add_argument("--no-browser", action="store_true")
    onboard.add_argument(
        "--history", choices=("ask", "approve", "decline"), default="ask",
        help="Historical-conversation consent; future capture remains enabled",
    )
    onboard.add_argument("--json", action="store_true")

    onboarding_status = commands.add_parser(
        "onboarding-status", help="Show redacted connection and automatic-import progress"
    )
    onboarding_status.add_argument("--json", action="store_true")

    onboarding_repair = commands.add_parser(
        "onboarding-repair", help="Repair authentication or resume the durable import"
    )
    onboarding_repair.add_argument("--yes", action="store_true")
    onboarding_repair.add_argument("--wait", action="store_true")
    onboarding_repair.add_argument("--json", action="store_true")

    history = commands.add_parser("import-history", help="Start or attach to durable history replay")
    history.add_argument("--yes", action="store_true")
    history.add_argument("--wait", action="store_true")
    history.add_argument("--json", action="store_true")
    history.add_argument("--input", metavar="PATH")

    status = commands.add_parser("import-status", help="Show content-free import status")
    status.add_argument("--job-id")
    status.add_argument("--json", action="store_true")

    resume = commands.add_parser("import-resume", help="Resume the same durable import job")
    resume.add_argument("--job-id")
    resume.add_argument("--yes", action="store_true")
    resume.add_argument("--wait", action="store_true")
    resume.add_argument("--json", action="store_true")

    cancel = commands.add_parser("import-cancel", help="Stop a worker without deleting state")
    cancel.add_argument("--job-id", required=True)
    cancel.add_argument("--yes", action="store_true")
    cancel.add_argument("--json", action="store_true")
    subparser.set_defaults(func=substrate_command)
