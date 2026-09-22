"""Shared wire contract between the Substrate Hermes plugin and the server.

This module is the Python twin of ``contract.mjs`` in the server repository.
Both implement the rules written down in ``CONTRACT.md`` and both are checked
against the same fixture file (``contract/envelope-fixtures.json``), whose
SHA-256 is recorded here as ``FIXTURE_SHA256``.  Standard library only.

Every validator raises :class:`ContractError` whose ``category`` is the error
category the server would answer with (``unsupported_schema``,
``unsupported_contract``, ``payload_too_large``, ``invalid_request``) or, for
server responses validated on the plugin side, ``invalid_response``.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Mapping

CONTRACT_VERSION = 1
SCHEMA_VERSION = 3

NAMESPACE = uuid.UUID("6f3a2b1c-9d8e-4f70-a1b2-c3d4e5f60718")

KINDS = frozenset(
    {
        "capture_turn",
        "capture_session",
        "memory_write",
        "memory_forget",
        "consent",
        "page_propose",
        "upload",
    }
)
PLUGIN_POSTABLE_KINDS = frozenset(
    {"capture_turn", "capture_session", "memory_write", "memory_forget", "consent"}
)
ACTIONS = frozenset({"stored", "duplicate", "sealed", "queued"})
ACTION_CLASSES = frozenset(
    {"read", "write", "execute", "network", "deploy", "delete", "delegate", "other"}
)
ARTIFACT_KEY_KINDS = frozenset({"path", "url", "host", "repo", "email", "ticket"})
ERROR_CATEGORIES = frozenset(
    {
        "unauthorized",
        "forbidden",
        "invalid_request",
        "unsupported_contract",
        "unsupported_schema",
        "payload_too_large",
        "not_found",
        "conflict",
        "rate_limited",
        "internal",
    }
)
CLIENT_ERROR_CATEGORIES = frozenset({"invalid_response", "transport_error", "timeout"})
CAPTURE_ORIGINS = frozenset({"live", "history_replay", "catchup"})
SPEAKER_ROLES = frozenset({"owner", "participant", "agent"})
MESSAGE_ROLES = frozenset({"user", "assistant", "tool"})
BOUNDARIES = frozenset({"end", "switch", "reset", "rewound", "compress"})
DURABILITIES = frozenset({"durable", "time_bounded", "transient"})
MEMORY_WRITE_SOURCES = frozenset({"memory_remember", "hermes_memory_tool"})
CONSENT_DECISIONS = frozenset({"approved", "declined", "revoked"})
EMPTY_REASONS = frozenset({"", "no_candidates", "gated", "not_implemented"})
FRAGMENT_ENCODINGS = frozenset({"utf8-content", "canonical-json"})
JOB_STATUSES = frozenset({"queued", "running", "completed", "failed"})
PIN_MODES = frozenset({"abstract", "full"})

LIMITS: dict[str, int] = {
    "max_event_bytes": 262144,
    "max_upload_bytes": 262144,
    "max_tool_call_bytes": 4096,
    "max_tool_result_bytes": 8192,
    "turn_context_deadline_ms": 500,
    "action_cues_deadline_ms": 100,
    "rules_refresh_seconds": 300,
}

MAX_SESSION_ID_BYTES = 512
MAX_ID_BYTES = 128
MAX_SPEAKER_ID_BYTES = 256
MAX_DISPLAY_BYTES = 256
MAX_TEXT_BYTES = 4096
MAX_ABOUT_BYTES = 256
MAX_REASON_BYTES = 1024
MAX_ARGS_PREVIEW_BYTES = 1024
MAX_TITLE_BYTES = 200
MAX_UPLOAD_TITLE_BYTES = 512
MAX_PROMPT_BYTES = 4096
MAX_QUERY_BYTES = 4096
MAX_FILENAME_BYTES = 256
MAX_URL_BYTES = 2048
MAX_BLOCK_BYTES = 8192
MAX_BLOCK_LINES = 40
MAX_TURN_MESSAGE_BYTES = 16384
MAX_RECENT_TURN_BYTES = 4096
MAX_RECENT_TURNS = 2
MAX_HANDLES = 64
MAX_NOTES = 3
MAX_NOTE_TEXT_BYTES = 160
MAX_RULES = 200
MAX_RULE_TEXT_BYTES = 200
MAX_ARTIFACT_KEYS = 32
MAX_ARTIFACT_KEY_BYTES = 512
MAX_PARTICIPANTS = 64
MAX_PLATFORM_BYTES = 64
MAX_CHAT_TYPE_BYTES = 32
MAX_AGENT_CONTEXT_BYTES = 32
MAX_DEADLINE_MS = 5000
MAX_SEARCH_LIMIT = 20
MAX_SEARCH_KINDS = 16
MAX_MARKDOWN_BYTES = 65536
MAX_SAFE_INTEGER = 2**53 - 1

HANDLE_RE = re.compile(r"^[mp]:[0-9a-f]{8,64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[45][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BATCH_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
HTTPS_URL_RE = re.compile(r"^https://[^\s/?#]+[^\s]*$")

# Literal SHA-256 of contract/envelope-fixtures.json.  Asserted by the tests.
FIXTURE_SHA256 = "627615398b726d04f32b5bab58b480b00ba85ca80c65d66864d7e8ea1a30ab85"

_FIXTURE_PATH = Path(__file__).resolve().parent / "contract" / "envelope-fixtures.json"


class ContractError(ValueError):
    """A contract violation; ``category`` is the wire error category."""

    def __init__(self, category: str, detail: str = "") -> None:
        self.category = category
        self.detail = detail
        super().__init__(f"{category}: {detail}" if detail else category)


# --------------------------------------------------------------------------
# Canonical JSON and deterministic ids
# --------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace, raw (non-ASCII) UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(obj: Any) -> bytes:
    return canonical_json(obj).encode("utf-8")


def deterministic_event_id(
    kind: str, session_id: str, offset: Mapping[str, int], payload: Any
) -> str:
    """uuid5(NAMESPACE, canonical_json({kind, session_id, offset, payload}))."""
    name = canonical_json(
        {
            "kind": kind,
            "session_id": session_id,
            "offset": {"start": offset["start"], "end": offset["end"]},
            "payload": payload,
        }
    )
    return str(uuid.uuid5(NAMESPACE, name))


# --------------------------------------------------------------------------
# Primitive checks.  Each raises ContractError(category, path).
# --------------------------------------------------------------------------


def _byte_len(value: str) -> int:
    return len(value.encode("utf-8"))


def _fail(category: str, path: str, why: str) -> None:
    raise ContractError(category, f"{path}: {why}")


def _obj(value: Any, path: str, allowed: frozenset[str] | set[str], required: set[str],
         category: str = "invalid_request") -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(category, path, "expected object")
    for key in value:
        if not isinstance(key, str) or key not in allowed:
            _fail(category, f"{path}.{key}", "unknown field")
    for key in required:
        if key not in value:
            _fail(category, f"{path}.{key}", "missing")
    return value


def _str(value: Any, path: str, *, max_bytes: int, min_bytes: int = 0,
         category: str = "invalid_request") -> str:
    if not isinstance(value, str):
        _fail(category, path, "expected string")
    size = _byte_len(value)
    if size < min_bytes:
        _fail(category, path, f"shorter than {min_bytes} bytes")
    if size > max_bytes:
        _fail(category, path, f"longer than {max_bytes} bytes")
    return value


def _int(value: Any, path: str, *, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER,
         category: str = "invalid_request") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(category, path, "expected integer")
    if value < minimum or value > maximum:
        _fail(category, path, f"out of range [{minimum}, {maximum}]")
    return value


def _number(value: Any, path: str, *, minimum: float = 0.0,
            category: str = "invalid_request") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(category, path, "expected number")
    if value != value or value < minimum:  # NaN or below minimum
        _fail(category, path, "out of range")
    return value


def _bool(value: Any, path: str, category: str = "invalid_request") -> bool:
    if not isinstance(value, bool):
        _fail(category, path, "expected boolean")
    return value


def _enum(value: Any, path: str, allowed: frozenset[str],
          category: str = "invalid_request") -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(category, path, f"expected one of {sorted(allowed)}")
    return value


def _match(value: Any, path: str, pattern: re.Pattern[str], *, max_bytes: int = 4096,
           category: str = "invalid_request") -> str:
    if not isinstance(value, str) or _byte_len(value) > max_bytes or not pattern.match(value):
        _fail(category, path, f"does not match {pattern.pattern}")
    return value


def _list(value: Any, path: str, *, max_items: int, min_items: int = 0,
          category: str = "invalid_request") -> list[Any]:
    if not isinstance(value, list):
        _fail(category, path, "expected array")
    if len(value) < min_items:
        _fail(category, path, f"fewer than {min_items} items")
    if len(value) > max_items:
        _fail(category, path, f"more than {max_items} items")
    return value


def _handles(value: Any, path: str, category: str = "invalid_request") -> list[str]:
    items = _list(value, path, max_items=MAX_HANDLES, category=category)
    return [_match(item, f"{path}[{i}]", HANDLE_RE, category=category)
            for i, item in enumerate(items)]


def _no_floats(value: Any, path: str, category: str = "invalid_request") -> None:
    """Envelopes carry integers only so canonical JSON is byte-identical across runtimes."""
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        _fail(category, path, "non-integer number")
    if isinstance(value, int) and abs(value) > MAX_SAFE_INTEGER:
        _fail(category, path, "integer outside +/-2^53-1")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                _fail(category, path, "non-string key")
            _no_floats(child, f"{path}.{key}", category)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _no_floats(child, f"{path}[{i}]", category)


# --------------------------------------------------------------------------
# Envelope
# --------------------------------------------------------------------------

_ENVELOPE_FIELDS = frozenset(
    {"schema_version", "contract_version", "event_id", "kind", "session_id", "offset",
     "capture_origin", "batch_id", "speaker", "created_at", "payload"}
)
_SPEAKER_FIELDS = frozenset({"id", "role", "display"})
_MESSAGE_FIELDS = frozenset(
    {"index", "role", "content", "timestamp", "speaker", "tool_calls", "tool_call_id",
     "tool_name", "result_digest", "result_bytes", "result_truncated", "fragment"}
)
_TOOL_CALL_FIELDS = frozenset(
    {"id", "tool_name", "args", "args_truncated", "args_sha256", "args_preview"}
)
_FRAGMENT_FIELDS = frozenset({"encoding", "index", "count", "sha256"})
_TURN_FIELDS = frozenset({"turn_id", "messages"})
_SESSION_FIELDS = frozenset(
    {"boundary", "session_complete", "next_session_id", "parent_session_id",
     "message_high_water", "platform", "chat_type", "participants"}
)
_PARTICIPANT_FIELDS = frozenset({"id", "display"})
_MEMORY_WRITE_FIELDS = frozenset({"text", "about", "durability", "source", "action", "target"})
_MEMORY_FORGET_FIELDS = frozenset({"handle", "reason"})
_CONSENT_FIELDS = frozenset(
    {"version", "scope", "decision", "recorded_at", "includes_other_profiles"}
)
_PAGE_PROPOSE_FIELDS = frozenset({"page_id", "title", "prompt", "session_id"})
_UPLOAD_FIELDS = frozenset({"title", "filename", "sha256", "byte_size", "source", "url"})


def _speaker(value: Any, path: str) -> dict[str, Any]:
    _obj(value, path, _SPEAKER_FIELDS, {"id", "role", "display"})
    _str(value["id"], f"{path}.id", max_bytes=MAX_SPEAKER_ID_BYTES, min_bytes=1)
    _enum(value["role"], f"{path}.role", SPEAKER_ROLES)
    _str(value["display"], f"{path}.display", max_bytes=MAX_DISPLAY_BYTES)
    return value


def _tool_call(value: Any, path: str) -> None:
    _obj(value, path, _TOOL_CALL_FIELDS, {"id", "tool_name"})
    _str(value["id"], f"{path}.id", max_bytes=MAX_ID_BYTES, min_bytes=1)
    _str(value["tool_name"], f"{path}.tool_name", max_bytes=MAX_ID_BYTES, min_bytes=1)
    truncated = value.get("args_truncated", False)
    _bool(truncated, f"{path}.args_truncated")
    if truncated:
        if "args" in value:
            _fail("invalid_request", f"{path}.args", "present in truncated form")
        if "args_sha256" not in value or "args_preview" not in value:
            _fail("invalid_request", path, "truncated form requires args_sha256 and args_preview")
        _match(value["args_sha256"], f"{path}.args_sha256", SHA256_RE)
        _str(value["args_preview"], f"{path}.args_preview", max_bytes=MAX_ARGS_PREVIEW_BYTES)
        return
    if "args_sha256" in value or "args_preview" in value:
        _fail("invalid_request", path, "args_sha256/args_preview only in truncated form")
    if "args" not in value or not isinstance(value["args"], dict):
        _fail("invalid_request", f"{path}.args", "expected object")
    if len(canonical_bytes(value["args"])) > LIMITS["max_tool_call_bytes"]:
        _fail("invalid_request", f"{path}.args",
              f"canonical JSON exceeds {LIMITS['max_tool_call_bytes']} bytes")


def _fragment(value: Any, path: str) -> None:
    _obj(value, path, _FRAGMENT_FIELDS, {"encoding", "index", "count", "sha256"})
    _enum(value["encoding"], f"{path}.encoding", FRAGMENT_ENCODINGS)
    count = _int(value["count"], f"{path}.count", minimum=1)
    _int(value["index"], f"{path}.index", minimum=0, maximum=count - 1)
    _match(value["sha256"], f"{path}.sha256", SHA256_RE)


def _message(value: Any, path: str) -> dict[str, Any]:
    _obj(value, path, _MESSAGE_FIELDS, {"index", "role", "content"})
    _int(value["index"], f"{path}.index", minimum=0)
    role = _enum(value["role"], f"{path}.role", MESSAGE_ROLES)
    content = _str(value["content"], f"{path}.content", max_bytes=LIMITS["max_event_bytes"])
    if "timestamp" in value:
        _match(value["timestamp"], f"{path}.timestamp", RFC3339_RE)
    if "speaker" in value:
        _speaker(value["speaker"], f"{path}.speaker")
    if "fragment" in value:
        _fragment(value["fragment"], f"{path}.fragment")
    if "tool_calls" in value:
        if role != "assistant":
            _fail("invalid_request", f"{path}.tool_calls", "only on assistant messages")
        calls = _list(value["tool_calls"], f"{path}.tool_calls", max_items=64)
        for i, call in enumerate(calls):
            _tool_call(call, f"{path}.tool_calls[{i}]")
    tool_only = {"tool_call_id", "tool_name", "result_digest", "result_bytes", "result_truncated"}
    if role == "tool":
        if _byte_len(content) > LIMITS["max_tool_result_bytes"]:
            _fail("invalid_request", f"{path}.content",
                  f"tool result excerpt exceeds {LIMITS['max_tool_result_bytes']} bytes")
        for key in ("result_digest", "result_bytes"):
            if key not in value:
                _fail("invalid_request", f"{path}.{key}", "missing on tool message")
        _match(value["result_digest"], f"{path}.result_digest", SHA256_RE)
        _int(value["result_bytes"], f"{path}.result_bytes", minimum=0)
        if "result_truncated" in value:
            _bool(value["result_truncated"], f"{path}.result_truncated")
        if "tool_call_id" in value:
            _str(value["tool_call_id"], f"{path}.tool_call_id", max_bytes=MAX_ID_BYTES, min_bytes=1)
        if "tool_name" in value:
            _str(value["tool_name"], f"{path}.tool_name", max_bytes=MAX_ID_BYTES, min_bytes=1)
    else:
        for key in tool_only:
            if key in value:
                _fail("invalid_request", f"{path}.{key}", "only on tool messages")
    return value


def _payload_capture_turn(payload: Any, offset: Mapping[str, int], path: str) -> None:
    _obj(payload, path, _TURN_FIELDS, {"turn_id", "messages"})
    _str(payload["turn_id"], f"{path}.turn_id", max_bytes=MAX_ID_BYTES, min_bytes=1)
    messages = _list(payload["messages"], f"{path}.messages", max_items=4096, min_items=1)
    previous = -1
    previous_fragment = False
    for i, message in enumerate(messages):
        _message(message, f"{path}.messages[{i}]")
        index = message["index"]
        has_fragment = "fragment" in message
        if index < previous or (index == previous and not (has_fragment and previous_fragment)):
            _fail("invalid_request", f"{path}.messages[{i}].index", "indices must increase")
        previous, previous_fragment = index, has_fragment
    if offset["start"] != messages[0]["index"] or offset["end"] != messages[-1]["index"] + 1:
        _fail("invalid_request", "offset", "must span messages[0].index .. messages[-1].index+1")


def _payload_capture_session(payload: Any, path: str) -> None:
    _obj(payload, path, _SESSION_FIELDS,
         {"boundary", "session_complete", "message_high_water", "platform", "chat_type"})
    _enum(payload["boundary"], f"{path}.boundary", BOUNDARIES)
    _bool(payload["session_complete"], f"{path}.session_complete")
    _int(payload["message_high_water"], f"{path}.message_high_water", minimum=0)
    _str(payload["platform"], f"{path}.platform", max_bytes=MAX_PLATFORM_BYTES, min_bytes=1)
    _str(payload["chat_type"], f"{path}.chat_type", max_bytes=MAX_CHAT_TYPE_BYTES, min_bytes=1)
    for key in ("next_session_id", "parent_session_id"):
        if key in payload:
            _str(payload[key], f"{path}.{key}", max_bytes=MAX_SESSION_ID_BYTES, min_bytes=1)
    if "participants" in payload:
        items = _list(payload["participants"], f"{path}.participants", max_items=MAX_PARTICIPANTS)
        for i, item in enumerate(items):
            ipath = f"{path}.participants[{i}]"
            _obj(item, ipath, _PARTICIPANT_FIELDS, {"id", "display"})
            _str(item["id"], f"{ipath}.id", max_bytes=MAX_SPEAKER_ID_BYTES, min_bytes=1)
            _str(item["display"], f"{ipath}.display", max_bytes=MAX_DISPLAY_BYTES)


def _payload_memory_write(payload: Any, path: str) -> None:
    _obj(payload, path, _MEMORY_WRITE_FIELDS, {"text", "durability", "source"})
    _str(payload["text"], f"{path}.text", max_bytes=MAX_TEXT_BYTES, min_bytes=1)
    _enum(payload["durability"], f"{path}.durability", DURABILITIES)
    _enum(payload["source"], f"{path}.source", MEMORY_WRITE_SOURCES)
    if "about" in payload:
        _str(payload["about"], f"{path}.about", max_bytes=MAX_ABOUT_BYTES)
    if "action" in payload:
        _str(payload["action"], f"{path}.action", max_bytes=64)
    if "target" in payload:
        _str(payload["target"], f"{path}.target", max_bytes=MAX_ABOUT_BYTES)


def _payload_memory_forget(payload: Any, path: str) -> None:
    _obj(payload, path, _MEMORY_FORGET_FIELDS, {"handle", "reason"})
    _match(payload["handle"], f"{path}.handle", HANDLE_RE)
    _str(payload["reason"], f"{path}.reason", max_bytes=MAX_REASON_BYTES)


def _payload_consent(payload: Any, path: str) -> None:
    _obj(payload, path, _CONSENT_FIELDS,
         {"version", "scope", "decision", "recorded_at", "includes_other_profiles"})
    _int(payload["version"], f"{path}.version", minimum=1, maximum=1)
    _enum(payload["scope"], f"{path}.scope", frozenset({"hermes_history"}))
    _enum(payload["decision"], f"{path}.decision", CONSENT_DECISIONS)
    _match(payload["recorded_at"], f"{path}.recorded_at", RFC3339_RE)
    _bool(payload["includes_other_profiles"], f"{path}.includes_other_profiles")


def _payload_page_propose(payload: Any, path: str) -> None:
    _obj(payload, path, _PAGE_PROPOSE_FIELDS, {"page_id", "title", "prompt", "session_id"})
    _str(payload["page_id"], f"{path}.page_id", max_bytes=MAX_ID_BYTES, min_bytes=1)
    _str(payload["title"], f"{path}.title", max_bytes=MAX_TITLE_BYTES, min_bytes=1)
    _str(payload["prompt"], f"{path}.prompt", max_bytes=MAX_PROMPT_BYTES, min_bytes=1)
    _str(payload["session_id"], f"{path}.session_id", max_bytes=MAX_SESSION_ID_BYTES, min_bytes=1)


def _payload_upload(payload: Any, path: str) -> None:
    _obj(payload, path, _UPLOAD_FIELDS, {"title", "sha256", "byte_size", "source"})
    _str(payload["title"], f"{path}.title", max_bytes=MAX_UPLOAD_TITLE_BYTES, min_bytes=1)
    _match(payload["sha256"], f"{path}.sha256", SHA256_RE)
    _int(payload["byte_size"], f"{path}.byte_size", minimum=0, maximum=LIMITS["max_upload_bytes"])
    source = _enum(payload["source"], f"{path}.source", frozenset({"content", "url"}))
    if "filename" in payload:
        _str(payload["filename"], f"{path}.filename", max_bytes=MAX_FILENAME_BYTES)
    if source == "url":
        if "url" not in payload:
            _fail("invalid_request", f"{path}.url", "missing for source=url")
        _match(payload["url"], f"{path}.url", HTTPS_URL_RE, max_bytes=MAX_URL_BYTES)
    elif "url" in payload:
        _fail("invalid_request", f"{path}.url", "only for source=url")


def validate_envelope(
    env: Any,
    *,
    idempotency_key: str | None = None,
    allowed_kinds: frozenset[str] = PLUGIN_POSTABLE_KINDS,
) -> None:
    """Validate a ledger event envelope in the server's order.

    Raises :class:`ContractError` with the category the server would return:
    ``payload_too_large`` (canonical bytes over ``max_event_bytes``),
    ``unsupported_schema``, ``unsupported_contract``, then ``invalid_request``
    for every structural problem.  ``idempotency_key`` when given must equal
    ``event_id``.  ``allowed_kinds`` defaults to what the plugin may post;
    the server uses ``KINDS`` when validating events it writes itself.
    """
    if not isinstance(env, dict):
        raise ContractError("invalid_request", "body: expected object")
    if len(canonical_bytes(env)) > LIMITS["max_event_bytes"]:
        raise ContractError("payload_too_large", "body: exceeds max_event_bytes")
    schema = env.get("schema_version")
    if isinstance(schema, bool) or schema != SCHEMA_VERSION:
        raise ContractError("unsupported_schema", f"schema_version: {schema!r}")
    contract = env.get("contract_version")
    if isinstance(contract, bool) or contract != CONTRACT_VERSION:
        raise ContractError("unsupported_contract", f"contract_version: {contract!r}")
    _obj(env, "body", _ENVELOPE_FIELDS, set(_ENVELOPE_FIELDS))
    event_id = _match(env["event_id"], "event_id", UUID_RE)
    if idempotency_key is not None and idempotency_key != event_id:
        _fail("invalid_request", "event_id", "does not match Idempotency-Key")
    kind = env["kind"]
    if not isinstance(kind, str) or kind not in KINDS:
        _fail("invalid_request", "kind", "unknown kind")
    if kind not in allowed_kinds:
        _fail("invalid_request", "kind", "not postable on this route")
    _str(env["session_id"], "session_id", max_bytes=MAX_SESSION_ID_BYTES, min_bytes=1)
    offset = _obj(env["offset"], "offset", frozenset({"start", "end"}), {"start", "end"})
    start = _int(offset["start"], "offset.start", minimum=0)
    _int(offset["end"], "offset.end", minimum=start)
    _enum(env["capture_origin"], "capture_origin", CAPTURE_ORIGINS)
    batch_id = env["batch_id"]
    if not isinstance(batch_id, str) or (batch_id != "" and not BATCH_ID_RE.match(batch_id)):
        _fail("invalid_request", "batch_id", "expected empty string or 8..64 hex")
    _speaker(env["speaker"], "speaker")
    _match(env["created_at"], "created_at", RFC3339_RE)
    payload = env["payload"]
    if not isinstance(payload, dict):
        _fail("invalid_request", "payload", "expected object")
    _no_floats(payload, "payload")
    if kind == "capture_turn":
        _payload_capture_turn(payload, offset, "payload")
    elif kind == "capture_session":
        _payload_capture_session(payload, "payload")
    elif kind == "memory_write":
        _payload_memory_write(payload, "payload")
    elif kind == "memory_forget":
        _payload_memory_forget(payload, "payload")
    elif kind == "consent":
        _payload_consent(payload, "payload")
    elif kind == "page_propose":
        _payload_page_propose(payload, "payload")
    elif kind == "upload":
        _payload_upload(payload, "payload")


def ack_ok(ack: Any, event_id: str) -> bool:
    """True iff the ACK retires the spool item (stored, same id, known action)."""
    if not isinstance(ack, dict):
        return False
    if ack.get("stored") is not True:
        return False
    if ack.get("event_id") != event_id:
        return False
    action = ack.get("action")
    return isinstance(action, str) and action in ACTIONS


# --------------------------------------------------------------------------
# Requests built by the plugin
# --------------------------------------------------------------------------

_TURN_CONTEXT_REQUEST_FIELDS = frozenset(
    {"contract_version", "session_id", "turn_id", "turn", "platform", "chat_type",
     "sender_id", "agent_identity", "agent_context", "parent_session_id", "message",
     "recent_turns", "injected_handles", "cited_handles", "deadline_ms"}
)
_ACTION_CUES_REQUEST_FIELDS = frozenset(
    {"contract_version", "session_id", "turn_id", "tool_call_id", "tool_name",
     "action_class", "artifact_keys", "deadline_ms"}
)


def _contract_version_field(value: Any, path: str, category: str = "invalid_request") -> None:
    if isinstance(value, bool) or value != CONTRACT_VERSION:
        _fail("unsupported_contract" if category == "invalid_request" else category,
              f"{path}.contract_version", "must be 1")


def _artifact_keys(value: Any, path: str, category: str = "invalid_request") -> None:
    items = _list(value, path, max_items=MAX_ARTIFACT_KEYS, category=category)
    for i, item in enumerate(items):
        ipath = f"{path}[{i}]"
        _obj(item, ipath, frozenset({"kind", "key"}), {"kind", "key"}, category)
        _enum(item["kind"], f"{ipath}.kind", ARTIFACT_KEY_KINDS, category)
        _str(item["key"], f"{ipath}.key", max_bytes=MAX_ARTIFACT_KEY_BYTES, min_bytes=1,
             category=category)


def validate_turn_context_request(req: Any) -> dict[str, Any]:
    _obj(req, "body", _TURN_CONTEXT_REQUEST_FIELDS, set(_TURN_CONTEXT_REQUEST_FIELDS))
    _contract_version_field(req["contract_version"], "body")
    _str(req["session_id"], "session_id", max_bytes=MAX_SESSION_ID_BYTES, min_bytes=1)
    _str(req["turn_id"], "turn_id", max_bytes=MAX_ID_BYTES, min_bytes=1)
    _int(req["turn"], "turn", minimum=0)
    _str(req["platform"], "platform", max_bytes=MAX_PLATFORM_BYTES)
    _str(req["chat_type"], "chat_type", max_bytes=MAX_CHAT_TYPE_BYTES)
    _str(req["sender_id"], "sender_id", max_bytes=MAX_SPEAKER_ID_BYTES)
    _str(req["agent_identity"], "agent_identity", max_bytes=MAX_SPEAKER_ID_BYTES)
    _str(req["agent_context"], "agent_context", max_bytes=MAX_AGENT_CONTEXT_BYTES)
    _str(req["parent_session_id"], "parent_session_id", max_bytes=MAX_SESSION_ID_BYTES)
    _str(req["message"], "message", max_bytes=MAX_TURN_MESSAGE_BYTES)
    turns = _list(req["recent_turns"], "recent_turns", max_items=MAX_RECENT_TURNS)
    for i, turn in enumerate(turns):
        tpath = f"recent_turns[{i}]"
        _obj(turn, tpath, frozenset({"user", "assistant"}), {"user", "assistant"})
        _str(turn["user"], f"{tpath}.user", max_bytes=MAX_RECENT_TURN_BYTES)
        _str(turn["assistant"], f"{tpath}.assistant", max_bytes=MAX_RECENT_TURN_BYTES)
    _handles(req["injected_handles"], "injected_handles")
    _handles(req["cited_handles"], "cited_handles")
    _int(req["deadline_ms"], "deadline_ms", minimum=1, maximum=MAX_DEADLINE_MS)
    return req


def validate_action_cues_request(req: Any) -> dict[str, Any]:
    _obj(req, "body", _ACTION_CUES_REQUEST_FIELDS, set(_ACTION_CUES_REQUEST_FIELDS))
    _contract_version_field(req["contract_version"], "body")
    _str(req["session_id"], "session_id", max_bytes=MAX_SESSION_ID_BYTES, min_bytes=1)
    _str(req["turn_id"], "turn_id", max_bytes=MAX_ID_BYTES, min_bytes=1)
    _str(req["tool_call_id"], "tool_call_id", max_bytes=MAX_ID_BYTES, min_bytes=1)
    _str(req["tool_name"], "tool_name", max_bytes=MAX_ID_BYTES, min_bytes=1)
    _enum(req["action_class"], "action_class", ACTION_CLASSES)
    _artifact_keys(req["artifact_keys"], "artifact_keys")
    _int(req["deadline_ms"], "deadline_ms", minimum=1, maximum=MAX_DEADLINE_MS)
    return req


# --------------------------------------------------------------------------
# Responses consumed by the plugin (category: invalid_response)
# --------------------------------------------------------------------------

_R = "invalid_response"

# Per-route allowlists of top-level response fields and their accepted types.
# Used by the client to shape responses before anything else looks at them.
RESPONSE_FIELDS: dict[str, dict[str, tuple[type, ...]]] = {
    "capabilities": {
        "contract_version": (int,), "provider": (str,), "server_commit": (str,),
        "limits": (dict,), "actions": (list,), "kinds": (list,), "tenant": (dict,),
    },
    "events": {"stored": (bool,), "event_id": (str,), "action": (str,), "job_id": (str,)},
    "turn_context": {
        "contract_version": (int,), "session_id": (str,), "turn": (int,), "block": (str,),
        "handles": (list,), "tail_handles": (list,), "brief_version": (int,),
        "latency_ms": (int, float), "empty_reason": (str,),
    },
    "action_cues": {
        "contract_version": (int,), "tool_call_id": (str,), "notes": (list,),
        "latency_ms": (int, float),
    },
    "rules": {"contract_version": (int,), "rules_version": (int,), "rules": (list,)},
    "search": {"contract_version": (int,), "results": (list,)},
    "expand": {
        "contract_version": (int,), "handle": (str,), "kind": (str,), "title": (str,),
        "abstract": (str,), "markdown": (str,),
    },
    "evidence": {"contract_version": (int,), "excerpts": (list,), "raw": (str,)},
    "propose": {
        "contract_version": (int,), "handle": (str,), "page_id": (str,), "status": (str,),
        "job_id": (str,),
    },
    "pinned": {
        "contract_version": (int,), "brief_version": (int,), "brief": (dict, type(None)),
        "pinned": (list,),
    },
    "upload": {"contract_version": (int,), "job_id": (str,), "version_id": (str,), "action": (str,)},
    "job_status": {
        "contract_version": (int,), "job_id": (str,), "kind": (str,), "status": (str,),
        "attempts": (int,), "created_at": (str,), "finished_at": (str, type(None)),
        "error_class": (str, type(None)), "result": (dict, type(None)),
    },
    "import_status": {
        "contract_version": (int,), "batch_id": (str,), "events_received": (int,),
        "sessions_seen": (int,), "sessions_completed": (int,), "versions_created": (int,),
        "extracted": (int,), "extraction_failed": (int,), "pending": (int,),
        "complete": (bool,), "last_event_at": (str, type(None)),
    },
}


def shape_response(route: str, value: Any) -> dict[str, Any]:
    """Keep only allowlisted top-level fields whose type matches; drop the rest."""
    fields = RESPONSE_FIELDS[route]
    if not isinstance(value, dict):
        raise ContractError(_R, f"{route}: expected object")
    shaped: dict[str, Any] = {}
    for key, types in fields.items():
        if key not in value:
            continue
        child = value[key]
        if isinstance(child, bool) and bool not in types:
            continue
        if isinstance(child, types):
            shaped[key] = child
    return shaped


def validate_capabilities(caps: Any) -> dict[str, Any]:
    """Validate ``GET /api/v1/capabilities``; ``unsupported_contract`` on version mismatch."""
    if not isinstance(caps, dict):
        raise ContractError(_R, "capabilities: expected object")
    version = caps.get("contract_version")
    if isinstance(version, bool) or version != CONTRACT_VERSION:
        raise ContractError("unsupported_contract", f"capabilities.contract_version: {version!r}")
    shaped = shape_response("capabilities", caps)
    for key in ("provider", "server_commit", "limits", "actions", "kinds", "tenant"):
        if key not in shaped:
            _fail(_R, f"capabilities.{key}", "missing")
    if shaped["provider"] != "substrate":
        _fail(_R, "capabilities.provider", "expected 'substrate'")
    _str(shaped["server_commit"], "capabilities.server_commit", max_bytes=64, category=_R)
    limits = shaped["limits"]
    for key in LIMITS:
        if key not in limits:
            _fail(_R, f"capabilities.limits.{key}", "missing")
        _int(limits[key], f"capabilities.limits.{key}", minimum=0, category=_R)
    actions = _list(shaped["actions"], "capabilities.actions", max_items=16, category=_R)
    for i, action in enumerate(actions):
        _str(action, f"capabilities.actions[{i}]", max_bytes=32, min_bytes=1, category=_R)
    if not ACTIONS.issubset(actions):
        _fail(_R, "capabilities.actions", "must include every known action")
    kinds = _list(shaped["kinds"], "capabilities.kinds", max_items=32, category=_R)
    for i, kind in enumerate(kinds):
        _str(kind, f"capabilities.kinds[{i}]", max_bytes=32, min_bytes=1, category=_R)
    if not PLUGIN_POSTABLE_KINDS.issubset(kinds):
        _fail(_R, "capabilities.kinds", "must include every plugin-postable kind")
    tenant = _obj(shaped["tenant"], "capabilities.tenant", frozenset({"tenant_id", "brief_version"}),
                  {"tenant_id", "brief_version"}, _R)
    _str(tenant["tenant_id"], "capabilities.tenant.tenant_id", max_bytes=MAX_ID_BYTES, min_bytes=1,
         category=_R)
    _int(tenant["brief_version"], "capabilities.tenant.brief_version", minimum=0, category=_R)
    return shaped


def validate_turn_context(resp: Any) -> dict[str, Any]:
    """Validate ``POST /memory/turn-context`` before anything enters the prompt."""
    shaped = shape_response("turn_context", resp)
    for key in RESPONSE_FIELDS["turn_context"]:
        if key not in shaped:
            _fail(_R, f"turn_context.{key}", "missing or wrong type")
    _contract_version_field(shaped["contract_version"], "turn_context", _R)
    _str(shaped["session_id"], "turn_context.session_id", max_bytes=MAX_SESSION_ID_BYTES,
         min_bytes=1, category=_R)
    _int(shaped["turn"], "turn_context.turn", minimum=0, category=_R)
    block = _str(shaped["block"], "turn_context.block", max_bytes=MAX_BLOCK_BYTES, category=_R)
    if block.count("\n") + (1 if block else 0) > MAX_BLOCK_LINES:
        _fail(_R, "turn_context.block", f"more than {MAX_BLOCK_LINES} lines")
    _handles(shaped["handles"], "turn_context.handles", _R)
    _handles(shaped["tail_handles"], "turn_context.tail_handles", _R)
    _int(shaped["brief_version"], "turn_context.brief_version", minimum=0, category=_R)
    _number(shaped["latency_ms"], "turn_context.latency_ms", category=_R)
    _enum(shaped["empty_reason"], "turn_context.empty_reason", EMPTY_REASONS, _R)
    return shaped


def validate_action_cues(resp: Any) -> dict[str, Any]:
    """Validate ``POST /memory/action-cues``; notes are bounded to 3 x 160 bytes."""
    shaped = shape_response("action_cues", resp)
    for key in RESPONSE_FIELDS["action_cues"]:
        if key not in shaped:
            _fail(_R, f"action_cues.{key}", "missing or wrong type")
    _contract_version_field(shaped["contract_version"], "action_cues", _R)
    _str(shaped["tool_call_id"], "action_cues.tool_call_id", max_bytes=MAX_ID_BYTES, min_bytes=1,
         category=_R)
    notes = _list(shaped["notes"], "action_cues.notes", max_items=MAX_NOTES, category=_R)
    for i, note in enumerate(notes):
        npath = f"action_cues.notes[{i}]"
        _obj(note, npath, frozenset({"handle", "text", "enforce"}), {"handle", "text", "enforce"}, _R)
        _match(note["handle"], f"{npath}.handle", HANDLE_RE, category=_R)
        text = _str(note["text"], f"{npath}.text", max_bytes=MAX_NOTE_TEXT_BYTES, min_bytes=1,
                    category=_R)
        if "\n" in text:
            _fail(_R, f"{npath}.text", "must be a single line")
        _bool(note["enforce"], f"{npath}.enforce", _R)
    _number(shaped["latency_ms"], "action_cues.latency_ms", category=_R)
    return shaped


def validate_rules(resp: Any) -> dict[str, Any]:
    """Validate ``GET /memory/rules``; every rule is enforceable by definition."""
    shaped = shape_response("rules", resp)
    for key in RESPONSE_FIELDS["rules"]:
        if key not in shaped:
            _fail(_R, f"rules.{key}", "missing or wrong type")
    _contract_version_field(shaped["contract_version"], "rules", _R)
    _int(shaped["rules_version"], "rules.rules_version", minimum=0, category=_R)
    rules = _list(shaped["rules"], "rules.rules", max_items=MAX_RULES, category=_R)
    for i, rule in enumerate(rules):
        rpath = f"rules.rules[{i}]"
        _obj(rule, rpath, frozenset({"handle", "text", "action_classes", "artifact_keys", "enforce"}),
             {"handle", "text", "action_classes", "artifact_keys", "enforce"}, _R)
        _match(rule["handle"], f"{rpath}.handle", HANDLE_RE, category=_R)
        text = _str(rule["text"], f"{rpath}.text", max_bytes=MAX_RULE_TEXT_BYTES, min_bytes=1,
                    category=_R)
        if "\n" in text:
            _fail(_R, f"{rpath}.text", "must be a single line")
        classes = _list(rule["action_classes"], f"{rpath}.action_classes",
                        max_items=len(ACTION_CLASSES), category=_R)
        for j, cls in enumerate(classes):
            _enum(cls, f"{rpath}.action_classes[{j}]", ACTION_CLASSES, _R)
        _artifact_keys(rule["artifact_keys"], f"{rpath}.artifact_keys", _R)
        if rule["enforce"] is not True:
            _fail(_R, f"{rpath}.enforce", "must be true")
    return shaped


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def fixture_path() -> Path:
    return _FIXTURE_PATH


def load_fixtures() -> dict[str, Any]:
    with _FIXTURE_PATH.open("rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def fixture_sha256() -> str:
    with _FIXTURE_PATH.open("rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


__all__ = [
    "ACTIONS", "ACTION_CLASSES", "ARTIFACT_KEY_KINDS", "BOUNDARIES", "CAPTURE_ORIGINS",
    "CLIENT_ERROR_CATEGORIES", "CONSENT_DECISIONS", "CONTRACT_VERSION", "ContractError",
    "DURABILITIES", "EMPTY_REASONS", "ERROR_CATEGORIES", "FIXTURE_SHA256", "HANDLE_RE",
    "JOB_STATUSES", "KINDS", "LIMITS", "MEMORY_WRITE_SOURCES", "MESSAGE_ROLES", "NAMESPACE",
    "PLUGIN_POSTABLE_KINDS", "RESPONSE_FIELDS", "SCHEMA_VERSION", "SPEAKER_ROLES", "UUID_RE",
    "ack_ok", "canonical_bytes", "canonical_json", "deterministic_event_id", "fixture_path",
    "fixture_sha256", "load_fixtures", "shape_response", "validate_action_cues",
    "validate_action_cues_request", "validate_capabilities", "validate_envelope",
    "validate_rules", "validate_turn_context", "validate_turn_context_request",
]
