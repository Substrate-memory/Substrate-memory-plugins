#!/usr/bin/env python3
"""Build and verify the deterministic Hermes plugin archive."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "substrate_wiki"
PLUGIN_SOURCE = REPOSITORY_ROOT / "src" / PLUGIN_NAME
ARCHIVE_PATH = REPOSITORY_ROOT / "dist" / f"{PLUGIN_NAME}.zip"
INSTALLER_PATH = REPOSITORY_ROOT / "scripts" / "install_hermes_plugin.py"
LICENSE_PATH = REPOSITORY_ROOT / "LICENSE"
RELEASES_PATH = REPOSITORY_ROOT / "release-assets"
PROVENANCE_FILENAME = "PROVENANCE.json"
LICENSE_FILENAME = "LICENSE"
TARGET_HERMES_VERSION = "0.18.2"
BUILD_FORMAT_VERSION = 3
SOURCE_COMMIT_ENVIRONMENT_VARIABLE = "HERMES_PLUGIN_SOURCE_COMMIT"
PLUGIN_REDACTION_FILENAME = "redaction.py"
GENERATED_DETECTOR_BLOCK_START = "# BEGIN GENERATED SERVER PROVIDER DETECTORS\n"
GENERATED_DETECTOR_BLOCK_END = "# END GENERATED SERVER PROVIDER DETECTORS\n"
REQUIRED_FILES = (
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
)
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644
DIRECTORY_MODE = 0o40755


def _is_generated(relative_path: Path) -> bool:
    return "__pycache__" in relative_path.parts or relative_path.suffix in {".pyc", ".pyo"}


def _require_release_clean_redaction(source: Path = PLUGIN_SOURCE) -> None:
    """Reject missing or multiply generated detector blocks without server coupling."""

    plugin = (source / PLUGIN_REDACTION_FILENAME).read_text(encoding="utf-8")
    if plugin.count(GENERATED_DETECTOR_BLOCK_START) != 1 or plugin.count(
        GENERATED_DETECTOR_BLOCK_END
    ) != 1:
        raise ValueError("plugin redaction detector block is missing or duplicated")
    if "_PROVIDER_TOKEN = re.compile(" not in plugin or (
        "_PERCENT_ENCODED_PROVIDER_TOKEN = re.compile(" not in plugin
    ):
        raise ValueError("plugin redaction provider detectors are missing")


def _source_files(source: Path = PLUGIN_SOURCE) -> tuple[Path, ...]:
    files = tuple(
        sorted(
            (
                path.relative_to(source)
                for path in source.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and not _is_generated(path.relative_to(source))
            ),
            key=lambda path: path.as_posix(),
        )
    )
    expected = tuple(
        sorted((Path(name) for name in REQUIRED_FILES), key=lambda path: path.as_posix())
    )
    if files != expected:
        missing = sorted(set(expected) - set(files), key=lambda path: path.as_posix())
        unexpected = sorted(set(files) - set(expected), key=lambda path: path.as_posix())
        details = []
        if missing:
            details.append("missing: " + ", ".join(path.as_posix() for path in missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(path.as_posix() for path in unexpected))
        raise ValueError("plugin source is not release-clean (" + "; ".join(details) + ")")
    return files


def _manifest_value(manifest: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*([^#\s]+)\s*$", manifest)
    if match is None:
        raise ValueError(f"plugin.yaml must define {key!r}")
    return match.group(1)


def _plugin_version(source: Path = PLUGIN_SOURCE) -> str:
    manifest = (source / "plugin.yaml").read_text(encoding="utf-8")
    identity = _manifest_value(manifest, "name")
    if identity != PLUGIN_NAME:
        raise ValueError(
            f"plugin.yaml name must be {PLUGIN_NAME!r} to match the package directory; got {identity!r}"
        )
    return _manifest_value(manifest, "version")


def _validate_source_commit(source_commit: object) -> str:
    if source_commit == "unknown":
        return "unknown"
    if isinstance(source_commit, str) and re.fullmatch(r"[0-9a-f]{40}", source_commit):
        return source_commit
    raise ValueError("source_commit must be 'unknown' or a full 40-character lowercase Git SHA")


def _source_commit() -> str:
    """Return an explicitly supplied immutable commit, or a stable unknown marker."""
    candidate = os.environ.get(SOURCE_COMMIT_ENVIRONMENT_VARIABLE, "").strip().lower()
    if not candidate:
        return "unknown"
    try:
        return _validate_source_commit(candidate)
    except ValueError as exc:
        raise ValueError(
            f"{SOURCE_COMMIT_ENVIRONMENT_VARIABLE} must be a full 40-character Git SHA"
        ) from exc


def _require_commit_contains_source(
    source_commit: str,
    source: Path = PLUGIN_SOURCE,
) -> None:
    """Prove that every packaged source byte exists at the claimed Git commit."""
    source = source.resolve()
    try:
        relative_root = source.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("plugin source is outside the repository") from exc
    files = _source_files(source)
    expected_paths = tuple(
        f"{relative_root.as_posix()}/{relative.as_posix()}" for relative in files
    )
    tree = subprocess.run(
        (
            "git",
            "-C",
            os.fspath(REPOSITORY_ROOT),
            "ls-tree",
            "-r",
            "--name-only",
            source_commit,
            "--",
            relative_root.as_posix(),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    committed_paths = tuple(line for line in tree.stdout.splitlines() if line)
    if tree.returncode != 0 or committed_paths != expected_paths:
        raise ValueError("source_commit does not contain the release-clean plugin file set")
    for relative, committed_path in zip(files, committed_paths, strict=True):
        blob = subprocess.run(
            (
                "git",
                "-C",
                os.fspath(REPOSITORY_ROOT),
                "show",
                f"{source_commit}:{committed_path}",
            ),
            check=False,
            capture_output=True,
        )
        if blob.returncode != 0 or blob.stdout != (source / relative).read_bytes():
            raise ValueError(f"plugin source differs from source_commit: {relative.as_posix()}")
    license_blob = subprocess.run(
        ("git", "-C", os.fspath(REPOSITORY_ROOT), "show", f"{source_commit}:LICENSE"),
        check=False,
        capture_output=True,
    )
    if (
        license_blob.returncode != 0
        or not LICENSE_PATH.is_file()
        or LICENSE_PATH.is_symlink()
        or license_blob.stdout != LICENSE_PATH.read_bytes()
    ):
        raise ValueError("LICENSE differs from source_commit")


def _provenance_bytes(
    source: Path, files: tuple[Path, ...], *, source_commit: str | None = None
) -> bytes:
    provenance = {
        "build_format_version": BUILD_FORMAT_VERSION,
        "license_sha256": hashlib.sha256(LICENSE_PATH.read_bytes()).hexdigest(),
        "plugin_version": _plugin_version(source),
        "provider_id": PLUGIN_NAME,
        "source_commit": _source_commit()
        if source_commit is None
        else _validate_source_commit(source_commit),
        "source_files": {
            path.as_posix(): hashlib.sha256((source / path).read_bytes()).hexdigest()
            for path in files
        },
        "target_hermes_version": TARGET_HERMES_VERSION,
    }
    return (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _zip_info(name: str, *, directory: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (DIRECTORY_MODE if directory else FILE_MODE) << 16
    if directory:
        info.external_attr |= 0x10
    return info


def build_archive_bytes(source: Path = PLUGIN_SOURCE, *, source_commit: str | None = None) -> bytes:
    """Return canonical archive bytes for the release-clean plugin source."""
    source = source.resolve()
    _require_release_clean_redaction(source)
    files = _source_files(source)
    provenance = _provenance_bytes(source, files, source_commit=source_commit)

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(_zip_info(f"{PLUGIN_NAME}/", directory=True), b"", compresslevel=9)
        archive.writestr(
            _zip_info(f"{PLUGIN_NAME}/{PROVENANCE_FILENAME}"),
            provenance,
            compresslevel=9,
        )
        archive.writestr(
            _zip_info(f"{PLUGIN_NAME}/{LICENSE_FILENAME}"),
            LICENSE_PATH.read_bytes(),
            compresslevel=9,
        )
        for relative_path in files:
            archive.writestr(
                _zip_info(f"{PLUGIN_NAME}/{relative_path.as_posix()}"),
                (source / relative_path).read_bytes(),
                compresslevel=9,
            )
    return output.getvalue()


def _archive_source_commit(archive_path: Path) -> str:
    """Extract and validate the provenance commit embedded in an archive."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            provenance = json.loads(
                archive.read(f"{PLUGIN_NAME}/{PROVENANCE_FILENAME}").decode("utf-8")
            )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, zipfile.BadZipFile) as exc:
        raise ValueError(f"archive has malformed provenance: {archive_path}") from exc
    if not isinstance(provenance, dict):
        raise ValueError(f"archive has malformed provenance: {archive_path}")
    try:
        return _validate_source_commit(provenance["source_commit"])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"archive has malformed provenance: {archive_path}") from exc


def check_archive(archive_path: Path = ARCHIVE_PATH, source: Path = PLUGIN_SOURCE) -> bool:
    """Return whether an archive exactly matches a canonical rebuild."""
    if not archive_path.is_file() or archive_path.is_symlink():
        return False
    source_commit = _archive_source_commit(archive_path)
    return archive_path.read_bytes() == build_archive_bytes(source, source_commit=source_commit)


def _installer_bytes(version: str, installer_path: Path = INSTALLER_PATH) -> bytes:
    """Return the installer only when it is pinned to the packaged plugin version."""
    content = installer_path.read_bytes()
    marker = f'EXPECTED_VERSION = "{version}"'.encode()
    if marker not in content:
        raise ValueError(f"installer does not target plugin version {version}")
    return content


def _replace_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError(f"artifact parent must not be a symlink: {path.parent}")
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_bytes(content)
        temporary_path.replace(path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _validate_immutable_slot(path: Path, content: bytes) -> None:
    """Reject unsafe or conflicting versioned artifact paths before publication."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError(f"immutable release artifact path is unsafe: {path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise ValueError(f"immutable release artifact differs: {path}")


def _write_immutable(path: Path, content: bytes) -> None:
    """Create a versioned release file once, accepting only byte-identical reruns."""
    _validate_immutable_slot(path, content)
    if path.exists():
        return
    try:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise ValueError(f"immutable release artifact differs: {path}") from None


def publish_release(
    archive: bytes,
    *,
    source: Path = PLUGIN_SOURCE,
    archive_path: Path = ARCHIVE_PATH,
    installer_path: Path = INSTALLER_PATH,
    releases_path: Path = RELEASES_PATH,
) -> tuple[Path, Path]:
    """Publish immutable versioned artifacts and refresh the current aliases."""
    version = _plugin_version(source)
    installer = _installer_bytes(version, installer_path)
    release_directory = releases_path / version
    release_archive = release_directory / archive_path.name
    release_installer = release_directory / installer_path.name

    # Versioned paths are write-once. Unversioned paths are byte-for-byte aliases
    # to the current release and may move only as part of a new version release.
    # Preflight both files so a pre-existing conflict cannot leave a half-published
    # version directory behind.
    _validate_immutable_slot(release_archive, archive)
    _validate_immutable_slot(release_installer, installer)
    _write_immutable(release_archive, archive)
    _write_immutable(release_installer, installer)
    _replace_file(archive_path, archive)
    return release_archive, release_installer


def check_release(
    *,
    source: Path = PLUGIN_SOURCE,
    archive_path: Path = ARCHIVE_PATH,
    installer_path: Path = INSTALLER_PATH,
    releases_path: Path = RELEASES_PATH,
) -> bool:
    """Verify canonical current bytes and their immutable versioned copies."""
    if not check_archive(archive_path, source):
        return False
    source_commit = _archive_source_commit(archive_path)
    if source_commit == "unknown":
        return False
    version = _plugin_version(source)
    release_directory = releases_path / version
    release_archive = release_directory / archive_path.name
    release_installer = release_directory / installer_path.name
    if (
        release_directory.is_symlink()
        or not release_archive.is_file()
        or release_archive.is_symlink()
        or release_archive.read_bytes() != archive_path.read_bytes()
    ):
        return False
    if not release_installer.is_file() or release_installer.is_symlink():
        return False
    installer = _installer_bytes(version, installer_path)
    return release_installer.read_bytes() == installer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "verify the checked-in current and immutable versioned release artifacts "
            "without rewriting them"
        ),
    )
    args = parser.parse_args(argv)

    if args.check and not ARCHIVE_PATH.is_file():
        print(f"error: archive is missing: {ARCHIVE_PATH}", file=sys.stderr)
        return 1

    try:
        source_commit = _archive_source_commit(ARCHIVE_PATH) if args.check else _source_commit()
        if source_commit == "unknown":
            raise ValueError(
                f"{SOURCE_COMMIT_ENVIRONMENT_VARIABLE} must identify the committed plugin source"
            )
        _require_commit_contains_source(source_commit)
        expected = build_archive_bytes(source_commit=source_commit)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        if not check_release(
            source=PLUGIN_SOURCE,
            archive_path=ARCHIVE_PATH,
            installer_path=INSTALLER_PATH,
            releases_path=RELEASES_PATH,
        ):
            print(
                "error: Hermes plugin release is stale, non-canonical, or missing its "
                "immutable versioned copy; "
                "run `python scripts/build_plugin.py`",
                file=sys.stderr,
            )
            return 1
        print(f"Hermes plugin release is current: {ARCHIVE_PATH}")
        return 0

    release_archive, release_installer = publish_release(
        expected,
        source=PLUGIN_SOURCE,
        archive_path=ARCHIVE_PATH,
        installer_path=INSTALLER_PATH,
        releases_path=RELEASES_PATH,
    )
    print(
        f"Built {ARCHIVE_PATH} ({len(expected)} bytes) and published immutable artifacts "
        f"at {release_archive.parent} ({release_installer.name})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
