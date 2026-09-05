"""Build deterministic per-plugin Substrate release archives.

Packages each plugin directory as ``dist/<name>.zip`` with fixed
timestamps and sorted members so two builds of the same tree are
byte-identical, plus a single ``dist/SHA256SUMS`` covering all six
archives. ``--check`` rebuilds every archive in memory and compares
against the on-disk files.

Release layout (repo-level release version in the root ``VERSION`` file
covers the whole plugin set):

- ``substrate.zip`` from ``plugins/substrate`` (Hermes reference plugin,
  content frozen at 0.3.0; bytes must stay identical across releases that
  do not touch it) with ``substrate/`` prefix.
- ``claude-code.zip`` from ``plugins/claude-code`` with ``claude-code/`` prefix.
- ``claude-cowork.zip`` from ``plugins/claude-cowork`` with ``claude-cowork/`` prefix.
- ``codex.zip`` from ``plugins/codex`` with ``codex/`` prefix.
- ``grok-bot.zip`` from ``plugins/grok-bot`` with ``grok-bot/`` prefix.
- ``openclaw.zip`` from ``plugins/openclaw`` with ``openclaw/`` prefix.

Each archive also carries the root ``LICENSE`` as ``<prefix>/LICENSE``.
"""

from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPOSITORY_ROOT / "plugins" / "substrate"
DIST_DIR = REPOSITORY_ROOT / "dist"
ARCHIVE_NAME = "substrate.zip"
PREFIX = "substrate/"
FIXED_TIMESTAMP = (2020, 1, 1, 0, 0, 0)

# Archive name -> plugin source directory name. Order defines build order;
# SHA256SUMS lines are sorted by archive name.
PLUGINS: dict[str, str] = {
    "substrate": "substrate",
    "claude-code": "claude-code",
    "claude-cowork": "claude-cowork",
    "codex": "codex",
    "grok-bot": "grok-bot",
    "openclaw": "openclaw",
}

ARCHIVE_NAMES: list[str] = [f"{name}.zip" for name in PLUGINS]


# Directory and file names that must never ship in a release archive
# (caches, test trees, private files, env files, VCS metadata). This is a
# deny-list on top of the per-plugin source directory: the release under
# test must still enumerate its exact allowed members.
EXCLUDED_DIR_NAMES = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    ".git",
    ".hg",
    "tests",
    "test",
    "htmlcov",
    "node_modules",
    "dist",
    "build",
})
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
EXCLUDED_FILE_NAMES = frozenset({".env"})


def _excluded(path: Path, source_dir: Path) -> bool:
    if path.is_symlink():
        return True
    if path.name in EXCLUDED_FILE_NAMES:
        return True
    if path.name.endswith(EXCLUDED_SUFFIXES):
        return True
    try:
        relative_parts = path.relative_to(source_dir).parts
    except ValueError:
        return True
    return any(part in EXCLUDED_DIR_NAMES for part in relative_parts)


def collect_members_for(source_dir: Path, prefix: str) -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    license_path = REPOSITORY_ROOT / "LICENSE"
    if license_path.is_file():
        members.append((prefix + "LICENSE", license_path.read_bytes()))
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source_dir).as_posix()
        if _excluded(path, source_dir):
            continue
        members.append((prefix + relative, path.read_bytes()))
    members.sort(key=lambda item: item[0])
    return members


def collect_members() -> list[tuple[str, bytes]]:
    """Members of the frozen Hermes ``substrate.zip`` (kept for compatibility)."""
    return collect_members_for(SOURCE_DIR, PREFIX)


def build_archive_bytes_for(name: str) -> bytes:
    import io

    source_dir = REPOSITORY_ROOT / "plugins" / PLUGINS[name]
    prefix = f"{name}/"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for arcname, payload in collect_members_for(source_dir, prefix):
            info = zipfile.ZipInfo(arcname, date_time=FIXED_TIMESTAMP)
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


def build_archive_bytes() -> bytes:
    """Bytes of the frozen Hermes ``substrate.zip`` (kept for compatibility)."""
    return build_archive_bytes_for("substrate")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_all() -> dict[str, str]:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    for name in PLUGINS:
        archive_name = f"{name}.zip"
        data = build_archive_bytes_for(name)
        (DIST_DIR / archive_name).write_bytes(data)
        digests[archive_name] = digest(data)
    sums = "".join(f"{digests[n]}  {n}\n" for n in sorted(digests))
    (DIST_DIR / "SHA256SUMS").write_text(sums, encoding="utf-8")
    return digests


def build() -> tuple[Path, str]:
    digests = build_all()
    return DIST_DIR / ARCHIVE_NAME, digests[ARCHIVE_NAME]


def check() -> bool:
    ok = True
    for name in PLUGINS:
        archive_name = f"{name}.zip"
        archive_path = DIST_DIR / archive_name
        if not archive_path.is_file():
            print(f"dist archive missing: {archive_name}; run without --check first")
            ok = False
    sums_path = DIST_DIR / "SHA256SUMS"
    if not sums_path.is_file():
        print("dist SHA256SUMS missing; run without --check first")
        return False
    expected: dict[str, str] = {f"{n}.zip": digest(build_archive_bytes_for(n)) for n in PLUGINS}
    for archive_name, sha in expected.items():
        path = DIST_DIR / archive_name
        if path.is_file() and path.read_bytes() != build_archive_bytes_for(archive_name[:-4]):
            print(f"dist archive is not byte-identical to a fresh build: {archive_name}")
            ok = False
    recorded = sums_path.read_text(encoding="utf-8")
    want = "".join(f"{expected[n]}  {n}\n" for n in sorted(expected))
    if recorded != want:
        print("SHA256SUMS does not match a fresh build")
        print(f"--- recorded ---\n{recorded}--- want ---\n{want}")
        return False
    # Refuse stray files: dist must hold exactly the six archives + SHA256SUMS.
    present = sorted(p.name for p in DIST_DIR.iterdir() if p.is_file())
    if present != sorted([*ARCHIVE_NAMES, "SHA256SUMS"]):
        print(f"unexpected dist contents: {present}")
        return False
    if ok:
        for archive_name in sorted(expected):
            print(f"OK: {archive_name} {expected[archive_name]}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify dist against a fresh build")
    args = parser.parse_args(argv)
    if args.check:
        return 0 if check() else 1
    digests = build_all()
    for archive_name in sorted(digests):
        print(f"{DIST_DIR / archive_name} {digests[archive_name]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
