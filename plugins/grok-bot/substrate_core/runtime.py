"""Substrate memory runtime for Grok Bot.

This module is a faithful port of the Hermes reference
``plugins/substrate/plugin.py``: identical tool handlers, redaction,
turn-context building, capture envelopes, worker semantics, onboarding
surfacing, and ``STATIC_MEMORY_PROMPT``. Only the ``register`` entry point
is generalized (duck-typed) so Grok Bot harnesses can attach it, and
Grok-friendly bridge aliases are appended at the end.
"""

from __future__ import annotations

import hashlib
import json
import math
import queue
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from . import contract
from . import onboarding
from .client import ClientError, SubstrateClient

STATIC_MEMORY_PROMPT = (
    "Substrate memory. Lines in `<memory-context>` are facts from the user's "
    "knowledge base, selected for this turn. Use them naturally and do not "
    "announce that you remembered. `[contested]` means sources disagree; call "
    "`memory_expand` before relying on it. `[as of DATE]` means it may have "
    "changed. Call `memory_evidence` when the user asks why you believe something. "
    "Call `memory_search` with your intended action before irreversible operations. "
    "Pinned pages follow."
)

TOOLSET = "substrate"
_TOOL_RESULT_BYTES = contract.LIMITS["max_tool_result_bytes"]
_TEXT_SECRET_RE = re.compile(
    r"(?i)(\b(?:authorization|api[_-]?key|access[_-]?token|token|password|secret)\b\s*[:=]\s*)"
    r"(?:bearer\s+)?[^\s,;]+"
)
_SK_RE = re.compile(r"\bsk_[A-Za-z0-9_-]{8,}\b")
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|private[_-]?key)"
)


def _clean_unicode(value: str) -> str:
    return value.encode("utf-8", "replace").decode("utf-8")


def _clip_utf8(value: str, maximum: int) -> str:
    value = _clean_unicode(value)
    raw = value.encode("utf-8")
    if len(raw) <= maximum:
        return value
    return raw[:maximum].decode("utf-8", "ignore")


def _redact_text(value: str) -> str:
    value = _clean_unicode(value)
    value = _TEXT_SECRET_RE.sub(r"\1[REDACTED]", value)
    return _SK_RE.sub("[REDACTED]", value)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return str(value)


def _bounded_text(value: Any, maximum: int) -> str:
    return _clip_utf8(_redact_text(_as_text(value)), maximum)


def _valid_handle(value: Any) -> bool:
    return isinstance(value, str) and contract.HANDLE_RE.fullmatch(value) is not None


def _handles(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value[: contract.MAX_HANDLES] if _valid_handle(item)]


def _recent_turns(history: Any) -> list[dict[str, str]]:
    """Extract the last two completed user/assistant pairs from Hermes history."""
    if not isinstance(history, list):
        return []
    pairs: list[dict[str, str]] = []
    pending: str | None = None
    answer: str | None = None
    for item in history:
        if not isinstance(item, Mapping):
            continue
        role = item.get("role")
        if role == "user":
            if pending is not None and answer is not None:
                pairs.append({"user": pending, "assistant": answer})
            pending = _bounded_text(item.get("content", ""), contract.MAX_RECENT_TURN_BYTES)
            answer = None
        elif role == "assistant" and pending is not None:
            # A tool-using turn can contain an assistant tool-call row followed
            # by the final assistant row. Keep the last assistant content.
            answer = _bounded_text(item.get("content", ""), contract.MAX_RECENT_TURN_BYTES)
    if pending is not None and answer is not None:
        pairs.append({"user": pending, "assistant": answer})
    return pairs[-contract.MAX_RECENT_TURNS :]


def _derive_turn(history: Any) -> int:
    if not isinstance(history, list):
        return 0
    # pre_llm_call receives history including the current, unanswered user row.
    users = sum(
        1 for item in history if isinstance(item, Mapping) and item.get("role") == "user"
    )
    last_role = next(
        (
            item.get("role")
            for item in reversed(history)
            if isinstance(item, Mapping) and item.get("role") in {"user", "assistant", "tool"}
        ),
        None,
    )
    return max(0, users - (1 if last_role == "user" else 0))


def _turn_context_request(
    session_id: Any,
    user_message: Any,
    conversation_history: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    explicit_turn = kwargs.get("turn")
    turn = (
        explicit_turn
        if isinstance(explicit_turn, int) and not isinstance(explicit_turn, bool) and explicit_turn >= 0
        else _derive_turn(conversation_history)
    )
    turn_id = _bounded_text(kwargs.get("turn_id", ""), contract.MAX_ID_BYTES)
    if not turn_id:
        # Current Hermes supplies turn_id. This fallback keeps older hosts valid.
        turn_id = str(uuid.uuid4())
    request = {
        "contract_version": contract.CONTRACT_VERSION,
        "session_id": _bounded_text(session_id, contract.MAX_SESSION_ID_BYTES),
        "turn_id": turn_id,
        "turn": min(turn, contract.MAX_SAFE_INTEGER),
        "platform": _bounded_text(kwargs.get("platform", ""), contract.MAX_PLATFORM_BYTES),
        "chat_type": _bounded_text(kwargs.get("chat_type", ""), contract.MAX_CHAT_TYPE_BYTES),
        "sender_id": _bounded_text(kwargs.get("sender_id", ""), contract.MAX_SPEAKER_ID_BYTES),
        "agent_identity": _bounded_text(
            kwargs.get("agent_identity", kwargs.get("agent_id", kwargs.get("model", ""))),
            contract.MAX_SPEAKER_ID_BYTES,
        ),
        "agent_context": _bounded_text(
            kwargs.get("agent_context", kwargs.get("profile_name", "")),
            contract.MAX_AGENT_CONTEXT_BYTES,
        ),
        "parent_session_id": _bounded_text(
            kwargs.get("parent_session_id", ""), contract.MAX_SESSION_ID_BYTES
        ),
        "message": _bounded_text(user_message, contract.MAX_TURN_MESSAGE_BYTES),
        "recent_turns": _recent_turns(conversation_history),
        "injected_handles": _handles(kwargs.get("injected_handles", [])),
        "cited_handles": _handles(kwargs.get("cited_handles", [])),
        "deadline_ms": contract.LIMITS["turn_context_deadline_ms"],
    }
    return contract.validate_turn_context_request(request)


def pre_llm_call(
    session_id: str = "",
    user_message: str = "",
    conversation_history: list[Any] | None = None,
    **kwargs: Any,
) -> dict[str, str] | None:
    """Fetch and validate per-turn memory. Every failure injects nothing."""
    try:
        request = _turn_context_request(
            session_id, user_message, conversation_history or [], **kwargs
        )
        response = SubstrateClient.from_env().post_json(
            "/api/v1/memory/turn-context", request, timeout=0.5
        )
        shaped = contract.shape_response("turn_context", response)
        checked = contract.validate_turn_context(shaped)
        # Bind the response to this request, rather than trusting valid data for
        # another session/turn.
        if checked["session_id"] != request["session_id"] or checked["turn"] != request["turn"]:
            return None
        block = checked["block"]
        return {"context": block} if block else None
    except ClientError as exc:
        if exc.category == "invalid_config":
            status = onboarding.ensure_started()
            if status and status.get("status") == "authorization_pending":
                return {"context": _onboarding_notice(status)}
        return None
    except Exception:
        return None


def _onboarding_notice(status: dict[str, Any]) -> str:
    link = str(status.get("verification_uri_complete", ""))
    code = str(status.get("user_code", ""))
    expires = int(status.get("expires_in", 0))
    name = str(status.get("agent_name", ""))
    name_line = (
        f"Agent name (editable on the approval page): {name}\n" if name else ""
    )
    return (
        "<substrate-connect>\n"
        "Substrate memory needs one-time browser approval. Show this link to the "
        "user and ask them to open it, sign in by email, and approve the "
        "connection:\n"
        f"{link}\n"
        f"One-time code: {code} (valid for {expires} seconds). After approval the "
        "key is stored privately for this profile and memory works automatically. "
        "Never ask the user to paste a key into chat.\n"
        f"{name_line}"
        "</substrate-connect>"
    )


# ---------------------------------------------------------------------------
# Turn capture
# ---------------------------------------------------------------------------


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    """Redact and bound arbitrary tool arguments into canonical JSON values."""
    if depth >= 8:
        return "[TRUNCATED]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value if abs(value) <= contract.MAX_SAFE_INTEGER else str(value)
    if isinstance(value, float):
        return str(value) if math.isfinite(value) else "[NONFINITE]"
    if isinstance(value, str):
        return _bounded_text(value, 8192)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in list(value.items())[:128]:
            name = _clip_utf8(str(key), 256)
            result[name] = "[REDACTED]" if _SENSITIVE_KEY_RE.search(name) else _safe_value(
                child, depth=depth + 1
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1) for item in value[:128]]
    return _bounded_text(value, 1024)


def _tool_call(item: Any, ordinal: int) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    call_id = _bounded_text(item.get("id", f"call-{ordinal}"), contract.MAX_ID_BYTES)
    function = item.get("function") if isinstance(item.get("function"), Mapping) else {}
    name = _bounded_text(
        item.get("tool_name", item.get("name", function.get("name", ""))),
        contract.MAX_ID_BYTES,
    )
    if not call_id or not name:
        return None
    raw_args = item.get("args", function.get("arguments", {}))
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            raw_args = {"value": raw_args}
    if not isinstance(raw_args, Mapping):
        raw_args = {"value": raw_args}
    args = _safe_value(raw_args)
    assert isinstance(args, dict)
    encoded = contract.canonical_bytes(args)
    if len(encoded) <= contract.LIMITS["max_tool_call_bytes"]:
        return {"id": call_id, "tool_name": name, "args": args}
    return {
        "id": call_id,
        "tool_name": name,
        "args_truncated": True,
        "args_sha256": hashlib.sha256(encoded).hexdigest(),
        "args_preview": _clip_utf8(encoded.decode("utf-8"), contract.MAX_ARGS_PREVIEW_BYTES),
    }


def _capture_message(item: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    role = item.get("role")
    if role not in contract.MESSAGE_ROLES:
        return None
    if role == "tool":
        full = _redact_text(_as_text(item.get("content", "")))
        raw = full.encode("utf-8")
        excerpt = _clip_utf8(full, contract.LIMITS["max_tool_result_bytes"])
        message: dict[str, Any] = {
            "index": index,
            "role": "tool",
            "content": excerpt,
            "result_digest": hashlib.sha256(raw).hexdigest(),
            "result_bytes": min(len(raw), contract.MAX_SAFE_INTEGER),
        }
        if len(excerpt.encode("utf-8")) != len(raw):
            message["result_truncated"] = True
        call_id = _bounded_text(
            item.get("tool_call_id", item.get("id", "")), contract.MAX_ID_BYTES
        )
        name = _bounded_text(item.get("tool_name", item.get("name", "")), contract.MAX_ID_BYTES)
        if call_id:
            message["tool_call_id"] = call_id
        if name:
            message["tool_name"] = name
    else:
        message = {
            "index": index,
            "role": role,
            "content": _bounded_text(item.get("content", ""), 32_768),
        }
        if role == "assistant" and isinstance(item.get("tool_calls"), list):
            calls = [
                call
                for n, raw_call in enumerate(item["tool_calls"][:32])
                if (call := _tool_call(raw_call, n)) is not None
            ]
            if calls:
                message["tool_calls"] = calls
    timestamp = item.get("timestamp")
    if isinstance(timestamp, str) and contract.RFC3339_RE.fullmatch(timestamp):
        message["timestamp"] = timestamp
    return message


def _completed_messages(
    history: Any, user_content: Any, assistant_content: Any
) -> list[dict[str, Any]]:
    raw_history = history if isinstance(history, list) else []
    messages = [
        message
        for index, item in enumerate(raw_history[-4096:])
        if (message := _capture_message(item, index)) is not None
    ]
    user = _bounded_text(user_content, 32_768)
    assistant = _bounded_text(assistant_content, 32_768)
    if not messages or messages[-1]["role"] != "assistant":
        if user and (not messages or messages[-1]["role"] != "user"):
            next_index = messages[-1]["index"] + 1 if messages else 0
            messages.append({"index": next_index, "role": "user", "content": user})
        if assistant:
            next_index = messages[-1]["index"] + 1 if messages else 0
            messages.append({"index": next_index, "role": "assistant", "content": assistant})
    elif assistant:
        # post_llm_call is after output transforms; capture what the user saw.
        messages[-1]["content"] = assistant
    return messages


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _capture_envelope(
    user_content: Any,
    assistant_content: Any,
    *,
    session_id: Any,
    messages: Any,
    **kwargs: Any,
) -> dict[str, Any] | None:
    clean_messages = _completed_messages(messages, user_content, assistant_content)
    if not clean_messages:
        return None
    event_id = str(uuid.uuid4())
    sid = _bounded_text(session_id, contract.MAX_SESSION_ID_BYTES)
    if not sid:
        return None
    turn_id = _bounded_text(kwargs.get("turn_id", ""), contract.MAX_ID_BYTES) or event_id
    sender_id = _bounded_text(kwargs.get("sender_id", ""), contract.MAX_SPEAKER_ID_BYTES) or "user"
    envelope: dict[str, Any] = {
        "schema_version": contract.SCHEMA_VERSION,
        "contract_version": contract.CONTRACT_VERSION,
        "event_id": event_id,
        "kind": "capture_turn",
        "session_id": sid,
        "offset": {"start": clean_messages[0]["index"], "end": clean_messages[-1]["index"] + 1},
        "capture_origin": "live",
        "batch_id": "",
        "speaker": {"id": sender_id, "role": "owner", "display": ""},
        "created_at": _utc_now(),
        "payload": {"turn_id": turn_id, "messages": clean_messages},
    }
    # The wire envelope is capped at 256 KiB. Retain the newest complete part
    # of unusually large histories, with original message indices as offsets.
    while len(contract.canonical_bytes(envelope)) > contract.LIMITS["max_event_bytes"] and len(clean_messages) > 1:
        clean_messages.pop(0)
        envelope["offset"]["start"] = clean_messages[0]["index"]
    if len(contract.canonical_bytes(envelope)) > contract.LIMITS["max_event_bytes"]:
        clean_messages[0]["content"] = _clip_utf8(clean_messages[0]["content"], 1024)
        if clean_messages[0]["role"] == "tool":
            # Digest/byte count still describe the bounded redacted source; only
            # the posted excerpt is reduced further to fit the envelope.
            clean_messages[0]["result_truncated"] = True
    contract.validate_envelope(envelope, idempotency_key=event_id)
    return envelope


class _CaptureWorker:
    """A single lazy daemon worker. Queueing never waits for network I/O."""

    def __init__(self) -> None:
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=64)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def enqueue(self, envelope: dict[str, Any]) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="substrate-capture", daemon=True
                )
                self._thread.start()
        try:
            self._queue.put_nowait(envelope)
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            envelope = self._queue.get()
            try:
                SubstrateClient.from_env().post_json(
                    "/api/v1/ledger/events",
                    envelope,
                    timeout=5.0,
                    idempotency_key=envelope["event_id"],
                    max_response_bytes=65_536,
                )
            except BaseException:
                pass
            finally:
                self._queue.task_done()


_CAPTURE_WORKER = _CaptureWorker()

# Sessions whose end has already been queued for materialization. Hermes fires
# on_session_end per turn, so boundary hooks must deduplicate; the set is
# bounded and process-local, and the server deduplicates by event id anyway.
_SENT_SESSIONS: dict[str, int] = {}
_SENT_SESSIONS_LOCK = threading.Lock()
_MAX_SENT_SESSIONS = 4096


def _session_envelope(
    session_id: Any,
    boundary: str,
    *,
    platform: Any = "",
    chat_type: Any = "",
    next_session_id: Any = "",
    parent_session_id: Any = "",
    high_water: int = 0,
) -> dict[str, Any] | None:
    """Build a validated capture_session envelope for a session boundary."""
    sid = _bounded_text(session_id, contract.MAX_SESSION_ID_BYTES)
    if not sid:
        return None
    event_id = str(uuid.uuid4())
    envelope: dict[str, Any] = {
        "schema_version": contract.SCHEMA_VERSION,
        "contract_version": contract.CONTRACT_VERSION,
        "event_id": event_id,
        "kind": "capture_session",
        "session_id": sid,
        "offset": {"start": 0, "end": max(0, high_water)},
        "capture_origin": "live",
        "batch_id": "",
        "speaker": {"id": "user", "role": "owner", "display": ""},
        "created_at": _utc_now(),
        "payload": {
            "boundary": boundary,
            "session_complete": True,
            "message_high_water": max(0, high_water),
            "platform": _bounded_text(platform, 32) or "cli",
            "chat_type": _bounded_text(chat_type, 32) or "direct",
        },
    }
    if next_session_id:
        envelope["payload"]["next_session_id"] = _bounded_text(next_session_id, contract.MAX_SESSION_ID_BYTES)
    if parent_session_id:
        envelope["payload"]["parent_session_id"] = _bounded_text(parent_session_id, contract.MAX_SESSION_ID_BYTES)
    try:
        contract.validate_envelope(envelope, idempotency_key=event_id)
    except contract.ContractError:
        return None
    return envelope


def end_session(
    session_id: Any = "",
    *,
    boundary: str = "end",
    platform: Any = "",
    chat_type: Any = "",
    next_session_id: Any = "",
    parent_session_id: Any = "",
    high_water: int = 0,
    **kwargs: Any,
) -> None:
    """Queue a live session-complete marker for server-side materialization.

    Idempotent per session: repeated boundary callbacks for one session are
    ignored. Every failure is swallowed; session capture never blocks the host.
    """
    try:
        sid = _bounded_text(session_id, contract.MAX_SESSION_ID_BYTES)
        if not sid or boundary not in contract.BOUNDARIES:
            return
        with _SENT_SESSIONS_LOCK:
            if sid in _SENT_SESSIONS:
                return
            if len(_SENT_SESSIONS) >= _MAX_SENT_SESSIONS:
                _SENT_SESSIONS.clear()
            _SENT_SESSIONS[sid] = 1
        envelope = _session_envelope(
            sid,
            boundary,
            platform=platform or kwargs.get("platform", ""),
            chat_type=chat_type or kwargs.get("chat_type", ""),
            next_session_id=next_session_id or kwargs.get("new_session_id", "")
            or kwargs.get("next_session_id", ""),
            parent_session_id=parent_session_id or kwargs.get("old_session_id", "")
            or kwargs.get("parent_session_id", ""),
            high_water=high_water or 0,
        )
        if envelope is not None:
            _CAPTURE_WORKER.enqueue(envelope)
    except Exception:
        pass


def sync_turn(
    user_content: Any = "",
    assistant_content: Any = "",
    *,
    session_id: str = "",
    messages: list[Any] | None = None,
    **kwargs: Any,
) -> None:
    """Snapshot a completed Hermes turn and enqueue it for background capture."""
    try:
        envelope = _capture_envelope(
            user_content,
            assistant_content,
            session_id=session_id,
            messages=messages or [],
            **kwargs,
        )
        if envelope is not None:
            _CAPTURE_WORKER.enqueue(envelope)
    except Exception:
        pass


def post_llm_call(
    session_id: str = "",
    user_message: str = "",
    assistant_response: str = "",
    conversation_history: list[Any] | None = None,
    **kwargs: Any,
) -> None:
    """Adapt Hermes's successful-turn hook to the directly testable callback."""
    sync_turn(
        user_message,
        assistant_response,
        session_id=session_id,
        messages=conversation_history or [],
        **kwargs,
    )


def on_session_reset(**kwargs: Any) -> None:
    """Hermes /new boundary: mark the previous session complete."""
    end_session(
        kwargs.get("old_session_id") or kwargs.get("session_id") or "",
        boundary="reset",
        platform=kwargs.get("platform", ""),
        next_session_id=kwargs.get("new_session_id", ""),
        parent_session_id=kwargs.get("old_session_id", ""),
    )


def on_session_finalize(**kwargs: Any) -> None:
    """CLI shutdown or gateway session-expiry boundary."""
    end_session(
        kwargs.get("session_id") or "",
        boundary="end",
        platform=kwargs.get("platform", ""),
    )


# ---------------------------------------------------------------------------
# Retrieval tools
# ---------------------------------------------------------------------------


def _error(category: str) -> str:
    allowed = {"invalid_request", "invalid_response", "invalid_config", "timeout", "transport_error"}
    return json.dumps(
        {"error": category if category in allowed else "transport_error"},
        separators=(",", ":"),
    )


def _strict_args(args: Any, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(args, dict) or any(not isinstance(key, str) for key in args):
        raise ValueError
    if set(args) - allowed or not required.issubset(args):
        raise ValueError
    return args


def _limit(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
        raise ValueError
    return value


def _compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fit_result(value: dict[str, Any]) -> str:
    """Keep tool output valid JSON and below Hermes's captured-result bound."""
    text = _compact(value)
    if len(text.encode("utf-8")) <= _TOOL_RESULT_BYTES:
        return text
    # Shorten bulky document fields before dropping discrete search/evidence
    # items, which are usually more useful than the raw tail.
    for key in ("markdown", "raw", "abstract", "text"):
        child = value.get(key)
        if isinstance(child, str) and len(_compact(value).encode("utf-8")) > _TOOL_RESULT_BYTES:
            value[key] = _clip_utf8(child, max(0, _TOOL_RESULT_BYTES // 2))
    for key in ("results", "excerpts"):
        child = value.get(key)
        if isinstance(child, list):
            while child and len(_compact(value).encode("utf-8")) > _TOOL_RESULT_BYTES:
                child.pop()
    text = _compact(value)
    if len(text.encode("utf-8")) > _TOOL_RESULT_BYTES:
        return _error("invalid_response")
    return text


def _call_tool(route: str, path: str, request: dict[str, Any], shape: Any) -> str:
    try:
        if not SubstrateClient.from_env().api_key:
            status = onboarding.ensure_started()
            if status and status.get("status") == "authorization_pending":
                return _onboarding_result(status)
        response = SubstrateClient.from_env().post_json(path, request, timeout=3.0)
        shaped = contract.shape_response(route, response)
        return _fit_result(shape(shaped))
    except contract.ContractError:
        return _error("invalid_response")
    except ValueError:
        return _error("invalid_request")
    except ClientError as exc:
        return _error(exc.category)
    except Exception:
        return _error("transport_error")


def _onboarding_result(status: dict[str, Any]) -> str:
    link = str(status.get("verification_uri_complete", ""))
    code = str(status.get("user_code", ""))
    expires = int(status.get("expires_in", 0))
    name = str(status.get("agent_name", ""))
    return json.dumps(
        {
            "status": "authorization_required",
            "message": (
                "Substrate memory is not connected yet. Open the link below in a "
                "browser, sign in by email, and approve the connection to grant "
                "this Hermes profile a tenant-scoped memory key. The key is "
                "stored privately in this profile and never needs to be pasted "
                "into chat. After approving, call memory_search again; "
                "authorization completes automatically."
                + (f" This agent will be named '{name}' (editable on the approval page)." if name else "")
            ),
            "verification_uri_complete": link,
            "user_code": code,
            "expires_in": expires,
            "agent_name": name,
        },
        sort_keys=True,
    )


def _shape_search(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("contract_version") != contract.CONTRACT_VERSION or not isinstance(value.get("results"), list):
        raise contract.ContractError("invalid_response")
    results: list[dict[str, Any]] = []
    for item in value["results"][:20]:
        if not isinstance(item, Mapping) or not _valid_handle(item.get("handle")):
            continue
        result: dict[str, Any] = {"handle": item["handle"]}
        if isinstance(item.get("text"), str):
            result["text"] = _bounded_text(item["text"], 4096)
        score = item.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
            result["score"] = score
        if isinstance(item.get("kind"), str):
            result["kind"] = _clip_utf8(item["kind"], 32)
        if isinstance(item.get("markers"), list):
            result["markers"] = [
                _bounded_text(marker, 128) for marker in item["markers"][:16] if isinstance(marker, str)
            ]
        results.append(result)
    return {"contract_version": contract.CONTRACT_VERSION, "results": results}


def memory_search(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        args = _strict_args(args, {"query", "kinds", "limit"}, {"query"})
        query = args["query"]
        if not isinstance(query, str) or not query or len(query.encode("utf-8")) > contract.MAX_QUERY_BYTES:
            raise ValueError
        request: dict[str, Any] = {"query": query, "limit": _limit(args.get("limit"), 8)}
        if "kinds" in args:
            kinds = args["kinds"]
            if not isinstance(kinds, list) or len(kinds) > contract.MAX_SEARCH_KINDS:
                raise ValueError
            if any(not isinstance(kind, str) or not kind or len(kind.encode("utf-8")) > 32 for kind in kinds):
                raise ValueError
            request["kinds"] = kinds
    except Exception:
        return _error("invalid_request")
    return _call_tool("search", "/api/v1/memory/search", request, _shape_search)


def _shape_expand(value: dict[str, Any], expected: str) -> dict[str, Any]:
    if value.get("contract_version") != contract.CONTRACT_VERSION or value.get("handle") != expected:
        raise contract.ContractError("invalid_response")
    if not isinstance(value.get("kind"), str):
        raise contract.ContractError("invalid_response")
    result: dict[str, Any] = {
        "contract_version": contract.CONTRACT_VERSION,
        "handle": expected,
        "kind": _clip_utf8(value["kind"], 32),
    }
    for key, maximum in (("title", 200), ("abstract", 4096), ("markdown", contract.MAX_MARKDOWN_BYTES)):
        if key in value:
            if not isinstance(value[key], str):
                raise contract.ContractError("invalid_response")
            result[key] = _bounded_text(value[key], maximum)
    return result


def memory_expand(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        args = _strict_args(args, {"handle"}, {"handle"})
        handle = args["handle"]
        if not _valid_handle(handle):
            raise ValueError
    except Exception:
        return _error("invalid_request")
    return _call_tool(
        "expand",
        "/api/v1/memory/expand",
        {"handle": handle},
        lambda value: _shape_expand(value, handle),
    )


def _bounded_excerpt(value: Any) -> Any:
    clean = _safe_value(value)
    if isinstance(clean, dict):
        return {str(key): child for key, child in list(clean.items())[:32]}
    return clean


def _shape_evidence(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("contract_version") != contract.CONTRACT_VERSION or not isinstance(value.get("excerpts"), list):
        raise contract.ContractError("invalid_response")
    result: dict[str, Any] = {
        "contract_version": contract.CONTRACT_VERSION,
        "excerpts": [_bounded_excerpt(item) for item in value["excerpts"][:20]],
    }
    if "raw" in value:
        if not isinstance(value["raw"], str):
            raise contract.ContractError("invalid_response")
        result["raw"] = _bounded_text(value["raw"], contract.MAX_MARKDOWN_BYTES)
    return result


def memory_evidence(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        args = _strict_args(args, {"handle", "raw", "limit"}, {"handle"})
        handle = args["handle"]
        if not _valid_handle(handle):
            raise ValueError
        raw = args.get("raw", False)
        if not isinstance(raw, bool):
            raise ValueError
        request = {"handle": handle, "raw": raw, "limit": _limit(args.get("limit"), 5)}
    except Exception:
        return _error("invalid_request")
    return _call_tool("evidence", "/api/v1/memory/evidence", request, _shape_evidence)


MEMORY_SEARCH_SCHEMA = {
    "name": "memory_search",
    "description": "Search Substrate memory. Use the intended action in the query before irreversible operations.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to recall or the intended action."},
            "kinds": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}
MEMORY_EXPAND_SCHEMA = {
    "name": "memory_expand",
    "description": "Expand a Substrate memory or page handle into its bounded detail.",
    "parameters": {
        "type": "object",
        "properties": {"handle": {"type": "string", "pattern": contract.HANDLE_RE.pattern}},
        "required": ["handle"],
        "additionalProperties": False,
    },
}
MEMORY_EVIDENCE_SCHEMA = {
    "name": "memory_evidence",
    "description": "Get evidence excerpts for a Substrate memory handle.",
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {"type": "string", "pattern": contract.HANDLE_RE.pattern},
            "raw": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        },
        "required": ["handle"],
        "additionalProperties": False,
    },
}


def register(ctx: Any) -> None:
    """Register the plugin surface without I/O or worker startup.

    The context is duck-typed: hooks/tools/prompt sections are registered
    only when the host context exposes the matching method, so the same
    runtime works on Hermes and on Grok Bot harnesses (see bridge.py).
    """
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("pre_llm_call", pre_llm_call)
        # post_llm_call receives the finalized full conversation in
        # conversation_history, plus user_message and assistant_response.
        register_hook("post_llm_call", post_llm_call)
        # Session boundaries feed live-session materialization on the server.
        register_hook("on_session_reset", on_session_reset)
        register_hook("on_session_finalize", on_session_finalize)
    register_tool = getattr(ctx, "register_tool", None)
    if callable(register_tool):
        register_tool(
            name="memory_search", toolset=TOOLSET, schema=MEMORY_SEARCH_SCHEMA, handler=memory_search
        )
        register_tool(
            name="memory_expand", toolset=TOOLSET, schema=MEMORY_EXPAND_SCHEMA, handler=memory_expand
        )
        register_tool(
            name="memory_evidence", toolset=TOOLSET, schema=MEMORY_EVIDENCE_SCHEMA, handler=memory_evidence
        )
    register_prompt = getattr(ctx, "register_system_prompt_section", None)
    if callable(register_prompt):
        # This static section is frozen per session; dynamic memory stays in the
        # supported pre_llm_call injection path.
        register_prompt(
            "substrate.memory", STATIC_MEMORY_PROMPT, position="after_memory", max_chars=2000
        )


# ---------------------------------------------------------------------------
# Grok Bot bridge aliases (same functions, host-neutral names)
# ---------------------------------------------------------------------------

# Pre-turn bounded memory context. Returns the context string, or None when
# memory is unavailable (fail closed). Grok Bot harnesses without a hook
# system call this before each model request and prepend the result.
get_memory_context = pre_llm_call

# Completed-turn capture (nonblocking) and session boundary markers.
capture_completed_turn = post_llm_call
capture_session_end = on_session_finalize
capture_session_reset = on_session_reset
