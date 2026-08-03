"""Bounded, private, durable JSON spool for nonblocking Hermes delivery."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any


def _chmod_private(path: Path, mode: int) -> None:
    if os.name == "posix":
        os.chmod(path, mode)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_root(root: Path) -> Path:
    absolute = root.absolute()
    if absolute.exists() and absolute.is_symlink():
        raise OSError("spool root must not be a symlink")
    absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
    if absolute.is_symlink() or not absolute.is_dir():
        raise OSError("invalid spool root")
    _chmod_private(absolute, 0o700)
    return absolute.resolve(strict=True)


def _safe_child(root: Path, path: Path) -> Path:
    candidate = path.absolute()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("path escapes spool root") from None
    if candidate.is_symlink():
        raise ValueError("spool files must not be symlinks")
    return candidate


def secure_atomic_json_write(target: Path, value: Any) -> None:
    """Write JSON with exclusive temp creation, fsync, replace, and directory fsync."""
    root = _safe_root(target.parent)
    target = _safe_child(root, target)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = root / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _chmod_private(temporary, 0o600)
        if target.exists() and target.is_symlink():
            raise OSError("target must not be a symlink")
        os.replace(temporary, target)
        _chmod_private(target, 0o600)
        _fsync_directory(root)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class DurableSpool:
    def __init__(self, root: Path, *, max_items: int = 1000, max_bytes: int = 10 * 1024 * 1024) -> None:
        self.root = _safe_root(root)
        self.max_items = max(1, max_items)
        self.max_bytes = max(1024, max_bytes)
        self._lock = threading.Lock()
        self._sequence = 0
        self._claimed: set[Path] = set()
        self.evicted_count = 0
        self.quarantined_count = 0
        self._quarantine = _safe_root(self.root / "corrupt")

    def append(self, event: dict[str, Any]) -> Path:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if len(payload) > self.max_bytes:
            raise ValueError("event exceeds spool limit")
        with self._lock:
            self._sequence += 1
            target = self.root / (
                f"{time.time_ns():020d}-{os.getpid()}-{threading.get_ident()}-{self._sequence:08d}.json"
            )
            self._write_payload_locked(target, payload)
            self._trim_locked(protected=target)
            files = self._files_locked()
            total = sum(path.stat(follow_symlinks=False).st_size for path in files)
            if len(files) > self.max_items or total > self.max_bytes:
                try:
                    target.unlink()
                    _fsync_directory(self.root)
                except FileNotFoundError:
                    pass
                raise ValueError("spool capacity unavailable")
            return target

    def oldest(self) -> Path | None:
        with self._lock:
            files = [path for path in self._files_locked() if path not in self._claimed]
            return files[0] if files else None

    def claim_oldest(self) -> Path | None:
        """Reserve the oldest event so capacity trimming cannot remove it in flight."""
        with self._lock:
            files = [path for path in self._files_locked() if path not in self._claimed]
            if not files:
                return None
            path = files[0]
            self._claimed.add(path)
            return path

    def release(self, path: Path) -> None:
        with self._lock:
            self._claimed.discard(_safe_child(self.root, path))

    def load(self, path: Path) -> dict[str, Any]:
        safe = _safe_child(self.root, path)
        descriptor = self._open_readonly(safe)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(self.max_bytes + 1)
        if len(raw) > self.max_bytes:
            raise ValueError("spooled event exceeds limit")
        try:
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("corrupt spooled event") from None
        if not isinstance(value, dict):
            raise ValueError("invalid spooled event")
        return value

    def remove(self, path: Path) -> None:
        with self._lock:
            safe = _safe_child(self.root, path)
            self._claimed.discard(safe)
            try:
                safe.unlink()
                _fsync_directory(self.root)
            except FileNotFoundError:
                pass

    def quarantine(self, path: Path) -> None:
        """Move corrupt data aside without parsing or exposing its contents."""
        with self._lock:
            safe = _safe_child(self.root, path)
            self._claimed.discard(safe)
            if not safe.exists() or safe.is_symlink():
                return
            destination = self._quarantine / f"{safe.stem}-{uuid.uuid4().hex}.bad"
            os.replace(safe, destination)
            _chmod_private(destination, 0o600)
            self.quarantined_count += 1
            self._trim_quarantine_locked()
            _fsync_directory(self.root)
            _fsync_directory(self._quarantine)

    def __len__(self) -> int:
        with self._lock:
            return len(self._files_locked())

    def _write_payload_locked(self, target: Path, payload: bytes) -> None:
        target = _safe_child(self.root, target)
        temporary = self.root / f".{target.name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _chmod_private(temporary, 0o600)
            os.replace(temporary, target)
            _chmod_private(target, 0o600)
            _fsync_directory(self.root)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _open_readonly(path: Path) -> int:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise ValueError("spool path is not a regular file")
        return descriptor

    def _files_locked(self) -> list[Path]:
        files: list[Path] = []
        for item in self.root.iterdir():
            if item.name.endswith(".json") and item.is_file() and not item.is_symlink():
                files.append(item)
        return sorted(files, key=lambda item: item.name)

    def _trim_locked(self, *, protected: Path | None = None) -> None:
        protected_paths = set(self._claimed)
        if protected is not None:
            protected_paths.add(protected)
        files = self._files_locked()
        sizes: dict[Path, int] = {}
        for path in files:
            try:
                sizes[path] = path.stat(follow_symlinks=False).st_size
            except FileNotFoundError:
                sizes[path] = 0
        total = sum(sizes.values())
        changed = False
        while files and (len(files) > self.max_items or total > self.max_bytes):
            victim = next((path for path in files if path not in protected_paths), None)
            if victim is None:
                break
            files.remove(victim)
            try:
                victim.unlink()
                total -= sizes[victim]
                self.evicted_count += 1
                changed = True
            except FileNotFoundError:
                continue
        if changed:
            _fsync_directory(self.root)

    def _trim_quarantine_locked(self) -> None:
        files = sorted(
            (
                item
                for item in self._quarantine.iterdir()
                if item.is_file() and not item.is_symlink() and item.name.endswith(".bad")
            ),
            key=lambda item: item.name,
        )
        sizes: dict[Path, int] = {}
        for item in files:
            try:
                sizes[item] = item.stat(follow_symlinks=False).st_size
            except FileNotFoundError:
                sizes[item] = 0
        total = sum(sizes.values())
        while files and (len(files) > self.max_items or total > self.max_bytes):
            victim = files.pop(0)
            try:
                victim.unlink()
                total -= sizes[victim]
            except FileNotFoundError:
                continue
