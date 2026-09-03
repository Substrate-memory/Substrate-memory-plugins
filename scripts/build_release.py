"""Build the deterministic Substrate plugin release archive.

Packages ``plugins/substrate`` as ``dist/substrate.zip`` with fixed
timestamps and sorted members so two builds of the same tree are
byte-identical, plus ``dist/SHA256SUMS``. ``--check`` rebuilds in memory and
compares against the on-disk archive.
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


def collect_members() -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    license_path = REPOSITORY_ROOT / "LICENSE"
    if license_path.is_file():
        members.append((PREFIX + "LICENSE", license_path.read_bytes()))
    for path in sorted(SOURCE_DIR.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(SOURCE_DIR).as_posix()
        if "__pycache__" in path.parts or relative.endswith((".pyc", ".pyo")):
            continue
        members.append((PREFIX + relative, path.read_bytes()))
    members.sort(key=lambda item: item[0])
    return members


def build_archive_bytes() -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, payload in collect_members():
            info = zipfile.ZipInfo(name, date_time=FIXED_TIMESTAMP)
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build() -> tuple[Path, str]:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = DIST_DIR / ARCHIVE_NAME
    data = build_archive_bytes()
    archive_path.write_bytes(data)
    sums = f"{digest(data)}  {ARCHIVE_NAME}\n"
    (DIST_DIR / "SHA256SUMS").write_text(sums, encoding="utf-8")
    return archive_path, digest(data)


def check() -> bool:
    archive_path = DIST_DIR / ARCHIVE_NAME
    sums_path = DIST_DIR / "SHA256SUMS"
    if not archive_path.is_file() or not sums_path.is_file():
        print("dist archive or SHA256SUMS missing; run without --check first")
        return False
    expected = build_archive_bytes()
    if archive_path.read_bytes() != expected:
        print("dist archive is not byte-identical to a fresh build")
        return False
    recorded = sums_path.read_text(encoding="utf-8").strip()
    if recorded != f"{digest(expected)}  {ARCHIVE_NAME}":
        print("SHA256SUMS does not match a fresh build")
        return False
    print(f"OK: {ARCHIVE_NAME} {digest(expected)}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify dist against a fresh build")
    args = parser.parse_args(argv)
    if args.check:
        return 0 if check() else 1
    path, sha = build()
    print(f"{path} {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
