"""Canonical, bounded capture events shared by live streaming and replay."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, Protocol, runtime_checkable

from .redaction import iter_redacted_text_chunks, redact

SCHEMA_VERSION = 2
MAX_CAPTURE_BYTES = 256 * 1024
_EVENT_NAMESPACE = uuid.UUID("837bd8c2-df25-4a42-bdc1-d38f0c00a8bc")
_MESSAGE_FIELDS = (
    "role",
    "content",
    "timestamp",
)
_TEXT_BLOCK_TYPES = {"text", "input_text", "output_text"}
_BINARY_KEYS = {
    "attachment",
    "attachments",
    "base64",
    "binary",
    "blob",
    "bytes",
    "file_content",
    "image",
    "images",
}
_DATA_URL = re.compile(r"^data:[^;,]+;base64,", re.IGNORECASE)


@runtime_checkable
class BoundedTextSource(Protocol):
    """Repeatable source for text that must not be materialized as one string."""

    def iter_text_chunks(self) -> Iterator[str]: ...


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _visible_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return "[NESTED_CONTENT_OMITTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return "[BINARY_CONTENT_OMITTED]" if _DATA_URL.match(value) else value
    if isinstance(value, list):
        return [_visible_json(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            str(key): (
                "[BINARY_CONTENT_OMITTED]"
                if str(key).casefold() in _BINARY_KEYS
                else _visible_json(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    return "[NON_JSON_CONTENT_OMITTED]"


def _visible_content(value: Any) -> Any:
    """Keep textual message material while excluding binary/media payloads."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        blocks: list[Any] = []
        for item in value:
            if isinstance(item, str):
                blocks.append(item)
                continue
            if not isinstance(item, dict):
                continue
            block_type = str(item.get("type") or "").lower()
            if block_type in _TEXT_BLOCK_TYPES and isinstance(item.get("text"), str):
                blocks.append({"type": block_type, "text": item["text"]})
        return blocks
    if isinstance(value, dict):
        block_type = str(value.get("type") or "").lower()
        if block_type in _TEXT_BLOCK_TYPES and isinstance(value.get("text"), str):
            return {"type": block_type, "text": value["text"]}
        return _visible_json(value)
    return "[NON_TEXT_CONTENT_OMITTED]"


def normalize_message(
    message: dict[str, Any],
    *,
    index: int,
    secrets: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Return a redacted, inference-safe message or ``None`` for system data."""
    role = str(message.get("role") or "").strip().lower()
    if role not in {"user", "assistant"}:
        return None
    selected: dict[str, Any] = {"index": int(index), "role": role}
    for field in _MESSAGE_FIELDS[1:]:
        if field not in message or message[field] is None:
            continue
        selected[field] = (
            _visible_content(message[field])
            if field == "content"
            else _visible_json(message[field])
        )
    # Reasoning fields, token/billing fields and arbitrary provider metadata are
    # deliberately never copied into ``selected``.
    return redact(selected, secrets)


def normalize_messages(
    messages: Iterable[dict[str, Any]],
    *,
    start_index: int = 0,
    secrets: Sequence[str] = (),
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for offset, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        capture_index = message.get("_capture_index")
        index = (
            int(capture_index)
            if isinstance(capture_index, int) and not isinstance(capture_index, bool)
            else start_index + offset
        )
        item = normalize_message(message, index=index, secrets=secrets)
        if item is not None:
            normalized.append(item)
    return normalized


def _utf8_boundaries(value: str, maximum: int) -> Iterator[tuple[int, int]]:
    """Yield bounded character slices without ever encoding the remaining suffix."""
    start = 0
    while start < len(value):
        # UTF-8 uses at least one byte per Python character, so no valid slice
        # can contain more than ``maximum`` characters. Keeping ``high`` local
        # prevents the binary search from creating multi-megabyte temporary
        # strings for a large message.
        low, high = start + 1, min(len(value), start + maximum)
        while low < high:
            middle = (low + high + 1) // 2
            if len(value[start:middle].encode("utf-8")) <= maximum:
                low = middle
            else:
                high = middle - 1
        yield start, low
        start = low


def _fragment_message(message: dict[str, Any], maximum: int) -> Iterator[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, str):
        if len(canonical_bytes(message)) <= maximum // 2:
            yield message
            return
        # Non-text visible content is already bounded by the capture hooks in
        # practice. Retain the legacy encoding for unusual structured records.
        encoded = canonical_bytes(message).decode("utf-8")
        encoded_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        count = sum(1 for _ in _utf8_boundaries(encoded, max(1024, maximum // 4)))
        for index, (start, end) in enumerate(
            _utf8_boundaries(encoded, max(1024, maximum // 4))
        ):
            yield {
                "role": message["role"],
                "index": message["index"],
                "content": encoded[start:end],
                "fragment": {
                    "encoding": "canonical-json",
                    "index": index,
                    "count": count,
                    "sha256": encoded_digest,
                },
            }
        return

    fragment_bytes = max(1024, maximum // 4)
    metadata = {key: value for key, value in message.items() if key != "content"}
    metadata_bytes = canonical_bytes(metadata)
    message_hasher = hashlib.sha256()
    message_hasher.update(b"substrate-message-v2\0")
    message_hasher.update(metadata_bytes)
    message_hasher.update(b"\0content\0")
    content_bytes = 0
    count = 0
    for start, end in _utf8_boundaries(content, fragment_bytes):
        encoded_piece = content[start:end].encode("utf-8")
        message_hasher.update(encoded_piece)
        content_bytes += len(encoded_piece)
        count += 1

    # This size estimate is deliberately conservative. The exact event-size
    # assertion still runs before delivery, but small messages avoid fragment
    # metadata and retain the original envelope shape.
    if content_bytes + len(metadata_bytes) + 256 <= maximum // 2:
        yield message
        return

    message_digest = message_hasher.hexdigest()
    for index, (start, end) in enumerate(_utf8_boundaries(content, fragment_bytes)):
        fragment = dict(metadata)
        fragment["content"] = content[start:end]
        fragment["fragment"] = {
            "encoding": "utf8-content",
            "index": index,
            "count": count,
            "sha256": message_digest,
        }
        yield fragment


def _fragment_streamed_message(
    message: dict[str, Any],
    source: BoundedTextSource,
    *,
    maximum: int,
    secrets: Sequence[str],
) -> Iterator[dict[str, Any]]:
    """Digest/count in one bounded pass, then emit fragments in a second pass."""
    fragment_bytes = max(1024, maximum // 4)
    metadata_bytes = canonical_bytes(message)
    digest = hashlib.sha256()
    digest.update(b"substrate-message-v2\0")
    digest.update(metadata_bytes)
    digest.update(b"\0content\0")
    count = 0
    for piece in _iter_streamed_utf8_fragments(source, secrets, fragment_bytes):
        digest.update(piece.encode("utf-8"))
        count += 1

    message_digest = digest.hexdigest()
    fragment_index = 0
    for piece in _iter_streamed_utf8_fragments(source, secrets, fragment_bytes):
        fragment = dict(message)
        fragment["content"] = piece
        fragment["fragment"] = {
            "encoding": "utf8-content",
            "index": fragment_index,
            "count": count,
            "sha256": message_digest,
        }
        fragment_index += 1
        yield fragment


def _utf8_prefix_end(value: str, maximum: int) -> int:
    """Return the longest leading character count within a UTF-8 byte budget."""

    if not value or maximum <= 0:
        return 0
    low, high = 1, min(len(value), maximum)
    if len(value[0].encode("utf-8")) > maximum:
        return 0
    while low < high:
        middle = (low + high + 1) // 2
        if len(value[:middle].encode("utf-8")) <= maximum:
            low = middle
        else:
            high = middle - 1
    return low


def _iter_streamed_utf8_fragments(
    source: BoundedTextSource,
    secrets: Sequence[str],
    fragment_bytes: int,
) -> Iterator[str]:
    """Pack sanitized text independently of source chunk boundaries."""

    pending = ""
    pending_bytes = 0
    for safe_chunk in iter_redacted_text_chunks(source.iter_text_chunks(), secrets):
        remaining = safe_chunk
        while remaining:
            capacity = fragment_bytes - pending_bytes
            end = _utf8_prefix_end(remaining, capacity)
            if end == 0:
                if not pending:
                    raise ValueError("fragment byte budget cannot encode one character")
                yield pending
                pending = ""
                pending_bytes = 0
                continue
            selected = remaining[:end]
            pending += selected
            selected_bytes = len(selected.encode("utf-8"))
            pending_bytes += selected_bytes
            remaining = remaining[end:]
            if pending_bytes == fragment_bytes:
                yield pending
                pending = ""
                pending_bytes = 0
    if pending:
        yield pending


class CaptureEventBuilder:
    """Build bounded events containing only session identity and raw dialogue."""

    def __init__(
        self,
        scope: dict[str, Any],
        *,
        secrets: Sequence[str] = (),
        max_capture_bytes: int = MAX_CAPTURE_BYTES,
    ) -> None:
        # ``scope`` remains accepted so older callers do not need an adapter,
        # but none of it is duplicated into the upload envelope.
        del scope
        self.secrets = tuple(secrets)
        self.max_capture_bytes = max(16 * 1024, min(max_capture_bytes, MAX_CAPTURE_BYTES))

    def message_events(
        self,
        kind: str,
        session_id: str,
        messages: Sequence[dict[str, Any]],
        *,
        start_index: int = 0,
        payload: dict[str, Any] | None = None,
        capture_origin: str = "live",
        batch_id: str = "",
        deterministic: bool = False,
    ) -> list[dict[str, Any]]:
        """Compatibility list wrapper; history replay uses the iterator directly."""
        return list(
            self.iter_message_events(
                kind,
                session_id,
                messages,
                start_index=start_index,
                payload=payload,
                capture_origin=capture_origin,
                batch_id=batch_id,
                deterministic=deterministic,
            )
        )

    def iter_message_events(
        self,
        kind: str,
        session_id: str,
        messages: Iterable[dict[str, Any]],
        *,
        start_index: int = 0,
        payload: dict[str, Any] | None = None,
        capture_origin: str = "live",
        batch_id: str = "",
        deterministic: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Yield bounded user/assistant events while retaining only one fragment."""

        def fragments() -> Iterator[dict[str, Any]]:
            for offset, raw in enumerate(messages):
                if not isinstance(raw, dict):
                    continue
                capture_index = raw.get("_capture_index")
                index = (
                    int(capture_index)
                    if isinstance(capture_index, int) and not isinstance(capture_index, bool)
                    else start_index + offset
                )
                content = raw.get("content")
                if isinstance(content, BoundedTextSource):
                    bounded = dict(raw)
                    bounded["content"] = ""
                    normalized = normalize_message(bounded, index=index, secrets=self.secrets)
                    if normalized is not None:
                        normalized.pop("content", None)
                        yield from _fragment_streamed_message(
                            normalized,
                            content,
                            maximum=self.max_capture_bytes,
                            secrets=self.secrets,
                        )
                    continue
                normalized = normalize_message(raw, index=index, secrets=self.secrets)
                if normalized is not None:
                    yield from _fragment_message(normalized, self.max_capture_bytes)

        def groups() -> Iterator[list[dict[str, Any]]]:
            current: list[dict[str, Any]] = []
            for fragment in fragments():
                candidate = [*current, fragment]
                probe = self._group_event(
                    kind,
                    session_id,
                    candidate,
                    chunk_index=999_999_999,
                    final=False,
                    payload=payload,
                    capture_origin=capture_origin,
                    batch_id=batch_id,
                    deterministic=deterministic,
                    event_id="00000000-0000-0000-0000-000000000000",
                    validate=False,
                )
                if current and len(canonical_bytes(probe)) > self.max_capture_bytes:
                    yield current
                    current = [fragment]
                else:
                    current = candidate
            if current:
                yield current

        iterator = groups()
        previous = next(iterator, None)
        if previous is None:
            yield self._event(
                kind,
                session_id,
                {
                    **(payload or {}),
                    "messages": [],
                },
                boundary={"start": start_index, "end": start_index},
                capture_origin=capture_origin,
                batch_id=batch_id,
                deterministic=deterministic,
            )
            return

        chunk_index = 0
        for current in iterator:
            yield self._group_event(
                kind,
                session_id,
                previous,
                chunk_index=chunk_index,
                final=False,
                payload=payload,
                capture_origin=capture_origin,
                batch_id=batch_id,
                deterministic=deterministic,
            )
            previous = current
            chunk_index += 1
        yield self._group_event(
            kind,
            session_id,
            previous,
            chunk_index=chunk_index,
            final=True,
            payload=payload,
            capture_origin=capture_origin,
            batch_id=batch_id,
            deterministic=deterministic,
        )

    def _group_event(
        self,
        kind: str,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        chunk_index: int,
        final: bool,
        payload: dict[str, Any] | None,
        capture_origin: str,
        batch_id: str,
        deterministic: bool,
        event_id: str | None = None,
        validate: bool = True,
    ) -> dict[str, Any]:
        event = self._event(
            kind,
            session_id,
            {
                **(payload or {}),
                "messages": messages,
            },
            boundary={
                "start": min(int(message["index"]) for message in messages),
                "end": max(int(message["index"]) for message in messages) + 1,
            },
            capture_origin=capture_origin,
            batch_id=batch_id,
            deterministic=deterministic,
            event_id=event_id,
        )
        if validate and len(canonical_bytes(event)) > self.max_capture_bytes:
            raise ValueError("capture event exceeds maximum size")
        return event

    def payload_event(
        self,
        kind: str,
        session_id: str,
        payload: dict[str, Any],
        *,
        boundary: dict[str, int] | None = None,
        capture_origin: str = "live",
        batch_id: str = "",
        deterministic: bool = False,
    ) -> dict[str, Any]:
        safe_payload = redact(payload, self.secrets)
        event = self._event(
            kind,
            session_id,
            safe_payload,
            boundary=boundary or {"start": 0, "end": 0},
            capture_origin=capture_origin,
            batch_id=batch_id,
            deterministic=deterministic,
        )
        if len(canonical_bytes(event)) <= self.max_capture_bytes:
            return event
        marker = {
            "capture_truncated": True,
            "original_bytes": len(canonical_bytes(safe_payload)),
            "sha256": content_digest(safe_payload),
        }
        return self._event(
            kind,
            session_id,
            marker,
            boundary=boundary or {"start": 0, "end": 0},
            capture_origin=capture_origin,
            batch_id=batch_id,
            deterministic=deterministic,
        )

    @staticmethod
    def _boundary(messages: Sequence[dict[str, Any]]) -> dict[str, int]:
        indexes = [int(message["index"]) for message in messages]
        return {"start": min(indexes), "end": max(indexes) + 1}

    def _event(
        self,
        kind: str,
        session_id: str,
        payload: dict[str, Any],
        *,
        boundary: dict[str, int],
        capture_origin: str,
        batch_id: str,
        deterministic: bool,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        safe_session = str(session_id)[:512]
        safe_payload = dict(payload)
        identity = {
            "kind": kind,
            "session_id": safe_session,
            "boundary": boundary,
            "payload": safe_payload,
        }
        resolved_id = event_id
        if resolved_id is None:
            resolved_id = (
                str(uuid.uuid5(_EVENT_NAMESPACE, content_digest(identity)))
                if deterministic
                else str(uuid.uuid4())
            )
        event: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": resolved_id,
            "kind": kind,
            "session_id": safe_session,
            "created_at": 0 if deterministic else time.time(),
        }
        messages = safe_payload.pop("messages", None)
        if isinstance(messages, list):
            event["messages"] = messages
        if safe_payload:
            event["data"] = safe_payload
        return event
