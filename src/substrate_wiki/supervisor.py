"""Profile-scoped systemd service naming and control without secret arguments."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import time
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


def _runtime_path(hermes_home: Path, job_id: str) -> Path:
    return hermes_home / "substrate_wiki" / "imports" / "jobs" / job_id / "worker.json"


def _systemd_unit_installed(hermes_home: Path) -> bool:
    if os.name != "posix":
        return False
    unit = Path.home() / ".config" / "systemd" / "user" / unit_template_name(hermes_home)
    return unit.is_file() and not unit.is_symlink()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _portable_process_matches(pid: int, nonce: str) -> bool:
    if not _pid_alive(pid) or not nonce:
        return False
    if sys.platform.startswith("linux"):
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            return False
        return nonce.encode("ascii") in command
    # Other platforms cannot safely prove PID ownership with the stdlib.
    return False


def _portable_status(hermes_home: Path, job_id: str) -> dict[str, object]:
    path = _runtime_path(hermes_home, job_id)
    if path.is_symlink():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def start_service(hermes_home: Path, job_id: str) -> str:
    """Start once through systemd when installed, otherwise a detached worker.

    The portable path uses no shell and places no credential in argv or its
    content-free runtime record.  The durable checkpoint remains authoritative.
    """
    if _systemd_unit_installed(hermes_home):
        unit = unit_instance_name(hermes_home, job_id)
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", unit)
        return unit
    previous = _portable_status(hermes_home, job_id)
    try:
        previous_pid = int(previous.get("pid", 0))
    except (TypeError, ValueError):
        previous_pid = 0
    previous_nonce = str(previous.get("nonce", ""))
    if _portable_process_matches(previous_pid, previous_nonce):
        return f"process:{previous_pid}"
    supervisor = Path(__file__).resolve()
    nonce = secrets.token_hex(16)
    command = (sys.executable, os.fspath(supervisor), "--hermes-home",
               os.fspath(hermes_home.resolve()), "--job-id", job_id,
               "--runtime-nonce", nonce)
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL, "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    path = _runtime_path(hermes_home, job_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    from .spool import secure_atomic_json_write
    secure_atomic_json_write(path, {"pid": process.pid, "nonce": nonce,
                                    "started_at": time.time(),
                                    "restart_count": int(previous.get("restart_count", 0))})
    return f"process:{process.pid}"


def stop_service(hermes_home: Path, job_id: str) -> None:
    if _systemd_unit_installed(hermes_home):
        _systemctl("disable", "--now", unit_instance_name(hermes_home, job_id), check=False)
        return
    value = _portable_status(hermes_home, job_id)
    try:
        pid = int(value.get("pid", 0))
    except (TypeError, ValueError):
        return
    if _portable_process_matches(pid, str(value.get("nonce", ""))):
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def service_restart_count(hermes_home: Path, job_id: str) -> int:
    if _systemd_unit_installed(hermes_home):
        result = _systemctl(
            "show", unit_instance_name(hermes_home, job_id), "--property=NRestarts", "--value",
            check=False,
        )
        try:
            return max(0, int(result.stdout.strip()))
        except ValueError:
            return 0
    try:
        return max(0, int(_portable_status(hermes_home, job_id).get("restart_count", 0)))
    except (TypeError, ValueError):
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
    parser.add_argument("--runtime-nonce", required=False, default="")
    args = parser.parse_args(argv)
    bootstrap_package()
    from substrate_wiki.worker import run_job

    run_job(Path(args.hermes_home).resolve(), args.job_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
