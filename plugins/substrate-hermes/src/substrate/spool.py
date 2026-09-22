"""Durable write-ahead spool for ledger-event delivery.

Port of the v2 ``DurableSpool`` design (``hermes-substrate-wiki`` 2.0.4,
``src/substrate_wiki/spool.py``) with the v5 additions from
``plugin-architecture.md`` section 4: per-item priorities, eviction of the
oldest item in the lowest priority present (never in-flight), durable SQLite
``spool_counters`` updated in the same transaction as every state change, one
priority-ordered sender thread with backoff, and strict ACK retirement.

Standard library only.
"""

from __future__ import annotations

import json
import math
import os
import random
import sqlite3
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contract import import_ack_ok, to_import_item, validate_mcp_import_response

# Priorities: lower number sends first and is evicted last.
PRIORITY_EXPLICIT = 0  # memory_write, memory_forget, consent
PRIORITY_LIVE = 1  # capture_turn, capture_session (live + boundaries)
PRIORITY_CATCHUP = 2  # incremental catch-up
PRIORITY_REPLAY = 3  # history_replay, evicted first under pressure

_PRIORITIES = frozenset({PRIORITY_EXPLICIT, PRIORITY_LIVE, PRIORITY_CATCHUP, PRIORITY_REPLAY})

# Counter outcomes recorded in spool_counters.
OUTCOME_ENQUEUED = "enqueued"
OUTCOME_EVICTED = "evicted"
OUTCOME_QUARANTINED = "quarantined"
OUTCOME_DELIVERED = "delivered"
OUTCOME_CLAIMED = "claimed"
OUTCOME_RELEASED = "released"
OUTCOME_SPOOL_FULL = "spool_full"

_STATE_QUEUED = "queued"
_STATE_CLAIMED = "claimed"

# Sender backoff (seconds).
RETRY_BASE_SECONDS = 1.0
AUTH_RETRY_BASE_SECONDS = 30.0
MAX_RETRY_SECONDS = 300.0
_MAX_RETRY_EXPONENT = 8
_RETRY_JITTER_FRACTION = 0.2
_SEND_TIMEOUT = 5.0
_SEND_MAX_RESPONSE_BYTES = 65_536
# memory_import batch bounds (docs/mcp-contract.md section 4.7): at most 64
# items and 240 KiB of items per tools/call so the request stays far below
# the 262144-byte /mcp body limit.
_IMPORT_MAX_ITEMS = 64
_IMPORT_MAX_BYTES = 240 * 1024

# Categories that use the 30 s auth backoff base (token may need reconnect).
_AUTH_CATEGORIES = frozenset({
    "unauthorized", "forbidden", "not_configured", "invalid_api_url",
    "http_401", "http_403",
})
# Categories that are safe to retry (transient). Everything else is permanent
# and the item is quarantined with a durable counter instead of retried.
_TRANSIENT_CATEGORIES = frozenset({
    "timeout", "transport_error", "not_configured", "invalid_api_url",
    "unauthorized", "forbidden", "rate_limited", "server_error",
    "http_401", "http_403", "http_429",
})


def _transport_error(category: str) -> Exception:
    """Fabricate a transient client-style failure for a bad import body."""
    error = RuntimeError(category)
    error.category = category  # type: ignore[attr-defined]
    error.retry_after = None  # type: ignore[attr-defined]
    error.transient = True  # type: ignore[attr-defined]
    return error


class SpoolFull(RuntimeError):
    """Raised only for PRIORITY_EXPLICIT when no lower-priority room exists."""


def is_transient_category(category: str) -> bool:
    """True when a delivery failure with this category should be retried."""
    if category in _TRANSIENT_CATEGORIES:
        return True
    if category.startswith("http_"):
        try:
            code = int(category[5:])
        except ValueError:
            return False
        if code in (401, 403, 429):
            return True
        return code >= 500
    return False


def retry_delay(
    category: str,
    failure_streak: int,
    *,
    retry_after: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Backoff: 1 s base (30 s auth), doubling, jittered, component capped at
    300 s. A server Retry-After is a floor honored even above 300 s; the
    sender always waits interruptibly so stop() still lands promptly."""
    base = AUTH_RETRY_BASE_SECONDS if category in _AUTH_CATEGORIES else RETRY_BASE_SECONDS
    exponent = min(max(0, failure_streak - 1), _MAX_RETRY_EXPONENT)
    delay = min(MAX_RETRY_SECONDS, base * (2 ** exponent))
    jittered = min(
        delay * (rng or random).uniform(
            1.0 - _RETRY_JITTER_FRACTION, 1.0 + _RETRY_JITTER_FRACTION
        ),
        MAX_RETRY_SECONDS,
    )
    if (
        isinstance(retry_after, (int, float))
        and not isinstance(retry_after, bool)
        and math.isfinite(retry_after)
    ):
        return max(jittered, max(0.0, float(retry_after)))
    return max(0.0, jittered)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _chmod_private(path: Path, mode: int) -> None:
    if os.name == "posix":
        os.chmod(path, mode)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
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
    """Exclusive-create temp file, fsync, atomic replace, directory fsync."""
    root = _safe_root(target.parent)
    target = _safe_child(root, target)
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
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


def _open_readonly(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise ValueError("spool path is not a regular file")
    return descriptor


def _read_wrapped_file(root: Path, path: Path, *, max_bytes: int) -> dict[str, Any]:
    safe = _safe_child(root, path)
    descriptor = _open_readonly(safe)
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("spooled event exceeds limit")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("corrupt spooled event") from None
    if not isinstance(value, dict):
        raise ValueError("invalid spooled event")
    return value
_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY,
  filename TEXT NOT NULL UNIQUE,
  priority INTEGER NOT NULL,
  kind TEXT NOT NULL,
  capture_origin TEXT NOT NULL,
  event_id TEXT NOT NULL,
  byte_count INTEGER NOT NULL,
  created_at REAL NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS items_send_order
  ON items (state, priority, created_at);
CREATE TABLE IF NOT EXISTS spool_counters (
  kind TEXT NOT NULL,
  capture_origin TEXT NOT NULL,
  priority INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  item_count INTEGER NOT NULL DEFAULT 0,
  byte_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (kind, capture_origin, priority, outcome)
);
"""


def _counter_key(kind: str, capture_origin: str, priority: int, outcome: str) -> str:
    return f"{kind}|{capture_origin}|{priority}|{outcome}"


class Spool:
    """Bounded durable spool: one JSON file per item plus a SQLite index."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_items: int = 1000,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self.root = _safe_root(Path(root))
        self.max_items = max(1, int(max_items))
        self.max_bytes = max(1024, int(max_bytes))
        self._lock = threading.RLock()
        self._quarantine = _safe_root(self.root / "corrupt")
        self.db_path = self.root / "spool.db"
        # Refuse to open through a symlink so state cannot be redirected.
        if self.db_path.is_symlink():
            raise ValueError("spool database must not be a symlink")
        self._db = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30.0,
            isolation_level=None,
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.executescript(_SCHEMA)
        _chmod_private(self.db_path, 0o600)
        # Sender state.
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._client: Any = None
        self._streak = 0
        self._rand = random.Random()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                # A crash may strand claimed rows; release them so nothing is
                # lost mid-flight. No counter: the claim was already counted.
                self._db.execute(
                    "UPDATE items SET state=? WHERE state=?",
                    (_STATE_QUEUED, _STATE_CLAIMED),
                )
                self._adopt_orphan_files_locked()
                self._rewrite_missing_files_locked()
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            _fsync_directory(self.root)

    # -- counters ---------------------------------------------------------

    def _bump_locked(
        self,
        kind: str,
        capture_origin: str,
        priority: int,
        outcome: str,
        byte_count: int,
    ) -> None:
        self._db.execute(
            "INSERT INTO spool_counters"
            " (kind, capture_origin, priority, outcome, item_count, byte_count, updated_at)"
            " VALUES (?, ?, ?, ?, 1, ?, ?)"
            " ON CONFLICT (kind, capture_origin, priority, outcome) DO UPDATE SET"
            " item_count = item_count + 1,"
            " byte_count = byte_count + excluded.byte_count,"
            " updated_at = excluded.updated_at",
            (kind, capture_origin, priority, outcome, byte_count, _utc_now_iso()),
        )

    def counters(self) -> dict[str, dict[str, Any]]:
        """Durable counters read back from SQLite."""
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, capture_origin, priority, outcome,"
                " item_count, byte_count, updated_at FROM spool_counters"
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for kind, origin, priority, outcome, count, nbytes, updated_at in rows:
            result[_counter_key(kind, origin, priority, outcome)] = {
                "item_count": count,
                "byte_count": nbytes,
                "updated_at": updated_at,
            }
        return result

    def pending(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) FROM items").fetchone()
        return int(row[0])

    # -- enqueue / eviction ------------------------------------------------

    @staticmethod
    def _check_enqueue_args(priority: int, kind: str, capture_origin: str) -> None:
        if isinstance(priority, bool) or priority not in _PRIORITIES:
            raise ValueError("priority must be 0, 1, 2, or 3")
        for name, value in (("kind", kind), ("capture_origin", capture_origin)):
            if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
                raise ValueError(f"{name} must be a non-empty string <= 256 bytes")

    def enqueue(
        self, envelope: dict[str, Any], *, priority: int, kind: str, capture_origin: str
    ) -> str:
        """Persist one envelope, evicting the oldest lowest-priority item if full.

        Returns normally after eviction. Raises :class:`SpoolFull` only for
        ``PRIORITY_EXPLICIT`` when no lower-priority (less important) item can
        make room. Explicit items are never silently dropped.
        """
        self._check_enqueue_args(priority, kind, capture_origin)
        if not isinstance(envelope, dict):
            raise ValueError("envelope must be an object")
        envelope = dict(envelope)
        event_id = envelope.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            event_id = str(uuid.uuid4())
            envelope["event_id"] = event_id
        payload = (
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        ).encode("utf-8")
        byte_count = len(payload)
        if byte_count > self.max_bytes and priority != PRIORITY_EXPLICIT:
            # A single item that can never fit is quarantined with a durable
            # counter rather than silently dropped.
            return self._quarantine_oversize_locked(
                envelope, event_id, priority, kind, capture_origin, payload, byte_count
            )
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                item_id = self._enqueue_locked(
                    envelope, event_id, priority, kind, capture_origin, payload, byte_count
                )
                self._db.execute("COMMIT")
            except SpoolFull:
                self._db.execute("ROLLBACK")
                # The spool_full failure itself is durable: record it in a
                # fresh transaction (the failed attempt rolled back).
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    self._bump_locked(
                        kind, capture_origin, priority, OUTCOME_SPOOL_FULL, byte_count
                    )
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
                raise
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            _fsync_directory(self.root)
            if self._thread is not None and self._thread.is_alive():
                self._wake.set()
            return item_id

    def _enqueue_locked(
        self,
        envelope: dict[str, Any],
        event_id: str,
        priority: int,
        kind: str,
        capture_origin: str,
        payload: bytes,
        byte_count: int,
    ) -> str:
        if byte_count > self.max_bytes:
            # Explicit oversize: durable spool_full failure, never silent.
            # (The counter lands in enqueue()'s SpoolFull handler.)
            raise SpoolFull(f"spool_full: single event exceeds {self.max_bytes} bytes")
        count, total = self._totals_locked()
        while count + 1 > self.max_items or total + byte_count > self.max_bytes:
            victim = self._eviction_victim_locked(priority)
            if victim is None:
                break
            self._evict_row_locked(victim)
            count -= 1
            total -= victim["byte_count"]
        if count + 1 > self.max_items or total + byte_count > self.max_bytes:
            if priority == PRIORITY_EXPLICIT:
                raise SpoolFull("spool_full: no lower-priority item can make room")
            # Degenerate case (everything in flight): keep the item rather
            # than drop it; bounds are re-enforced on the next enqueue.
        item_id = uuid.uuid4().hex
        created_at = time.time()
        filename = f"{int(created_at * 1_000_000_000):020d}-{item_id}.json"
        wrapper = {
            "item_id": item_id,
            "priority": priority,
            "kind": kind,
            "capture_origin": capture_origin,
            "event_id": event_id,
            "byte_count": byte_count,
            "created_at": created_at,
            "envelope": envelope,
        }
        # Write-ahead file first: a crash before COMMIT leaves an orphan file
        # that __init__ adopts, so the item is never lost.
        self._write_wrapper_locked(filename, wrapper)
        self._db.execute(
            "INSERT INTO items (item_id, filename, priority, kind, capture_origin,"
            " event_id, byte_count, created_at, state, attempts, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?)",
            (item_id, filename, priority, kind, capture_origin,
             event_id, byte_count, created_at, payload),
        )
        self._bump_locked(kind, capture_origin, priority, OUTCOME_ENQUEUED, byte_count)
        return item_id

    def _totals_locked(self) -> tuple[int, int]:
        row = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(byte_count), 0) FROM items"
        ).fetchone()
        return int(row[0]), int(row[1])

    def _eviction_victim_locked(self, new_priority: int) -> dict[str, Any] | None:
        """Oldest queued item at the lowest priority present, never in-flight.

        Explicit arrivals only displace strictly lower-priority items; any
        other arrival may displace within its own priority.
        """
        # A low-priority arrival must never displace a higher-priority
        # (more important) item: victims are restricted to the same or a lower
        # importance level. Explicit arrivals only displace strictly
        # lower-priority items, so another explicit item is never evicted.
        row = self._db.execute(
            "SELECT item_id, filename, priority, kind, capture_origin, byte_count"
            " FROM items WHERE state=? AND priority>=? "
            " ORDER BY priority DESC, created_at ASC, rowid ASC LIMIT 1",
            (_STATE_QUEUED, new_priority + 1 if new_priority == PRIORITY_EXPLICIT else new_priority),
        ).fetchone()
        if row is None:
            return None
        return {
            "item_id": row[0], "filename": row[1], "priority": row[2],
            "kind": row[3], "capture_origin": row[4], "byte_count": row[5],
        }

    def _evict_row_locked(self, victim: dict[str, Any]) -> None:
        self._unlink_file_locked(victim["filename"])
        self._db.execute("DELETE FROM items WHERE item_id=?", (victim["item_id"],))
        self._bump_locked(
            victim["kind"], victim["capture_origin"], victim["priority"],
            OUTCOME_EVICTED, victim["byte_count"],
        )

    def _quarantine_oversize_locked(
        self,
        envelope: dict[str, Any],
        event_id: str,
        priority: int,
        kind: str,
        capture_origin: str,
        payload: bytes,
        byte_count: int,
    ) -> str:
        item_id = uuid.uuid4().hex
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                destination = f"{int(time.time() * 1_000_000_000):020d}-{item_id}.bad"
                self._write_raw_locked(self._quarantine, destination, payload)
                self._bump_locked(
                    kind, capture_origin, priority, OUTCOME_QUARANTINED, byte_count
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return item_id

    # -- file helpers (caller holds the lock) -------------------------------

    def _write_wrapper_locked(self, filename: str, wrapper: dict[str, Any]) -> None:
        target = _safe_child(self.root, self.root / filename)
        payload = (
            json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("utf-8")
        temporary = self.root / f".{filename}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
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
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _write_raw_locked(directory: Path, filename: str, payload: bytes) -> None:
        target = _safe_child(directory, directory / filename)
        temporary = directory / f".{filename}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
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
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _unlink_file_locked(self, filename: str) -> None:
        try:
            safe = _safe_child(self.root, self.root / filename)
        except ValueError:
            return
        try:
            safe.unlink()
        except FileNotFoundError:
            pass

    # -- recovery ------------------------------------------------------------

    def _adopt_orphan_files_locked(self) -> None:
        """Adopt write-ahead files left by a crash before COMMIT (or adopt)."""
        try:
            names = sorted(
                item.name for item in self.root.iterdir()
                if item.name.endswith(".json") and item.is_file() and not item.is_symlink()
            )
        except OSError:
            return
        known = {
            row[0]
            for row in self._db.execute("SELECT filename FROM items").fetchall()
        }
        for name in names:
            if name in known:
                continue
            try:
                wrapper = _read_wrapped_file(self.root, self.root / name, max_bytes=self.max_bytes)
                item_id = wrapper["item_id"]
                priority = wrapper["priority"]
                kind = wrapper["kind"]
                capture_origin = wrapper["capture_origin"]
                event_id = wrapper["event_id"]
                envelope = wrapper["envelope"]
                self._check_enqueue_args(priority, kind, capture_origin)
                if not isinstance(envelope, dict) or not isinstance(event_id, str):
                    raise ValueError("bad wrapper")
                payload = (
                    json.dumps(envelope, ensure_ascii=False,
                               separators=(",", ":"), sort_keys=True)
                ).encode("utf-8")
                self._db.execute(
                    "INSERT OR IGNORE INTO items (item_id, filename, priority, kind,"
                    " capture_origin, event_id, byte_count, created_at, state,"
                    " attempts, payload)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?)",
                    (item_id, name, priority, kind, capture_origin, event_id,
                     len(payload), float(wrapper.get("created_at", time.time())), payload),
                )
                self._bump_locked(kind, capture_origin, priority, OUTCOME_ENQUEUED, len(payload))
            except (OSError, ValueError, KeyError, TypeError):
                try:
                    safe = _safe_child(self.root, self.root / name)
                    destination = f"{safe.stem}-{uuid.uuid4().hex}.bad"
                    os.replace(safe, self._quarantine / destination)
                    _chmod_private(self._quarantine / destination, 0o600)
                except (OSError, ValueError):
                    pass
                self._bump_locked("unknown", "unknown", PRIORITY_REPLAY,
                                  OUTCOME_QUARANTINED, 0)

    def _rewrite_missing_files_locked(self) -> None:
        """Rewrite write-ahead files lost after COMMIT (payload BLOB is truth)."""
        rows = self._db.execute("SELECT filename, payload FROM items").fetchall()
        for filename, payload in rows:
            try:
                candidate = self.root / filename
                if candidate.is_file() and not candidate.is_symlink():
                    continue
            except OSError:
                continue
            try:
                envelope = json.loads(bytes(payload).decode("utf-8"))
            except (UnicodeError, ValueError, TypeError):
                continue
            row = self._db.execute(
                "SELECT item_id, priority, kind, capture_origin, event_id,"
                " byte_count, created_at FROM items WHERE filename=?",
                (filename,),
            ).fetchone()
            if row is None:
                continue
            self._write_wrapper_locked(filename, {
                "item_id": row[0], "priority": row[1], "kind": row[2],
                "capture_origin": row[3], "event_id": row[4],
                "byte_count": row[5], "created_at": row[6], "envelope": envelope,
            })
    # -- claim / release / retire / quarantine -------------------------------

    def claim(self) -> dict[str, Any] | None:
        """Reserve the oldest queued item (priority first, then FIFO).

        Claimed items are in-flight and can never be evicted. A crash before
        release/retire leaves the row claimed; reopening the spool releases it.
        """
        with self._lock:
            while True:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    row = self._db.execute(
                        "SELECT item_id, filename, priority, kind, capture_origin,"
                        " event_id, byte_count, attempts, payload FROM items"
                        " WHERE state=? ORDER BY priority ASC, created_at ASC,"
                        " rowid ASC LIMIT 1",
                        (_STATE_QUEUED,),
                    ).fetchone()
                    if row is None:
                        self._db.execute("COMMIT")
                        return None
                    (item_id, filename, priority, kind, origin, event_id,
                     byte_count, attempts, payload) = row
                    try:
                        envelope = json.loads(bytes(payload).decode("utf-8"))
                        if not isinstance(envelope, dict):
                            raise ValueError("bad payload")
                    except (UnicodeError, ValueError, TypeError):
                        self._quarantine_row_locked(
                            item_id, filename, priority, kind, origin, byte_count
                        )
                        self._db.execute("COMMIT")
                        continue
                    self._db.execute(
                        "UPDATE items SET state=?, attempts=attempts+1 WHERE item_id=?",
                        (_STATE_CLAIMED, item_id),
                    )
                    self._bump_locked(kind, origin, priority, OUTCOME_CLAIMED, 0)
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
                return {
                    "item_id": item_id, "priority": priority, "kind": kind,
                    "capture_origin": origin, "event_id": event_id,
                    "byte_count": byte_count, "attempts": attempts + 1,
                    "envelope": envelope,
                }

    def release(self, item_id: str) -> None:
        """Return a claimed item to the queue (transient failure path)."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT kind, capture_origin, priority FROM items"
                    " WHERE item_id=? AND state=?",
                    (item_id, _STATE_CLAIMED),
                ).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return
                self._db.execute(
                    "UPDATE items SET state=? WHERE item_id=?",
                    (_STATE_QUEUED, item_id),
                )
                self._bump_locked(row[0], row[1], row[2], OUTCOME_RELEASED, 0)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def retire(self, item_id: str) -> bool:
        """Remove an item after a valid ACK, with a durable delivered counter."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT filename, priority, kind, capture_origin, byte_count"
                    " FROM items WHERE item_id=?",
                    (item_id,),
                ).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return False
                filename, priority, kind, origin, byte_count = row
                # Unlink before DELETE: a crash in between leaves a row without
                # a file, which recovery rewrites from the payload BLOB.
                self._unlink_file_locked(filename)
                self._db.execute("DELETE FROM items WHERE item_id=?", (item_id,))
                self._bump_locked(kind, origin, priority, OUTCOME_DELIVERED, byte_count)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            _fsync_directory(self.root)
            return True

    def quarantine(self, item_id: str) -> bool:
        """Move an item aside with a durable quarantined counter."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT filename, priority, kind, capture_origin, byte_count"
                    " FROM items WHERE item_id=?",
                    (item_id,),
                ).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return False
                filename, priority, kind, origin, byte_count = row
                self._quarantine_row_locked(
                    item_id, filename, priority, kind, origin, byte_count
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            _fsync_directory(self.root)
            _fsync_directory(self._quarantine)
            return True

    def _quarantine_row_locked(
        self,
        item_id: str,
        filename: str,
        priority: int,
        kind: str,
        capture_origin: str,
        byte_count: int,
    ) -> None:
        try:
            safe = _safe_child(self.root, self.root / filename)
            destination = f"{safe.stem}-{uuid.uuid4().hex}.bad"
            try:
                os.replace(safe, self._quarantine / destination)
                _chmod_private(self._quarantine / destination, 0o600)
            except FileNotFoundError:
                pass
        except ValueError:
            pass
        self._db.execute("DELETE FROM items WHERE item_id=?", (item_id,))
        self._bump_locked(kind, capture_origin, priority, OUTCOME_QUARANTINED, byte_count)

    # -- sender --------------------------------------------------------

    def start(self, client: Any) -> None:
        """Start the single sender thread (idempotent; swaps the client)."""
        with self._lock:
            self._client = client
            thread = self._thread
            if thread is not None and thread.is_alive():
                return
            self._stop.clear()
            self._wake.set()
            self._thread = threading.Thread(
                target=self._sender_loop, name="substrate-spool-sender", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown and wait up to ``timeout`` seconds (default 5 s)."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))

    def _sleep_interruptible(self, seconds: float) -> bool:
        """Sleep in short slices; return True when stopping."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if self._stop.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._stop.is_set()
            self._stop.wait(min(remaining, 0.5))

    def _sender_loop(self) -> None:
        while not self._stop.is_set():
            batch = self.claim_batch()
            if not batch:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            self._deliver_batch(batch)
        # Do not strand in-flight reservations on shutdown: release them so a
        # reopen (or a later start) redelivers instead of losing them.
        return

    def claim_batch(self) -> list[dict[str, Any]]:
        """Reserve up to 64 queued items within 240 KiB (priority, then FIFO).

        Claimed items are in-flight and can never be evicted. A crash before
        release/retire leaves rows claimed; reopening the spool releases them.
        """
        batch: list[dict[str, Any]] = []
        batch_bytes = 0
        while len(batch) < _IMPORT_MAX_ITEMS:
            claimed = self.claim()
            if claimed is None:
                break
            try:
                size = len(json.dumps(
                    claimed["envelope"], sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8"))
            except (TypeError, ValueError):
                size = claimed.get("byte_count", 0) or 0
            if batch and batch_bytes + size > _IMPORT_MAX_BYTES:
                self.release(claimed["item_id"])
                break
            batch.append(claimed)
            batch_bytes += size
        return batch

    def _release_all(self, batch: list[dict[str, Any]]) -> None:
        for claimed in batch:
            try:
                self.release(claimed["item_id"])
            except Exception:  # noqa: BLE001 - best effort release
                pass

    def _import_once(self, client: Any, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Send one ``memory_import`` tools/call; return its structured result."""
        structured, _text = client.call_tool(
            "memory_import",
            {"items": items},
            timeout=_SEND_TIMEOUT,
            max_response_bytes=_SEND_MAX_RESPONSE_BYTES,
        )
        return structured

    def _settle_batch(
        self, batch: list[dict[str, Any]], structured: Any
    ) -> bool:
        """Retire/quarantine each item by its per-item action.

        Returns True when every item reached a terminal state (retired or
        quarantined); items with no usable per-item result are released for
        a later retry and yield False.
        """
        try:
            checked = validate_mcp_import_response(structured)
        except Exception:  # noqa: BLE001 - a bad import body is never fatal
            self._release_all(batch)
            self._streak += 1
            self._sleep_interruptible(
                retry_delay("transport_error", self._streak, rng=self._rand)
            )
            return False
        by_index = {
            entry.get("index"): entry
            for entry in checked["results"]
            if isinstance(entry, dict)
        }
        settled_all = True
        for position, claimed in enumerate(batch):
            entry = by_index.get(position)
            if not isinstance(entry, dict):
                try:
                    self.release(claimed["item_id"])
                except Exception:  # noqa: BLE001 - best effort release
                    pass
                settled_all = False
                continue
            action = entry.get("action")
            if action == "rejected":
                try:
                    self.quarantine(claimed["item_id"])
                except Exception:  # noqa: BLE001 - best effort quarantine
                    pass
                continue
            try:
                ok = import_ack_ok(entry, claimed["event_id"])
            except Exception:  # noqa: BLE001 - a bad entry is never fatal
                ok = False
            if ok:
                try:
                    self.retire(claimed["item_id"])
                except Exception:  # noqa: BLE001 - best effort retire
                    pass
            else:
                try:
                    self.release(claimed["item_id"])
                except Exception:  # noqa: BLE001 - best effort release
                    pass
                settled_all = False
        if settled_all:
            self._streak = 0
        else:
            self._streak += 1
            self._sleep_interruptible(
                retry_delay("transport_error", self._streak, rng=self._rand)
            )
        return settled_all

    def _deliver_single(
        self, client: Any, claimed: dict[str, Any]
    ) -> str:
        """Deliver one item alone; isolate poison items from a failed batch.

        Returns "retired", "quarantined", or "retry" (released + backed off).
        """
        try:
            item = to_import_item(claimed["envelope"])
        except Exception:  # noqa: BLE001 - categories only, never content
            try:
                self.quarantine(claimed["item_id"])
            except Exception:  # noqa: BLE001 - best effort quarantine
                pass
            return "quarantined"
        try:
            structured = self._import_once(client, [item])
        except Exception as exc:  # noqa: BLE001 - categories only, never content
            return self._request_failed([claimed], exc)
        try:
            checked = validate_mcp_import_response(structured)
        except Exception:  # noqa: BLE001 - a bad import body is never fatal
            return self._request_failed(
                [claimed], _transport_error("transport_error")
            )
        entries = checked["results"]
        entry = entries[0] if entries else None
        if not isinstance(entry, dict):
            try:
                self.release(claimed["item_id"])
            except Exception:  # noqa: BLE001 - best effort release
                pass
            self._streak += 1
            self._sleep_interruptible(
                retry_delay("transport_error", self._streak, rng=self._rand)
            )
            return "retry"
        if entry.get("action") == "rejected":
            try:
                self.quarantine(claimed["item_id"])
            except Exception:  # noqa: BLE001 - best effort quarantine
                pass
            return "quarantined"
        try:
            ok = import_ack_ok(entry, claimed["event_id"])
        except Exception:  # noqa: BLE001 - a bad entry is never fatal
            ok = False
        if ok:
            try:
                self.retire(claimed["item_id"])
            except Exception:  # noqa: BLE001 - best effort retire
                pass
            self._streak = 0
            return "retired"
        try:
            self.release(claimed["item_id"])
        except Exception:  # noqa: BLE001 - best effort release
            pass
        self._streak += 1
        self._sleep_interruptible(
            retry_delay("transport_error", self._streak, rng=self._rand)
        )
        return "retry"

    def _request_failed(
        self, batch: list[dict[str, Any]], exc: Exception
    ) -> str:
        """Handle a failed ``memory_import`` request; release + backoff or quarantine."""
        category = getattr(exc, "category", "transport_error")
        if not isinstance(category, str) or not category:
            category = "transport_error"
        retry_after = getattr(exc, "retry_after", None)
        # The client marks permanent failures (invalid_request, not_found,
        # bad responses) transient=False; honor that flag so a poison batch
        # is split into single-item deliveries instead of retried forever.
        # Endpoint-specific override: this sender only delivers import
        # batches, where anything but a valid per-item action is a transient
        # failure. An undecodable 200 (invalid_response) must therefore stay
        # spooled and retry, never quarantine.
        flag = getattr(exc, "transient", None)
        transient = flag if isinstance(flag, bool) else is_transient_category(category)
        if category == "invalid_response":
            transient = True
        if not transient:
            if len(batch) > 1:
                return "split"
            try:
                self.quarantine(batch[0]["item_id"])
            except Exception:  # noqa: BLE001 - best effort quarantine
                pass
            self._streak = 0
            return "quarantined"
        self._release_all(batch)
        self._streak += 1
        self._sleep_interruptible(
            retry_delay(category, self._streak, retry_after=retry_after, rng=self._rand)
        )
        return "retry"

    def _deliver_batch(self, batch: list[dict[str, Any]]) -> None:
        """Deliver one claimed batch through ``memory_import``; never raises."""
        client = self._client
        if client is None:
            self._release_all(batch)
            self._streak += 1
            self._sleep_interruptible(
                retry_delay("not_configured", self._streak, rng=self._rand)
            )
            return
        # One pass: shapeless envelopes quarantine immediately; the rest
        # stay positionally aligned with their import items.
        survivors: list[dict[str, Any]] = []
        survivor_items: list[dict[str, Any]] = []
        for claimed in batch:
            try:
                survivor_items.append(to_import_item(claimed["envelope"]))
                survivors.append(claimed)
            except Exception:  # noqa: BLE001 - categories only, never content
                try:
                    self.quarantine(claimed["item_id"])
                except Exception:  # noqa: BLE001 - best effort quarantine
                    pass
        if not survivors:
            self._streak = 0
            return
        try:
            structured = self._import_once(client, survivor_items)
        except Exception as exc:  # noqa: BLE001 - categories only, never content
            outcome = self._request_failed(survivors, exc)
            if outcome == "split":
                # Isolate the poison item: redeliver one at a time. A single
                # transport failure releases the rest with backoff.
                for claimed in survivors:
                    if self._deliver_single(client, claimed) == "retry":
                        break
            return
        self._settle_batch(survivors, structured)


    def close(self) -> None:
        """Stop the sender and close the database handle (reopenable)."""
        try:
            self.stop(timeout=5.0)
        finally:
            with self._lock:
                try:
                    self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                self._db.close()


_DEFAULT_DIR_ENV = "SUBSTRATE_SPOOL_DIR"
_global_lock = threading.Lock()
_global_spool: "Spool | None" = None


def default_spool_dir() -> Path:
    override = os.environ.get(_DEFAULT_DIR_ENV, "")
    if override:
        return Path(override)
    home = os.environ.get("HERMES_HOME", "") or str(Path.home() / ".hermes")
    return Path(home) / "substrate" / "spool"


def get_spool() -> "Spool":
    """Process-wide spool singleton (test seam: configure_spool/reset_spool)."""
    global _global_spool
    with _global_lock:
        if _global_spool is None:
            _global_spool = Spool(default_spool_dir())
        return _global_spool


def configure_spool(
    root: str | Path, *, max_items: int = 1000, max_bytes: int = 10 * 1024 * 1024
) -> "Spool":
    """Replace the singleton (stops the old sender first)."""
    global _global_spool
    with _global_lock:
        if _global_spool is not None:
            try:
                _global_spool.stop(timeout=5.0)
            finally:
                try:
                    _global_spool.close()
                except Exception:  # noqa: BLE001 - best effort swap
                    pass
        _global_spool = Spool(root, max_items=max_items, max_bytes=max_bytes)
        return _global_spool


def reset_spool() -> None:
    """Stop and drop the singleton (tests only)."""
    global _global_spool
    with _global_lock:
        spool, _global_spool = _global_spool, None
    if spool is not None:
        try:
            spool.stop(timeout=5.0)
        finally:
            try:
                spool.close()
            except Exception:  # noqa: BLE001 - best effort reset
                pass


__all__ = [
    "PRIORITY_CATCHUP",
    "PRIORITY_EXPLICIT",
    "PRIORITY_LIVE",
    "PRIORITY_REPLAY",
    "OUTCOME_CLAIMED",
    "OUTCOME_DELIVERED",
    "OUTCOME_ENQUEUED",
    "OUTCOME_EVICTED",
    "OUTCOME_QUARANTINED",
    "OUTCOME_RELEASED",
    "OUTCOME_SPOOL_FULL",
    "Spool",
    "SpoolFull",
    "configure_spool",
    "default_spool_dir",
    "get_spool",
    "is_transient_category",
    "reset_spool",
    "retry_delay",
    "secure_atomic_json_write",
]
