"""Profile-scoped systemd service naming and control without secret arguments."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path


def home_hash(hermes_home: Path) -> str:
    return hashlib.sha256(os.fspath(hermes_home.resolve()).encode()).hexdigest()[:12]


def unit_template_name(hermes_home: Path) -> str:
    return f"substrate-wiki-import-{home_hash(hermes_home)}@.service"


def unit_instance_name(hermes_home: Path, job_id: str) -> str:
    return f"substrate-wiki-import-{home_hash(hermes_home)}@{job_id}.service"


def _systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if os.name != "posix":
        raise RuntimeError("systemd user services require Linux")
    return subprocess.run(
        ("systemctl", "--user", *arguments),
        check=check,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )


def start_service(hermes_home: Path, job_id: str) -> str:
    unit = unit_instance_name(hermes_home, job_id)
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", unit)
    return unit


def stop_service(hermes_home: Path, job_id: str) -> None:
    _systemctl("disable", "--now", unit_instance_name(hermes_home, job_id), check=False)


def service_restart_count(hermes_home: Path, job_id: str) -> int:
    result = _systemctl(
        "show", unit_instance_name(hermes_home, job_id), "--property=NRestarts", "--value",
        check=False,
    )
    try:
        return max(0, int(result.stdout.strip()))
    except ValueError:
        return 0


def bootstrap_package() -> None:
    """Allow direct execution from the installed package without PYTHONPATH changes."""
    package_parent = Path(__file__).resolve().parent.parent
    if os.fspath(package_parent) not in sys.path:
        sys.path.insert(0, os.fspath(package_parent))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    bootstrap_package()
    from substrate_wiki.worker import run_job

    run_job(Path(args.hermes_home).resolve(), args.job_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
