#!/usr/bin/env python3
"""Verify and atomically install or upgrade the Substrate Hermes plugin."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

PLUGIN_NAME = "substrate_wiki"
EXPECTED_VERSION = "1.2.0"
EXPECTED_HERMES_VERSION = "0.18.2"
REQUIRED_FILES = {
    "README.md",
    "__init__.py",
    "cli.py",
    "client.py",
    "checkpoint.py",
    "events.py",
    "history.py",
    "plugin.yaml",
    "py.typed",
    "redaction.py",
    "spool.py",
    "supervisor.py",
    "worker.py",
}
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_archive(path: Path, expected_sha256: str = "") -> dict[str, Any]:
    data = path.read_bytes()
    digest = _digest(data)
    if expected_sha256 and digest != expected_sha256.lower():
        raise ValueError("archive SHA-256 mismatch")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("archive contains duplicate paths")
        if sum(info.file_size for info in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("archive is too large")
        for info in archive.infolist():
            name = info.filename
            candidate = PurePosixPath(name)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError("archive contains an unsafe path")
            if not candidate.parts or candidate.parts[0] != PLUGIN_NAME:
                raise ValueError("archive has an unexpected root directory")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("archive contains a symbolic link")
        provenance = json.loads(archive.read(f"{PLUGIN_NAME}/PROVENANCE.json").decode("utf-8"))
        if provenance.get("build_format_version") != 2:
            raise ValueError("unexpected plugin archive build format")
        if provenance.get("plugin_version") != EXPECTED_VERSION:
            raise ValueError("unexpected plugin version")
        if provenance.get("target_hermes_version") != EXPECTED_HERMES_VERSION:
            raise ValueError("unexpected Hermes target version")
        source_files = provenance.get("source_files")
        if not isinstance(source_files, dict):
            raise ValueError("archive provenance has no source file manifest")
        if set(source_files) != REQUIRED_FILES:
            raise ValueError("archive provenance has an unexpected source file set")
        source_commit = provenance.get("source_commit")
        if (
            not isinstance(source_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        ):
            raise ValueError("archive provenance has no immutable source commit")
        for relative, expected in source_files.items():
            actual = _digest(archive.read(f"{PLUGIN_NAME}/{relative}"))
            if actual != expected:
                raise ValueError(f"source digest mismatch: {relative}")
    return {
        "archive_sha256": digest,
        "plugin_version": EXPECTED_VERSION,
        "source_commit": source_commit,
    }


def _systemd_quote(value: Path) -> str:
    text = os.fspath(value.resolve())
    if any(character in text for character in ('\n', '\r', '"')):
        raise ValueError("service path cannot be represented safely")
    return f'"{text}"'


def _resolve_env_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        path = explicit.resolve()
    else:
        result = subprocess.run(
            ("hermes", "config", "env-path"),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        path = Path(result.stdout.strip()).resolve()
    if not path.is_file() or path.is_symlink():
        raise ValueError("Hermes environment path must be a regular non-symlink file")
    info = path.stat(follow_symlinks=False)
    if os.name == "posix":
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Hermes environment path must be owner-only")
    names: set[str] = set()
    with path.open("r", encoding="utf-8") as stream:
        for raw in stream:
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                names.add(line.split("=", 1)[0].removeprefix("export ").strip())
    if not {"HERMES_API_URL", "HERMES_API_KEY"} <= names:
        raise ValueError("Hermes environment is missing required Substrate variables")
    return path


def install_import_service(
    hermes_home: Path,
    plugin_target: Path,
    *,
    env_path: Path | None = None,
) -> dict[str, Any]:
    if os.name != "posix":
        raise ValueError("the import service can only be installed on Linux")
    from hashlib import sha256

    resolved_env = _resolve_env_path(env_path)
    home_hash = sha256(os.fspath(hermes_home.resolve()).encode()).hexdigest()[:12]
    unit_name = f"substrate-wiki-import-{home_hash}@.service"
    user_units = Path.home() / ".config" / "systemd" / "user"
    if user_units.exists() and user_units.is_symlink():
        raise ValueError("systemd user unit directory must not be a symlink")
    user_units.mkdir(parents=True, exist_ok=True, mode=0o700)
    unit_path = user_units / unit_name
    if unit_path.is_symlink():
        raise ValueError("existing import unit must not be a symlink")
    rollback: Path | None = None
    if unit_path.exists():
        rollback = unit_path.with_name(f"{unit_name}.rollback-{int(time.time())}")
        os.replace(unit_path, rollback)
    python = Path(sys.executable).resolve()
    supervisor = plugin_target / "supervisor.py"
    unit = "\n".join(
        (
            "[Unit]",
            "Description=Substrate Wiki durable history import %i",
            "After=network-online.target",
            "Wants=network-online.target",
            "StartLimitIntervalSec=600",
            "StartLimitBurst=8",
            "",
            "[Service]",
            "Type=simple",
            f"EnvironmentFile={_systemd_quote(resolved_env)}",
            f"ExecStart={_systemd_quote(python)} {_systemd_quote(supervisor)} "
            f"--hermes-home {_systemd_quote(hermes_home)} --job-id %i",
            "Restart=on-failure",
            "RestartSec=15s",
            "MemoryHigh=224M",
            "MemoryMax=256M",
            "OOMPolicy=stop",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            f"ReadWritePaths={_systemd_quote(hermes_home)}",
            "RestrictSUIDSGID=true",
            "LockPersonality=true",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        )
    ).encode("utf-8")
    temporary = unit_path.with_name(f".{unit_name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(unit)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, unit_path)
        os.chmod(unit_path, 0o600)
    except Exception:
        if rollback is not None and rollback.exists() and not unit_path.exists():
            os.replace(rollback, unit_path)
        raise
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    subprocess.run(
        ("systemctl", "--user", "daemon-reload"),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    return {
        "import_service": unit_name,
        "import_service_path": os.fspath(unit_path),
        "import_service_rollback": os.fspath(rollback) if rollback else None,
        "memory_high_bytes": 224 * 1024 * 1024,
        "memory_max_bytes": 256 * 1024 * 1024,
    }


def install(
    archive: Path,
    hermes_home: Path,
    *,
    expected_sha256: str = "",
    install_service: bool = False,
    env_path: Path | None = None,
) -> dict[str, Any]:
    verified = verify_archive(archive, expected_sha256)
    plugins = hermes_home / "plugins"
    plugins.mkdir(parents=True, exist_ok=True, mode=0o700)
    if plugins.is_symlink():
        raise ValueError("Hermes plugins directory must not be a symlink")
    target = plugins / PLUGIN_NAME
    if target.is_symlink():
        raise ValueError("existing plugin directory must not be a symlink")
    with tempfile.TemporaryDirectory(prefix="substrate-plugin-", dir=plugins) as directory:
        staging_root = Path(directory)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(staging_root)
        staged = staging_root / PLUGIN_NAME
        if not (staged / "plugin.yaml").is_file() or not (staged / "__init__.py").is_file():
            raise ValueError("archive is missing required plugin files")
        installed = not target.exists()
        rollback: Path | None = None
        if target.exists():
            rollback = plugins / f"{PLUGIN_NAME}.rollback-{int(time.time())}"
            os.replace(target, rollback)
        try:
            os.replace(staged, target)
        except Exception:
            if rollback is not None and rollback.exists() and not target.exists():
                os.replace(rollback, target)
            raise
        if os.name == "posix":
            os.chmod(target, 0o700)
            for child in target.rglob("*"):
                os.chmod(child, 0o600 if child.is_file() else 0o700)
    result = {
        **verified,
        "action": "installed" if installed else "upgraded",
        "target": os.fspath(target),
        "rollback": os.fspath(rollback) if rollback is not None else None,
    }
    if install_service:
        try:
            result.update(install_import_service(hermes_home, target, env_path=env_path))
        except Exception:
            if rollback is not None and rollback.exists() and target.exists():
                failed = plugins / f"{PLUGIN_NAME}.failed-{int(time.time())}"
                os.replace(target, failed)
                os.replace(rollback, target)
            raise
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes"),
    )
    parser.add_argument("--sha256", default="")
    parser.add_argument("--install-import-service", action="store_true")
    parser.add_argument("--env-path", type=Path)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.yes:
        parser.error("--yes is required for installation")
    try:
        result = install(
            args.archive.resolve(),
            args.hermes_home.resolve(),
            expected_sha256=args.sha256,
            install_service=args.install_import_service,
            env_path=args.env_path,
        )
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        message = {"error": type(exc).__name__, "installed": False}
        print(
            json.dumps(message, sort_keys=True)
            if args.json
            else f"Installation failed: {type(exc).__name__}"
        )
        return 1
    print(
        json.dumps(result, sort_keys=True)
        if args.json
        else f"Substrate plugin {result['action']}: {result['target']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
