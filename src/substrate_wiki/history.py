"""OOM-safe Hermes history discovery and durable stream-v2 replay."""

from __future__ import annotations

import codecs
import hashlib
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from .checkpoint import (
    TERMINAL_STATES,
    ImportCheckpoint,
    SessionDescriptor,
    discover_legacy_checkpoints,
)
from .client import SubstrateAPIError, SubstrateClient, validate_capabilities
from .events import CaptureEventBuilder, content_digest
from .redaction import configured_secret_values

_HUMAN_SOURCES = {
    "acp", "api-server", "bluebubbles", "cli", "dingtalk", "discord", "email",
    "feishu", "homeassistant", "matrix", "mattermost", "qqbot", "signal", "slack",
    "sms", "teams", "telegram", "wecom", "weixin", "whatsapp",
}
_EXCLUDED_SOURCES = {"batch", "cron", "flush", "test", "webhook", "worker"}
_GROUP_CHAT_TYPES = {"channel", "group", "groupchat", "room", "supergroup"}
_TRANSIENT_CATEGORIES = {"http_429", "timeout", "transport_error"}
_JSON_CHUNK_BYTES = 64 * 1024
_JSON_MEMORY_RECORD_BYTES = 1024 * 1024
_JSON_SCALAR_BYTES = 16 * 1024
_JSON_TEXT_OUTPUT_CHARS = 64 * 1024
_SQLITE_TEXT_CHARS = 1024 * 1024
_OFFICIAL_EXPORT_MAX_ORPHAN_AGE_SECONDS = 24 * 60 * 60


class _ImportStopRequested(Exception):
    """Internal control flow for a graceful stop between durable acknowledgements."""


@dataclass(slots=True)
class HistorySession:
    """Compatibility fixture and bounded in-memory source session."""

    external_id: str
    source: str
    user_id: str
    subject_id: str
    messages: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HistoryInventory:
    """Compatibility wrapper; production adapters never build this object."""

    sessions: list[HistorySession] = field(default_factory=list)
    discovered: int = 0
    eligible: int = 0
    skipped: int = 0
    quarantined: int = 0


class ConversationReplaySource(Protocol):
    source_kind: str
    source_locator: str

    def discover(self, checkpoint: ImportCheckpoint) -> dict[str, int]: ...

    def iter_messages(
        self, session: SessionDescriptor, *, start: int
    ) -> Iterator[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class _ActiveOfficialExport:
    locator: Path
    state: str
    updated_at: float
    discovered_sessions: int


def _absolute_path(path: Path) -> Path:
    """Return an absolute path without following a final symlink."""
    return Path(os.path.abspath(os.fspath(path)))


def _official_export_spill_root(hermes_home: Path) -> Path:
    return _absolute_path(
        hermes_home / "substrate_wiki" / "imports" / "spill"
    )


def _official_export_name(name: str) -> bool:
    return name == "official-export.jsonl" or (
        name.startswith("official-export-") and name.endswith(".jsonl")
    )


def _official_export_root_is_safe(spill_root: Path) -> bool:
    root = _absolute_path(spill_root)
    # The final four components are spill/imports/substrate_wiki/HERMES_HOME.
    # Refuse traversal through any symlink in storage managed by the plugin.
    current = root
    for _ in range(4):
        if current.is_symlink():
            return False
        current = current.parent
    if not root.exists():
        return True
    try:
        details = root.lstat()
    except FileNotFoundError:
        return True
    if not stat.S_ISDIR(details.st_mode):
        return False
    if os.name == "posix":
        getuid = getattr(os, "getuid", None)
        if (
            not callable(getuid)
            or details.st_uid != int(getuid())
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            return False
    return True


def _validated_official_export_path(
    spill_root: Path, locator: str | Path
) -> Path | None:
    """Return a safe job-owned export path, refusing arbitrary checkpoint paths."""
    root = _absolute_path(spill_root)
    candidate = _absolute_path(Path(locator))
    if not _official_export_root_is_safe(root) or candidate.is_symlink():
        return None
    try:
        if (
            os.path.normcase(os.fspath(candidate.parent))
            != os.path.normcase(os.fspath(root))
            or not _official_export_name(candidate.name)
        ):
            return None
    except (OSError, ValueError):
        return None
    return candidate


def _private_official_export_file(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(details.st_mode):
        return False
    if os.name == "posix":
        getuid = getattr(os, "getuid", None)
        if (
            not callable(getuid)
            or details.st_uid != int(getuid())
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            return False
    return True


def remove_official_export_spill(
    hermes_home: Path, *, source_kind: str, source_locator: str
) -> bool:
    """Safely remove a terminal official export without trusting its checkpoint."""
    if source_kind != "hermes-official-export":
        return False
    candidate = _validated_official_export_path(
        _official_export_spill_root(hermes_home), source_locator
    )
    if candidate is None:
        return False
    try:
        candidate.unlink()
    except FileNotFoundError:
        return False
    return True


def _parse_json(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return value
    try:
        decoded, end = json.JSONDecoder().raw_decode(value)
    except json.JSONDecodeError:
        return value
    return decoded if not value[end:].strip() else value


def _message_from_row(row: dict[str, Any], index: int) -> dict[str, Any] | None:
    role = str(row.get("role") or "").lower()
    if role not in {"user", "assistant", "tool"}:
        return None
    message: dict[str, Any] = {
        "role": role,
        "content": row.get("content") or "",
        "_capture_index": index,
    }
    for key in ("tool_call_id", "tool_name", "timestamp", "platform_message_id", "name"):
        if row.get(key) is not None:
            message[key] = row[key]
    if row.get("tool_calls") is not None:
        message["tool_calls"] = _parse_json(row["tool_calls"])
    return message


def _disposition(
    *, external_id: str, source: str, user_id: str, chat_type: str
) -> tuple[str, str, str]:
    if not external_id:
        return "quarantined", "", ""
    if source in _EXCLUDED_SOURCES or source not in _HUMAN_SOURCES:
        return "skipped", "", ""
    if chat_type in _GROUP_CHAT_TYPES:
        return "quarantined", "", ""
    if source == "cli":
        return "included", "owner", ""
    if not user_id:
        return "quarantined", "", ""
    user_hash = hashlib.sha256(f"{source}\0{user_id}".encode()).hexdigest()
    return "included", user_hash[:24], user_hash


class HermesSQLiteHistorySource:
    """Read Hermes state.db through bounded cursors and a read-only transaction."""

    source_kind = "hermes-sqlite"

    def __init__(self, database: Path) -> None:
        self.database = database
        self.source_locator = os.fspath(database.resolve())

    def _connect(self) -> sqlite3.Connection:
        if not self.database.is_file() or self.database.is_symlink():
            raise FileNotFoundError(self.database)
        connection = sqlite3.connect(
            f"{self.database.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        return connection

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}

    def discover(self, checkpoint: ImportCheckpoint) -> dict[str, int]:
        connection = self._connect()
        discovered = skipped = quarantined = eligible = 0
        try:
            session_columns = self._columns(connection, "sessions")
            message_columns = self._columns(connection, "messages")
            if not {"id", "source"} <= session_columns or not {
                "session_id", "role", "content"
            } <= message_columns:
                raise sqlite3.DatabaseError("unsupported Hermes session schema")
            selected = [
                column
                for column in ("id", "source", "user_id", "chat_type", "started_at")
                if column in session_columns
            ]
            order = "started_at ASC, id ASC" if "started_at" in session_columns else "id ASC"
            cursor = connection.execute(
                f"SELECT {', '.join(selected)} FROM sessions ORDER BY {order}"  # noqa: S608
            )
            for source_order, raw in enumerate(cursor):
                discovered += 1
                row = dict(raw)
                external_id = str(row.get("id") or "").strip()
                source = str(row.get("source") or "").strip().casefold()
                user_id = str(row.get("user_id") or "").strip()
                chat_type = str(row.get("chat_type") or "").strip().casefold()
                disposition, subject_id, user_hash = _disposition(
                    external_id=external_id,
                    source=source,
                    user_id=user_id,
                    chat_type=chat_type,
                )
                if disposition != "included":
                    quarantined += int(disposition == "quarantined")
                    skipped += int(disposition == "skipped")
                    continue
                condition = "session_id=?"
                if "active" in message_columns:
                    condition += " AND active=1"
                count = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM messages WHERE {condition}",  # noqa: S608
                        (external_id,),
                    ).fetchone()[0]
                )
                if count <= 0:
                    skipped += 1
                    continue
                checkpoint.add_session(
                    SessionDescriptor(
                        external_id=external_id,
                        source=source,
                        subject_id=subject_id,
                        user_hash=user_hash,
                        ordering=source_order,
                        message_high_water=count,
                    )
                )
                eligible += 1
            checkpoint.finish_discovery()
            connection.rollback()
        finally:
            connection.close()
        result = {
            "discovered": discovered,
            "eligible": eligible,
            "skipped": skipped,
            "quarantined": quarantined,
        }
        checkpoint.set_inventory(**result)
        return result

    def iter_messages(
        self, session: SessionDescriptor, *, start: int
    ) -> Iterator[dict[str, Any]]:
        connection = self._connect()
        try:
            columns = self._columns(connection, "messages")
            selected = [
                column
                for column in (
                    "role", "tool_call_id", "tool_name", "timestamp",
                    "platform_message_id", "tool_calls", "name",
                )
                if column in columns
            ]
            locator = "id" if "id" in columns else "rowid"
            selected.extend(
                (
                    f"{locator} AS _substrate_locator",
                    "length(content) AS _substrate_content_chars",
                    "typeof(content) AS _substrate_content_type",
                    f"CASE WHEN length(content) <= {_SQLITE_TEXT_CHARS} "
                    "THEN content ELSE NULL END AS content",
                )
            )
            condition = "session_id=?"
            if "active" in columns:
                condition += " AND active=1"
            order = (
                "timestamp ASC, id ASC"
                if {"timestamp", "id"} <= columns
                else f"{locator} ASC"
            )
            cursor = connection.execute(
                f"SELECT {', '.join(selected)} FROM messages WHERE {condition} "  # noqa: S608
                f"ORDER BY {order} LIMIT ? OFFSET ?",
                (session.external_id, session.message_high_water - start, start),
            )
            for index, raw in enumerate(cursor, start=start):
                row = dict(raw)
                if (
                    row.get("_substrate_content_type") == "text"
                    and int(row.get("_substrate_content_chars") or 0) > _SQLITE_TEXT_CHARS
                ):
                    row["content"] = _SQLiteTextSource(
                        self.database,
                        locator,
                        row.get("_substrate_locator"),
                    )
                elif row.get("_substrate_content_type") != "text":
                    row["content"] = "[BINARY_CONTENT_OMITTED]"
                message = _message_from_row(row, index)
                if message is not None:
                    yield message
            connection.rollback()
        finally:
            connection.close()


@dataclass(slots=True, frozen=True)
class _SQLiteTextSource:
    """Repeatably stream one SQLite TEXT value through bounded substr queries."""

    database: Path
    locator_column: str
    locator: Any

    def iter_text_chunks(self) -> Iterator[str]:
        connection = sqlite3.connect(
            f"{self.database.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        try:
            offset = 1
            while True:
                row = connection.execute(
                    f"SELECT substr(content, ?, ?) FROM messages "  # noqa: S608
                    f"WHERE {self.locator_column}=?",
                    (offset, _SQLITE_TEXT_CHARS, self.locator),
                ).fetchone()
                value = row[0] if row else None
                if not isinstance(value, str) or not value:
                    break
                yield value
                offset += len(value)
                if len(value) < _SQLITE_TEXT_CHARS:
                    break
            connection.rollback()
        finally:
            connection.close()


@dataclass(slots=True, frozen=True)
class _JSONSpan:
    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start


class _JSONScanner:
    """Forward-only bounded scanner for locating values in one JSON record."""

    def __init__(self, raw: bytes | Path, *, start: int = 0) -> None:
        self.stream: BinaryIO = io.BytesIO(raw) if isinstance(raw, bytes) else raw.open("rb", buffering=0)
        self.stream.seek(start)
        self.position = start
        self.buffer = b""
        self.offset = 0

    def close(self) -> None:
        self.stream.close()

    def _fill(self) -> bool:
        if self.offset < len(self.buffer):
            return True
        self.buffer = self.stream.read(_JSON_CHUNK_BYTES)
        self.offset = 0
        return bool(self.buffer)

    def peek(self) -> int | None:
        return self.buffer[self.offset] if self._fill() else None

    def take(self) -> int:
        value = self.peek()
        if value is None:
            raise ValueError("unexpected end of JSON record")
        self.offset += 1
        self.position += 1
        return value

    def skip_space(self) -> None:
        while self.peek() in {9, 10, 13, 32}:
            self.take()

    def string_span(self) -> _JSONSpan:
        self.skip_space()
        start = self.position
        if self.take() != 34:
            raise ValueError("expected JSON string")
        escaped = False
        while True:
            value = self.take()
            if escaped:
                escaped = False
            elif value == 92:
                escaped = True
            elif value == 34:
                return _JSONSpan(start, self.position)

    def value_span(self) -> _JSONSpan:
        self.skip_space()
        start = self.position
        first = self.peek()
        if first is None:
            raise ValueError("missing JSON value")
        if first == 34:
            return self.string_span()
        if first in {91, 123}:
            stack: list[int] = []
            in_string = False
            escaped = False
            while True:
                value = self.take()
                if in_string:
                    if escaped:
                        escaped = False
                    elif value == 92:
                        escaped = True
                    elif value == 34:
                        in_string = False
                    continue
                if value == 34:
                    in_string = True
                elif value in {91, 123}:
                    stack.append(value)
                elif value in {93, 125}:
                    if not stack or (stack[-1], value) not in {(91, 93), (123, 125)}:
                        raise ValueError("mismatched JSON container")
                    stack.pop()
                    if not stack:
                        return _JSONSpan(start, self.position)
        while self.peek() not in {None, 9, 10, 13, 32, 44, 93, 125}:
            self.take()
        if self.position == start:
            raise ValueError("empty JSON value")
        return _JSONSpan(start, self.position)


def _read_json_span(raw: bytes | Path, span: _JSONSpan, *, maximum: int) -> bytes:
    if span.size < 0 or span.size > maximum:
        raise ValueError("JSON value exceeds bounded decode limit")
    if isinstance(raw, bytes):
        return raw[span.start : span.end]
    with raw.open("rb", buffering=0) as stream:
        stream.seek(span.start)
        value = stream.read(span.size)
    if len(value) != span.size:
        raise ValueError("truncated JSON value")
    return value


def _decode_json_span(raw: bytes | Path, span: _JSONSpan, *, maximum: int) -> Any:
    return json.loads(_read_json_span(raw, span, maximum=maximum).decode("utf-8"))


def _object_fields(raw: bytes | Path, span: _JSONSpan | None = None) -> Iterator[tuple[str, _JSONSpan]]:
    scanner = _JSONScanner(raw, start=span.start if span else 0)
    try:
        scanner.skip_space()
        if scanner.take() != 123:
            raise ValueError("JSON value is not an object")
        for _ in range(1024):
            scanner.skip_space()
            if scanner.peek() == 125:
                scanner.take()
                return
            key_span = scanner.string_span()
            key = _decode_json_span(raw, key_span, maximum=_JSON_SCALAR_BYTES)
            if not isinstance(key, str):
                raise ValueError("JSON object key is not text")
            scanner.skip_space()
            if scanner.take() != 58:
                raise ValueError("missing JSON object colon")
            yield key, scanner.value_span()
            scanner.skip_space()
            separator = scanner.take()
            if separator == 125:
                return
            if separator != 44:
                raise ValueError("invalid JSON object separator")
        raise ValueError("JSON object has too many top-level fields")
    finally:
        scanner.close()


def _array_elements(raw: bytes | Path, span: _JSONSpan) -> Iterator[_JSONSpan]:
    scanner = _JSONScanner(raw, start=span.start)
    try:
        scanner.skip_space()
        if scanner.take() != 91:
            raise ValueError("JSON value is not an array")
        while True:
            scanner.skip_space()
            if scanner.peek() == 93:
                scanner.take()
                return
            yield scanner.value_span()
            scanner.skip_space()
            separator = scanner.take()
            if separator == 93:
                return
            if separator != 44:
                raise ValueError("invalid JSON array separator")
    finally:
        scanner.close()


def _record_fields(raw: bytes | Path) -> dict[str, _JSONSpan]:
    return {key: span for key, span in _object_fields(raw)}


def _bounded_scalar(raw: bytes | Path, span: _JSONSpan | None) -> Any:
    if span is None:
        return None
    return _decode_json_span(raw, span, maximum=_JSON_SCALAR_BYTES)


def _first_span_byte(raw: bytes | Path, span: _JSONSpan) -> int | None:
    value = _read_json_span(raw, _JSONSpan(span.start, min(span.end, span.start + 1)), maximum=1)
    return value[0] if value else None


def _iter_json_range(path: Path, start: int, end: int) -> Iterator[bytes]:
    with path.open("rb", buffering=0) as stream:
        stream.seek(start)
        remaining = end - start
        while remaining > 0:
            chunk = stream.read(min(_JSON_CHUNK_BYTES, remaining))
            if not chunk:
                raise ValueError("truncated JSON string")
            remaining -= len(chunk)
            yield chunk


@dataclass(slots=True, frozen=True)
class _JSONLTextSource:
    """Repeatably decode one oversized JSON string without reading its record."""

    record: Path
    span: _JSONSpan

    def iter_text_chunks(self) -> Iterator[str]:
        if _first_span_byte(self.record, self.span) != 34:
            raise ValueError("deferred JSON content is not a string")
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        pending = ""
        escaped = False
        unicode_digits: str | None = None
        high_surrogate: int | None = None

        def append(value: str) -> list[str]:
            nonlocal pending
            if value:
                pending += value
            emitted: list[str] = []
            while len(pending) >= _JSON_TEXT_OUTPUT_CHARS:
                emitted.append(pending[:_JSON_TEXT_OUTPUT_CHARS])
                pending = pending[_JSON_TEXT_OUTPUT_CHARS:]
            return emitted

        def codepoint(value: int) -> str:
            nonlocal high_surrogate
            if 0xD800 <= value <= 0xDBFF:
                replacement = "\ufffd" if high_surrogate is not None else ""
                high_surrogate = value
                return replacement
            if 0xDC00 <= value <= 0xDFFF and high_surrogate is not None:
                combined = 0x10000 + ((high_surrogate - 0xD800) << 10) + (value - 0xDC00)
                high_surrogate = None
                return chr(combined)
            prefix = "\ufffd" if high_surrogate is not None else ""
            high_surrogate = None
            return prefix + ("\ufffd" if 0xDC00 <= value <= 0xDFFF else chr(value))

        escapes = {
            34: '"', 47: "/", 92: "\\", 98: "\b", 102: "\f",
            110: "\n", 114: "\r", 116: "\t",
        }
        for raw_chunk in _iter_json_range(self.record, self.span.start + 1, self.span.end - 1):
            cursor = 0
            while cursor < len(raw_chunk):
                if unicode_digits is not None:
                    needed = 4 - len(unicode_digits)
                    taken = raw_chunk[cursor : cursor + needed]
                    unicode_digits += taken.decode("ascii")
                    cursor += len(taken)
                    if len(unicode_digits) == 4:
                        try:
                            resolved = codepoint(int(unicode_digits, 16))
                        except ValueError as exc:
                            raise ValueError("invalid JSON unicode escape") from exc
                        unicode_digits = None
                        for emitted in append(resolved):
                            yield emitted
                    continue
                if escaped:
                    marker = raw_chunk[cursor]
                    cursor += 1
                    escaped = False
                    if marker == 117:
                        unicode_digits = ""
                        needed = min(4, len(raw_chunk) - cursor)
                        unicode_digits = raw_chunk[cursor : cursor + needed].decode("ascii")
                        cursor += needed
                        if len(unicode_digits) == 4:
                            try:
                                resolved = codepoint(int(unicode_digits, 16))
                            except ValueError as exc:
                                raise ValueError("invalid JSON unicode escape") from exc
                            unicode_digits = None
                            for emitted in append(resolved):
                                yield emitted
                    elif marker in escapes:
                        prefix = "\ufffd" if high_surrogate is not None else ""
                        high_surrogate = None
                        for emitted in append(prefix + escapes[marker]):
                            yield emitted
                    else:
                        raise ValueError("invalid JSON escape")
                    continue
                slash = raw_chunk.find(b"\\", cursor)
                end = len(raw_chunk) if slash < 0 else slash
                if end > cursor:
                    prefix = "\ufffd" if high_surrogate is not None else ""
                    high_surrogate = None
                    decoded = decoder.decode(raw_chunk[cursor:end], final=False)
                    for emitted in append(prefix + decoded):
                        yield emitted
                cursor = end
                if slash >= 0:
                    escaped = True
                    cursor += 1
        if escaped or unicode_digits is not None:
            raise ValueError("truncated JSON escape")
        suffix = decoder.decode(b"", final=True)
        if high_surrogate is not None:
            suffix = "\ufffd" + suffix
        for emitted in append(suffix):
            yield emitted
        if pending:
            yield pending


def _jsonl_message(raw: bytes | Path, span: _JSONSpan, index: int) -> dict[str, Any] | None:
    if span.size <= _JSON_MEMORY_RECORD_BYTES:
        value = _decode_json_span(raw, span, maximum=_JSON_MEMORY_RECORD_BYTES)
        return _message_from_row(value, index) if isinstance(value, dict) else None
    fields = {key: value_span for key, value_span in _object_fields(raw, span)}
    role = _bounded_scalar(raw, fields.get("role"))
    row: dict[str, Any] = {"role": role}
    content_span = fields.get("content")
    if content_span is not None:
        if content_span.size <= _JSON_MEMORY_RECORD_BYTES:
            row["content"] = _decode_json_span(
                raw, content_span, maximum=_JSON_MEMORY_RECORD_BYTES
            )
        elif isinstance(raw, Path) and _first_span_byte(raw, content_span) == 34:
            row["content"] = _JSONLTextSource(raw, content_span)
        else:
            row["content"] = "[OVERSIZED_STRUCTURED_CONTENT_OMITTED]"
    for key in ("tool_call_id", "tool_name", "timestamp", "platform_message_id", "name"):
        value_span = fields.get(key)
        if value_span is not None and value_span.size <= _JSON_SCALAR_BYTES:
            row[key] = _decode_json_span(raw, value_span, maximum=_JSON_SCALAR_BYTES)
    tool_calls = fields.get("tool_calls")
    if tool_calls is not None:
        row["tool_calls"] = (
            _decode_json_span(raw, tool_calls, maximum=_JSON_MEMORY_RECORD_BYTES)
            if tool_calls.size <= _JSON_MEMORY_RECORD_BYTES
            else "[OVERSIZED_TOOL_DATA_OMITTED]"
        )
    return _message_from_row(row, index)


def _spill_root() -> Path:
    identity = str(os.getuid()) if hasattr(os, "getuid") else str(os.getpid())
    root = Path(tempfile.gettempdir()) / f"substrate-wiki-import-{identity}"
    if root.exists() and root.is_symlink():
        raise OSError("JSONL spill root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(root, 0o700)
    for stale in root.glob("active-*.record"):
        try:
            if not stale.is_symlink() and time.time() - stale.stat().st_mtime > 24 * 3600:
                stale.unlink()
        except OSError:
            continue
    return root


def _bounded_jsonl_records(path: Path) -> Iterator[tuple[int, bytes | Path]]:
    """Yield one JSONL record using fixed reads; only the active record is retained."""
    spill_root = _spill_root()
    with path.open("rb", buffering=0) as stream:
        offset = 0
        record_start = 0
        pending = bytearray()
        spill_path: Path | None = None
        spill_stream: Any = None

        def append(value: bytes) -> None:
            nonlocal pending, spill_path, spill_stream
            if spill_stream is None and len(pending) + len(value) <= _JSON_MEMORY_RECORD_BYTES:
                pending.extend(value)
                return
            if spill_stream is None:
                spill_path = spill_root / f"active-{os.getpid()}-{time.time_ns()}.record"
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(spill_path, flags, 0o600)
                spill_stream = os.fdopen(descriptor, "wb")
                spill_stream.write(pending)
                pending.clear()
            spill_stream.write(value)

        def finish() -> bytes | Path | None:
            nonlocal pending, spill_path, spill_stream
            if spill_stream is not None:
                spill_stream.flush()
                os.fsync(spill_stream.fileno())
                spill_stream.close()
                spill_stream = None
                result: bytes | Path | None = spill_path
                spill_path = None
                return result
            if pending.strip():
                result = bytes(pending)
                pending.clear()
                return result
            pending.clear()
            return None

        while True:
            chunk = stream.read(_JSON_CHUNK_BYTES)
            if not chunk:
                record = finish()
                if record is not None:
                    try:
                        yield record_start, record
                    finally:
                        if isinstance(record, Path):
                            record.unlink(missing_ok=True)
                break
            cursor = 0
            while cursor < len(chunk):
                newline = chunk.find(b"\n", cursor)
                if newline < 0:
                    append(chunk[cursor:])
                    break
                append(chunk[cursor:newline])
                record = finish()
                if record is not None:
                    try:
                        yield record_start, record
                    finally:
                        if isinstance(record, Path):
                            record.unlink(missing_ok=True)
                record_start = offset + newline + 1
                cursor = newline + 1
            offset += len(chunk)


class HermesJSONLHistorySource:
    source_kind = "hermes-jsonl"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.source_locator = os.fspath(path.resolve())

    def discover(self, checkpoint: ImportCheckpoint) -> dict[str, int]:
        discovered = skipped = quarantined = eligible = 0
        for source_order, (offset, raw) in enumerate(_bounded_jsonl_records(self.path)):
            discovered += 1
            try:
                fields = _record_fields(raw)
                external_id = str(
                    _bounded_scalar(raw, fields.get("id"))
                    or _bounded_scalar(raw, fields.get("session_id"))
                    or ""
                ).strip()
                source = str(_bounded_scalar(raw, fields.get("source")) or "").strip().casefold()
                user_id = str(_bounded_scalar(raw, fields.get("user_id")) or "").strip()
                chat_type = str(_bounded_scalar(raw, fields.get("chat_type")) or "").strip().casefold()
                messages_span = fields.get("messages")
                message_count = (
                    sum(1 for _ in _array_elements(raw, messages_span))
                    if messages_span is not None
                    else 0
                )
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                quarantined += 1
                continue
            disposition, subject_id, user_hash = _disposition(
                external_id=external_id,
                source=source,
                user_id=user_id,
                chat_type=chat_type,
            )
            if disposition != "included":
                quarantined += int(disposition == "quarantined")
                skipped += int(disposition == "skipped")
                continue
            if message_count <= 0:
                skipped += 1
                continue
            checkpoint.add_session(
                SessionDescriptor(
                    external_id=external_id,
                    source=source,
                    subject_id=subject_id,
                    user_hash=user_hash,
                    ordering=source_order,
                    message_high_water=message_count,
                    locator=str(offset),
                )
            )
            eligible += 1
        checkpoint.finish_discovery()
        result = {
            "discovered": discovered,
            "eligible": eligible,
            "skipped": skipped,
            "quarantined": quarantined,
        }
        checkpoint.set_inventory(**result)
        return result

    def iter_messages(
        self, session: SessionDescriptor, *, start: int
    ) -> Iterator[dict[str, Any]]:
        wanted = int(session.locator or 0)
        for offset, raw in _bounded_jsonl_records(self.path):
            if offset != wanted:
                continue
            try:
                messages = _record_fields(raw).get("messages")
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                return
            if messages is None:
                return
            for index, message_span in enumerate(_array_elements(raw, messages)):
                if index < start:
                    continue
                if index >= session.message_high_water:
                    break
                message = _jsonl_message(raw, message_span, index)
                if message is not None:
                    yield message
            return


class HermesOfficialExportSource(HermesJSONLHistorySource):
    """Spill Hermes's supported export to a private JSONL file before replay."""

    source_kind = "hermes-official-export"

    def __init__(
        self,
        database: Path,
        imports_root: Path,
        *,
        existing_locator: Path | None = None,
    ) -> None:
        self.database = database
        self.imports_root = _absolute_path(imports_root)
        if existing_locator is None:
            database_key = hashlib.sha256(
                os.fspath(_absolute_path(database)).encode()
            ).hexdigest()[:24]
            target = self.imports_root / f"official-export-{database_key}.jsonl"
            if target.exists() or target.is_symlink():
                # A final spill with no active checkpoint is an orphan.  Keep
                # it for age-based recovery, but never mistake it for this new
                # job's immutable snapshot.
                nonce = hashlib.sha256(
                    f"{os.getpid()}\0{time.time_ns()}".encode()
                ).hexdigest()[:16]
                target = self.imports_root / (
                    f"official-export-{database_key}-{nonce}.jsonl"
                )
        else:
            selected = _validated_official_export_path(
                self.imports_root, existing_locator
            )
            if selected is None:
                raise RuntimeError("unsafe official export checkpoint locator")
            target = selected
        # Establish the durable locator before invoking Hermes.  ImportCheckpoint
        # can therefore attach to an active job without refreshing its immutable
        # export snapshot.
        super().__init__(target)

    def _ensure_private_root(self) -> None:
        if not _official_export_root_is_safe(self.imports_root):
            raise OSError("official export directory is not private storage")
        self.imports_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(self.imports_root, 0o700)
        if not _official_export_root_is_safe(self.imports_root):
            raise OSError("official export directory is not private storage")

    def _ensure_export(self) -> None:
        self._ensure_private_root()
        target = Path(self.source_locator)
        if target.exists():
            if not _private_official_export_file(target):
                raise OSError("official export spill is not a private regular file")
            return
        try:
            from hermes_state import SessionDB
        except ImportError as exc:  # pragma: no cover - Hermes runtime only
            raise RuntimeError("Hermes export API is unavailable") from exc

        temporary = self.imports_root / (
            f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        db = SessionDB(db_path=self.database, read_only=True)
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1  # fdopen owns the descriptor from this point.
                exporter = getattr(db, "iter_export", None)
                if not callable(exporter):
                    raise RuntimeError("Hermes streaming export API is unavailable")
                for value in exporter():
                    if isinstance(value, dict):
                        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                        stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            # Linking a completed private temporary file publishes it only if
            # no concurrent invocation has already fixed this job's snapshot.
            # Unlike os.replace(), this can never overwrite an active source.
            try:
                os.link(temporary, target)
            except FileExistsError:
                if not _private_official_export_file(target):
                    raise OSError(
                        "concurrent official export spill is not a private regular file"
                    ) from None
        finally:
            try:
                if descriptor >= 0:
                    os.close(descriptor)
            finally:
                try:
                    db.close()
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass

    def discover(self, checkpoint: ImportCheckpoint) -> dict[str, int]:
        self._ensure_export()
        return super().discover(checkpoint)


def _recover_official_export_spills(
    hermes_home: Path,
    *,
    now: float | None = None,
    max_orphan_age_seconds: float = _OFFICIAL_EXPORT_MAX_ORPHAN_AGE_SECONDS,
) -> list[_ActiveOfficialExport]:
    """Preserve active immutable exports and clean terminal/stale spill files."""
    spill_root = _official_export_spill_root(hermes_home)
    jobs_root = hermes_home / "substrate_wiki" / "imports" / "jobs"
    active: list[_ActiveOfficialExport] = []
    referenced: set[str] = set()
    checkpoint_scan_complete = True

    if jobs_root.is_dir() and not jobs_root.is_symlink():
        for job_directory in jobs_root.iterdir():
            if job_directory.is_symlink() or not job_directory.is_dir():
                continue
            checkpoint_path = job_directory / "checkpoint.db"
            if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
                continue
            connection: sqlite3.Connection | None = None
            try:
                uri = f"{checkpoint_path.resolve().as_uri()}?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=1.0)
                row = connection.execute(
                    """SELECT source_kind, source_locator, state, updated_at
                       FROM job WHERE singleton=1"""
                ).fetchone()
                if row is None or str(row[0]) != "hermes-official-export":
                    continue
                discovered_sessions = int(
                    connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                )
                candidate = _validated_official_export_path(spill_root, str(row[1]))
                state = str(row[2])
                if candidate is None:
                    if state not in TERMINAL_STATES:
                        raise RuntimeError(
                            "active official export checkpoint has an unsafe locator"
                        )
                    continue
                key = os.path.normcase(os.fspath(candidate))
                referenced.add(key)
                if state in TERMINAL_STATES:
                    remove_official_export_spill(
                        hermes_home,
                        source_kind="hermes-official-export",
                        source_locator=os.fspath(candidate),
                    )
                    continue
                if candidate.exists() and not _private_official_export_file(candidate):
                    raise RuntimeError(
                        "active official export spill is not a private regular file"
                    )
                if (
                    not candidate.exists()
                    and (state != "created" or discovered_sessions != 0)
                ):
                    raise RuntimeError("active official export spill is missing")
                active.append(
                    _ActiveOfficialExport(
                        locator=candidate,
                        state=state,
                        updated_at=float(row[3]),
                        discovered_sessions=discovered_sessions,
                    )
                )
            except sqlite3.Error:
                checkpoint_scan_complete = False
                continue
            finally:
                if connection is not None:
                    connection.close()

    if (
        checkpoint_scan_complete
        and spill_root.is_dir()
        and _official_export_root_is_safe(spill_root)
    ):
        cutoff = (time.time() if now is None else now) - max(
            0.0, max_orphan_age_seconds
        )
        for candidate in spill_root.iterdir():
            if candidate.is_symlink():
                continue
            try:
                details = candidate.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(details.st_mode):
                continue
            is_export = _official_export_name(candidate.name)
            is_temporary = (
                candidate.name.startswith(".official-export-")
                and candidate.name.endswith(".tmp")
            ) or candidate.name == "official-export.tmp"
            if not is_export and not is_temporary:
                continue
            if os.path.normcase(os.fspath(_absolute_path(candidate))) in referenced:
                continue
            if details.st_mtime <= cutoff:
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass

    active.sort(key=lambda item: item.updated_at, reverse=True)
    return active


class InMemoryHistorySource:
    """Bounded compatibility adapter used only by unit tests."""

    source_kind = "in-memory"
    source_locator = "test"

    def __init__(self, inventory: HistoryInventory) -> None:
        self.inventory = inventory
        self.by_id = {session.external_id: session for session in inventory.sessions}

    def discover(self, checkpoint: ImportCheckpoint) -> dict[str, int]:
        for ordering, session in enumerate(self.inventory.sessions):
            checkpoint.add_session(
                SessionDescriptor(
                    session.external_id,
                    session.source,
                    session.subject_id,
                    hashlib.sha256(session.user_id.encode()).hexdigest() if session.user_id else "",
                    ordering,
                    len(session.messages),
                )
            )
        checkpoint.finish_discovery()
        result = {
            "discovered": self.inventory.discovered,
            "eligible": len(self.inventory.sessions),
            "skipped": self.inventory.skipped,
            "quarantined": self.inventory.quarantined,
        }
        checkpoint.set_inventory(**result)
        return result

    def iter_messages(
        self, session: SessionDescriptor, *, start: int
    ) -> Iterator[dict[str, Any]]:
        for index, value in enumerate(self.by_id[session.external_id].messages[start:], start):
            message = dict(value)
            message["_capture_index"] = index
            yield message


def select_history_source(
    hermes_home: Path, *, export_path: Path | None = None
) -> ConversationReplaySource:
    if export_path is not None:
        return HermesJSONLHistorySource(export_path)
    active_exports = _recover_official_export_spills(hermes_home)
    database = hermes_home / "state.db"
    if active_exports:
        # Source selection must honor the locator already sealed into the
        # checkpoint even if state.db has since changed or become readable.
        return HermesOfficialExportSource(
            database,
            _official_export_spill_root(hermes_home),
            existing_locator=active_exports[0].locator,
        )
    if database.is_symlink():
        raise RuntimeError("Hermes state database must not be a symlink")
    source = HermesSQLiteHistorySource(database)
    try:
        connection = source._connect()
        session_columns = source._columns(connection, "sessions")
        message_columns = source._columns(connection, "messages")
        if not {"id", "source"} <= session_columns or not {
            "session_id", "role", "content"
        } <= message_columns:
            raise sqlite3.DatabaseError("unsupported Hermes session schema")
        connection.rollback()
        connection.close()
        return source
    except (FileNotFoundError, OSError, sqlite3.DatabaseError):
        return HermesOfficialExportSource(
            database, _official_export_spill_root(hermes_home)
        )


def _endpoint(kind: str) -> str:
    return "/api/v1/hermes/completed-sessions" if kind == "session_end" else "/api/v1/hermes/turns"


def _deliver_with_retry_meta(
    client: SubstrateClient,
    event: dict[str, Any],
    *,
    stop_requested: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any], int]:
    delay = 1.0
    retries = 0
    while True:
        if stop_requested is not None and stop_requested():
            raise _ImportStopRequested
        try:
            result = client.request(
                "POST",
                _endpoint(str(event["kind"])),
                body=event,
                idempotency_key=str(event["event_id"]),
            )
            return (result if isinstance(result, dict) else {}), retries
        except SubstrateAPIError as exc:
            transient = exc.category in _TRANSIENT_CATEGORIES or (
                exc.category.startswith("http_")
                and exc.category[5:].isdigit()
                and int(exc.category[5:]) >= 500
            )
            if not transient:
                raise
            retries += 1
            remaining = max(delay, exc.retry_after or 0.0)
            while remaining > 0:
                if stop_requested is not None and stop_requested():
                    raise _ImportStopRequested from None
                interval = min(0.25, remaining)
                time.sleep(interval)
                remaining -= interval
            delay = min(300.0, delay * 2.0)


def _deliver_with_retry(client: SubstrateClient, event: dict[str, Any]) -> dict[str, Any]:
    """Compatibility wrapper returning only the acknowledgement body."""
    return _deliver_with_retry_meta(client, event)[0]


def _peak_rss_bytes() -> int:
    try:
        import resource

        value = int(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # type: ignore[attr-defined]
        )
        return value if sys.platform == "darwin" else value * 1024
    except (AttributeError, ImportError, OSError, ValueError):
        return 0


def _legacy_candidates(hermes_home: Path, client: SubstrateClient) -> list[dict[str, Any]]:
    candidates = discover_legacy_checkpoints(hermes_home / "substrate_wiki" / "imports")
    for item in candidates:
        try:
            remote = client.import_status(str(item["batch_id"]))
        except SubstrateAPIError:
            remote = {}
        if isinstance(remote, dict):
            item["processed"] = max(0, int(remote.get("processed", 0)))
            item["delivered"] = max(0, int(remote.get("delivered", 0)))
    candidates.sort(
        key=lambda item: (
            int(item.get("processed", 0)),
            int(item.get("delivered", 0)),
            float(item.get("checkpoint_time", 0)),
        ),
        reverse=True,
    )
    return candidates


class HermesHistoryImporter:
    """Stream one acknowledged event at a time from an immutable discovery snapshot."""

    def __init__(
        self,
        *,
        hermes_home: Path,
        client: SubstrateClient,
        source: ConversationReplaySource | None = None,
        inventory: HistoryInventory | None = None,
        agent_id: str = "default",
        export_path: Path | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.hermes_home = hermes_home
        self.client = client
        self.agent_id = agent_id or "default"
        self.source = source or (
            InMemoryHistorySource(inventory)
            if inventory is not None
            else select_history_source(hermes_home, export_path=export_path)
        )
        self.checkpoint: ImportCheckpoint | None = None
        self.stop_requested = stop_requested or (lambda: False)

    def prepare(self) -> ImportCheckpoint:
        capabilities = self.client.capabilities()
        validate_capabilities(capabilities, require_replay=True, require_entity=False)
        candidates = _legacy_candidates(self.hermes_home, self.client)
        selected = str(candidates[0]["batch_id"]) if candidates else None
        checkpoint = ImportCheckpoint.create_or_attach(
            self.hermes_home,
            source_kind=self.source.source_kind,
            source_locator=self.source.source_locator,
            agent_id=self.agent_id,
            batch_id=selected,
            legacy_batches=candidates,
        )
        if checkpoint.job()["state"] == "created":
            self.source.discover(checkpoint)
            completed: set[str] = set()
            for item in candidates:
                completed.update(item.get("completed_sessions", set()))
            checkpoint.mark_completed_sessions(completed)
        self.checkpoint = checkpoint
        return checkpoint

    @property
    def batch_id(self) -> str:
        if self.checkpoint is None:
            raise RuntimeError("import has not been prepared")
        return str(self.checkpoint.job()["batch_id"])

    def run(self, *, wait: bool) -> dict[str, Any]:
        checkpoint = self.checkpoint or self.prepare()
        try:
            if self.stop_requested():
                return checkpoint.status()
            for session in checkpoint.sessions():
                if self.stop_requested() or checkpoint.job()["state"] == "cancelled":
                    return checkpoint.status()
                if not self._deliver_session(checkpoint, session):
                    return checkpoint.status()
                checkpoint.update_peak_rss(_peak_rss_bytes())
            if wait:
                while True:
                    if self.stop_requested():
                        return checkpoint.status()
                    remote = self.client.import_status(self.batch_id)
                    if isinstance(remote, dict):
                        checkpoint.update_remote(remote)
                        if remote.get("complete"):
                            break
                    for _ in range(20):
                        if self.stop_requested():
                            return checkpoint.status()
                        time.sleep(0.1)
            return checkpoint.status()
        except _ImportStopRequested:
            return checkpoint.status()
        except Exception as exc:
            checkpoint.set_state("failed", error_class=type(exc).__name__)
            raise

    def _deliver_session(
        self, checkpoint: ImportCheckpoint, session: SessionDescriptor
    ) -> bool:
        start, rolling = checkpoint.session_progress(session.external_id)
        builder = CaptureEventBuilder(
            {
                "provider_id": "substrate_wiki",
                "agent_id": self.agent_id,
                "platform": session.source,
                "subject_id": session.subject_id,
            },
            secrets=tuple(
                secret
                for secret in dict.fromkeys(
                    (*configured_secret_values(), str(getattr(self.client, "api_key", "") or ""))
                )
                if secret
            ),
        )
        messages = self.source.iter_messages(session, start=start)
        for event in builder.iter_message_events(
            "turn",
            session.external_id,
            messages,
            start_index=start,
            capture_origin="history_replay",
            batch_id=self.batch_id,
            deterministic=True,
        ):
            if self.stop_requested():
                return False
            if checkpoint.job()["state"] == "cancelled":
                return False
            event_messages = event.get("messages", [])
            if not isinstance(event_messages, list) or not event_messages:
                continue
            indexes = [
                int(message["index"])
                for message in event_messages
                if isinstance(message, dict) and isinstance(message.get("index"), int)
            ]
            if not indexes:
                continue
            event_id = str(event["event_id"])
            if checkpoint.acknowledged(event_id):
                continue
            result, retries = _deliver_with_retry_meta(
                self.client, event, stop_requested=self.stop_requested
            )
            completed_indexes = []
            for message in event_messages:
                if not isinstance(message, dict):
                    continue
                fragment = message.get("fragment")
                if not isinstance(fragment, dict) or int(fragment.get("index", 0)) + 1 >= int(
                    fragment.get("count", 1)
                ):
                    completed_indexes.append(int(message["index"]))
            boundary_start = min(indexes)
            boundary_end = max(completed_indexes) + 1 if completed_indexes else boundary_start
            rolling = content_digest({"previous": rolling, "event": event})
            checkpoint.acknowledge(
                event_id=event_id,
                external_id=session.external_id,
                boundary_start=boundary_start,
                boundary_end=boundary_end,
                phase="message",
                duplicate=bool(result.get("duplicate")),
                retry_count=retries,
                session_digest=rolling,
            )
        completion = builder.payload_event(
            "session_end",
            session.external_id,
            {
                "session_complete": True,
                "total_message_boundary": {
                    "start": 0,
                    "end": session.message_high_water,
                },
                "session_digest": rolling,
                "protocol": "stream-v2",
            },
            boundary={"start": 0, "end": session.message_high_water},
            capture_origin="history_replay",
            batch_id=self.batch_id,
            deterministic=True,
        )
        event_id = str(completion["event_id"])
        if not checkpoint.acknowledged(event_id):
            result, retries = _deliver_with_retry_meta(
                self.client, completion, stop_requested=self.stop_requested
            )
            checkpoint.acknowledge(
                event_id=event_id,
                external_id=session.external_id,
                boundary_start=0,
                boundary_end=session.message_high_water,
                phase="session_end",
                duplicate=bool(result.get("duplicate")),
                retry_count=retries,
                session_digest=rolling,
            )
        return True


def load_hermes_inventory(
    hermes_home: Path, *, export_path: Path | None = None
) -> HistoryInventory:
    """Compatibility preview implemented via a private temporary checkpoint."""
    source = select_history_source(hermes_home, export_path=export_path)
    with tempfile.TemporaryDirectory(prefix="substrate-history-preview-") as directory:
        checkpoint = ImportCheckpoint.create_or_attach(
            Path(directory),
            source_kind=source.source_kind,
            source_locator=source.source_locator,
            agent_id="preview",
        )
        counts = source.discover(checkpoint)
        inventory = HistoryInventory(
            discovered=counts["discovered"],
            eligible=counts["eligible"],
            skipped=counts["skipped"],
            quarantined=counts["quarantined"],
        )
        checkpoint.close()
        if source.source_kind == "hermes-official-export":
            remove_official_export_spill(
                hermes_home,
                source_kind=source.source_kind,
                source_locator=source.source_locator,
            )
        return inventory
