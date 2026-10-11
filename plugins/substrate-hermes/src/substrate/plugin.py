"""Native Hermes retrieval plugin for Substrate.

``register`` only registers callbacks. Configuration and network access are
resolved lazily when a callback actually runs.

Durable delivery goes through the write-ahead spool in
:mod:`substrate.spool` (owned by the spool worker). Capture events enqueue
at ``PRIORITY_LIVE``; explicit ledger operations (``memory_remember``,
``memory_forget``) enqueue at ``PRIORITY_EXPLICIT`` and additionally POST
synchronously under the same ``event_id`` so the tool can return the
server's handle. Nothing is fire-and-forget.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import contract
from .client import ClientError, SubstrateClient
from .spool import (
    PRIORITY_EXPLICIT,
    PRIORITY_LIVE,
    SpoolFull,
    get_spool,
    is_transient_category,
)

STATIC_MEMORY_PROMPT = (
    "Substrate memory. Lines in `<memory-context>` are facts from the user's "
    "knowledge base, selected for this turn. Use them naturally and do not "
    "announce that you remembered. `[contested]` means sources disagree; call "
    "`memory_expand` before relying on it. `[as of DATE]` means it may have "
    "changed. Call `memory_evidence` when the user asks why you believe something. "
    "Call `memory_search` with your intended action before irreversible operations. "
    "Importing past conversations or sessions is the substrate_import tool, never "
    "memory_remember. If substrate_import cannot run, say so; do not claim an import. "
    "A result that says queued is saved and syncs by itself; do not retry it. "
    "Pinned pages follow."
)

TOOLSET = "substrate"
# Absolute path of the bundled login CLI, so instructions never say "<plugin-dir>".
ONBOARD_CLI = str(Path(__file__).resolve().parents[2] / "onboard.py")
ISSUES_URL = "https://github.com/Substrate-memory/Substrate-memory-plugins/issues"
# Soft version policy (COMPATIBILITY.md): install and run on any host version.
# The tested range is open-ended (this release and newer); only an older host
# gets a one-time note, and nothing is ever refused.
TESTED_HERMES_FLOOR = (0, 21, 0)
TESTED_HERMES_RANGE = "0.21.0 or newer"
_version_notice_shown = False
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


def _turn_context_args(
    session_id: Any,
    user_message: Any,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Build ``memory_turn_context`` MCP arguments, or None when unusable.

    The v2 contract takes only ``session_id``, ``prompt`` (<=16384 bytes),
    ``platform``, and optional ``turn_id``/``agent_context``/
    ``parent_session_id``. The server redacts the prompt, opens the pending
    turn with it, and numbers turns itself, so no history, turn number, or
    identity fields are sent.
    """
    sid = _bounded_text(session_id, contract.MAX_SESSION_ID_BYTES)
    if not sid:
        return None
    args: dict[str, Any] = {
        "session_id": sid,
        "prompt": _bounded_text(user_message, contract.MAX_TURN_MESSAGE_BYTES),
        "platform": _bounded_text(kwargs.get("platform", ""), contract.MAX_PLATFORM_BYTES)
        or "hermes",
    }
    turn_id = _bounded_text(kwargs.get("turn_id", ""), contract.MAX_ID_BYTES)
    if turn_id:
        args["turn_id"] = turn_id
    agent_context = _bounded_text(
        kwargs.get("agent_context", ""), contract.MAX_AGENT_CONTEXT_BYTES
    )
    if agent_context in {"main", "subagent"}:
        args["agent_context"] = agent_context
    parent = _bounded_text(kwargs.get("parent_session_id", ""), contract.MAX_SESSION_ID_BYTES)
    if parent:
        args["parent_session_id"] = parent
    return args


# Per-turn recall budget. 0.5 s was below one authenticated round trip to the
# hosted server (TLS + auth + recall), so the block was almost always dropped;
# a chat turn's own LLM call takes seconds, so 2 s still adds no felt delay.
TURN_CONTEXT_TIMEOUT = 2.0


def pre_llm_call(
    session_id: str = "",
    user_message: str = "",
    conversation_history: list[Any] | None = None,
    **kwargs: Any,
) -> dict[str, str] | None:
    """Fetch and validate per-turn memory. Every failure injects nothing.

    Calls the ``memory_turn_context`` MCP tool and injects only its ``block``.
    The ``[substrate] ... not saved yet`` sync line the contract appends for
    MCP hosts is intentionally ignored here: the Hermes write-ahead spool
    guarantees delivery of every captured turn, so there are no missing turns
    to sync (see CONTRACT.md section 13).

    Without a key, the turn starts (or resumes) the browser sign-in itself and
    injects the approval link and code for the agent to show in chat. The
    first turn after approval carries a one-time "connected as" note.
    """
    notes = [n for n in (_host_version_notice_once(),) if n]
    _note_live_session(session_id)
    client = None
    try:
        client = SubstrateClient.from_env()
        if not client.api_key:
            status = _start_login()
            if status is not None:
                return _with_notes([_login_notice(status)], notes)
            client = SubstrateClient.from_env()  # approved just now
            if not client.api_key:
                return _with_notes([_login_notice(None)], notes)
        announcement = _connected_announcement()
        if announcement:
            notes.append(announcement)
        args = _turn_context_args(session_id, user_message, **kwargs)
        if args is None:
            return _with_notes(_import_notes(client), notes)
        structured, _text = client.call_tool("memory_turn_context", args, timeout=TURN_CONTEXT_TIMEOUT)
        checked = contract.validate_mcp_turn_context(structured)
        # Bind the response to this request, rather than trusting valid data
        # for another session.
        if checked["session_id"] != args["session_id"]:
            return _with_notes(_import_notes(client), notes)
        block = checked["block"]
        return _with_notes([block] if block else [], notes + _import_notes(client))
    except ClientError as exc:
        if exc.category == "invalid_config":
            return _with_notes([_login_notice(_start_login())], notes)
        if exc.category in ("unauthorized", "forbidden"):
            return _with_notes([], notes)
        return _with_notes(_import_notes(client), notes)
    except Exception:
        return _with_notes(_import_notes(client), notes)


def _with_notes(parts: list[str], notes: list[str]) -> dict[str, str] | None:
    text = "\n".join(part for part in [*notes, *parts] if part)
    return {"context": text} if text else None


def running_hermes_version() -> str:
    try:
        from hermes_cli import __version__ as version  # type: ignore[import-not-found]
    except Exception:
        return ""
    return str(version or "")


def _version_tuple(version: str) -> tuple[int, ...] | None:
    match = re.match(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?", version or "")
    if match is None:
        return None
    return tuple(int(part or 0) for part in match.groups())


def host_version_notice(version: str | None = None) -> str:
    """Friendly note only when the host is older than the tested floor; never blocks.

    Newer and unrecognized versions get no note: new Hermes releases are
    expected to work, and nagging about them reads as a restriction.
    """
    version = running_hermes_version() if version is None else version
    parsed = _version_tuple(version)
    if parsed is None or parsed >= TESTED_HERMES_FLOOR:
        return ""
    return (
        f"Substrate memory is tested on Hermes {TESTED_HERMES_RANGE}; "
        f"this host runs Hermes {version}. It should work. If something does not, "
        f"tell us at {ISSUES_URL}."
    )


def _host_version_notice_once() -> str:
    global _version_notice_shown
    if _version_notice_shown:
        return ""
    _version_notice_shown = True
    notice = host_version_notice()
    return f"<substrate-notice>\nTell the user once: {notice}\n</substrate-notice>" if notice else ""


def _start_login() -> dict[str, Any] | None:
    try:
        from . import onboarding as _onboarding

        return _onboarding.ensure_started()
    except Exception:
        return None


def _connected_announcement() -> str:
    try:
        from . import onboarding as _onboarding

        message = _onboarding.take_connected_announcement()
    except Exception:
        return ""
    if not message:
        return ""
    return (
        "<substrate-connect>\n"
        f"Substrate memory was just connected. Tell the user once, in these words: {message}\n"
        "</substrate-connect>"
    )


# ---------------------------------------------------------------------------
# Past-conversation import: the one-time offer and the substrate_import tool
# ---------------------------------------------------------------------------

_LIVE_SESSIONS: dict[str, float] = {}
_MAX_LIVE_SESSIONS = 256
_PREVIEW_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
PREVIEW_CACHE_SECONDS = 300.0
_RESUME_CHECKED: dict[str, float] = {}
RESUME_CHECK_SECONDS = 60.0


def _note_live_session(session_id: Any) -> None:
    """Sessions seen live in this process are captured live: never import them."""
    sid = _bounded_text(session_id, contract.MAX_SESSION_ID_BYTES)
    if not sid:
        return
    with _SESSION_LOCK:
        _LIVE_SESSIONS[sid] = time.time()
        while len(_LIVE_SESSIONS) > _MAX_LIVE_SESSIONS:
            _LIVE_SESSIONS.pop(next(iter(_LIVE_SESSIONS)))


def _live_sessions() -> set[str]:
    with _SESSION_LOCK:
        live = set(_LIVE_SESSIONS)
        if _SESSION_STATE["active"]:
            live.add(_SESSION_STATE["active"])
    return live


def _home() -> Path:
    from . import credentials as _credentials

    return _credentials.active_home()


def _cached_preview(home: Path, *, fresh: bool = False) -> dict[str, Any]:
    from . import history

    key = str(home)
    cached = _PREVIEW_CACHE.get(key)
    if not fresh and cached is not None and time.monotonic() - cached[0] < PREVIEW_CACHE_SECONDS:
        return cached[1]
    view = history.preview(home, exclude=_live_sessions())
    _PREVIEW_CACHE[key] = (time.monotonic(), view)
    return view


def import_offer_notice(view: dict[str, Any]) -> str:
    """Injected each turn until the user answers the one-time import offer."""
    from . import history

    return (
        "<substrate-import-offer>\n"
        f"Substrate memory is connected. This Hermes profile also has "
        f"{history.describe_preview(view)} that are not in Substrate memory yet. "
        "In this same reply (after the connected line or your answer), ask the user once, "
        f"in plain words, for example: \"{history.offer_question(view)}\"\n"
        "When they answer: Import all -> call substrate_import with action \"start\", then "
        "tell them it runs in the background and you will say when it is done. "
        "Let me pick -> call substrate_import with action \"preview\" and limit 20, show "
        "the list, then call action \"start\" with the session_ids they choose. "
        "Not now -> call substrate_import with action \"decline\" and do not ask again "
        "(they can ask you to import past conversations any time).\n"
        "If you already asked in this conversation, do not ask again; act on their answer.\n"
        "</substrate-import-offer>"
    )


def _import_notes(client: Any) -> list[str]:
    """Import offer / finished line / auto-resume for a connected turn. Never raises."""
    try:
        from . import history

        home = _home()
        notes: list[str] = []
        finished = history.take_finished_announcement(home)
        if finished:
            notes.append(
                "<substrate-import>\nThe import of past conversations finished. Tell the "
                f"user once, in plain words: {finished}\n</substrate-import>"
            )
        decision = history.read_decision(home)
        if decision in ("yes", "picked"):
            key = str(home)
            if time.monotonic() - _RESUME_CHECKED.get(key, -RESUME_CHECK_SECONDS) >= RESUME_CHECK_SECONDS:
                _RESUME_CHECKED[key] = time.monotonic()
                history.resume_if_interrupted(home, client=client, exclude=_live_sessions())
            return notes
        if decision or history.was_asked(home):
            # Answered, or already put to the agent once for this connection.
            # The user can still ask for the import directly (substrate_import).
            return notes
        try:
            view = _cached_preview(home)
        except history.HistoryError:
            return notes  # unreadable right now: ask on a later turn
        if not view.get("sessions"):
            history.record_decision(home, "none")
            return notes
        history.record_asked(home)
        notes.append(import_offer_notice(view))
        return notes
    except Exception:
        return []


def _import_result(value: dict[str, Any]) -> str:
    return _compact(value)


def substrate_import(args: dict[str, Any], **kwargs: Any) -> str:
    """Preview, start, check, or decline the import of past Hermes conversations."""
    from . import history

    try:
        args = _strict_args(args, {"action", "session_ids", "limit"}, {"action"})
        action = args["action"]
        if action not in ("preview", "start", "status", "decline"):
            raise ValueError
        picked = args.get("session_ids")
        if picked is not None:
            if (not isinstance(picked, list) or not picked or len(picked) > history.MAX_PICKED
                    or any(not isinstance(item, str) or not item for item in picked)):
                raise ValueError
        limit = args.get("limit", 0)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 100:
            raise ValueError
    except Exception:
        return _error("invalid_request")
    try:
        home = _home()
        if action == "status":
            return _import_result(history.status(home))
        if action == "decline":
            history.record_decision(home, "no")
            return _import_result({
                "status": "declined",
                "message": "OK, past conversations stay out of Substrate memory. The user can "
                           "ask to import them any time.",
            })
        exclude = _live_sessions()
        view = history.preview(home, exclude=exclude, limit=limit,
                               picked=set(picked) if picked else None)
        if action == "preview":
            view["question"] = history.offer_question(view) if view["sessions"] else ""
            return _import_result(view)
        client = SubstrateClient.from_env()
        if not client.api_key:
            return _login_grant_result()
        if not view["sessions"]:
            if not picked:
                history.record_decision(home, "none")
            return _import_result({
                "status": "nothing_to_import",
                "message": "There are no earlier conversations in this Hermes profile to import "
                           "(automated runs, group chats and this conversation are left out).",
            })
        history.record_decision(home, "picked" if picked else "yes")
        if not history.start_background(home, client=client, exclude=exclude, picked=picked):
            return _import_result({
                **history.status(home), "status": "already_running",
                "message": "An import is already running in the background. Ask for the "
                           "status any time; the result is reported when it finishes.",
            })
        return _import_result({
            "status": "started",
            "sessions": view["sessions"],
            "messages": view["messages"],
            "message": (
                f"Importing {history.describe_preview(view)} in the background. Live messages "
                "are saved first. Ask for the status any time; the result is reported when "
                "it finishes."
            ),
        })
    except history.HistoryError as exc:
        return _import_result({
            "status": "failed", "error": exc.category,
            "message": "Could not read this profile's Hermes history (state.db). Nothing was "
                       "changed; try again later.",
        })
    except Exception:
        return _error("transport_error")


SUBSTRATE_IMPORT_SCHEMA = {
    "name": "substrate_import",
    "description": (
        "Import the user's past Hermes conversations, chats and sessions (earlier history) "
        "from this profile into Substrate memory. Use this tool for any request to import, "
        "bring in, add or sync earlier or past conversations, including when the user asks "
        "directly. Never use memory_remember for that: it stores one fact per call and "
        "cannot import sessions. Start only after the user agrees. preview: counts, dates, "
        "and with limit a list of sessions to pick from. start: import all eligible "
        "sessions, or only session_ids, in the background. status: progress or result. "
        "decline: the user said not now (never ask again). If the result is failed, "
        "nothing_to_import or authorization_required, say so plainly; never say "
        "conversations were imported unless status reports completed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["preview", "start", "status", "decline"]},
            "session_ids": {
                "type": "array", "items": {"type": "string"}, "maxItems": 500,
                "description": "Only these sessions (from preview). Omit to import all.",
            },
            "limit": {"type": "integer", "minimum": 0, "maximum": 100,
                      "description": "preview: how many recent sessions to list."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _cli_hint() -> str:
    return (
        f"To connect by hand, run: python {ONBOARD_CLI} start "
        f"(then python {ONBOARD_CLI} poll to wait for approval)."
    )


def _login_notice(status: dict[str, Any] | None = None) -> str:
    """Missing-credential instruction: show the link and code, never ask for a key."""
    if isinstance(status, dict) and status.get("status") == "finishing":
        return (
            "<substrate-connect>\n"
            f"{status.get('message', '')} Tell the user this in plain words. Do not show "
            "a link, do not start a new sign-in, and do not run onboard.py start.\n"
            "</substrate-connect>"
        )
    if isinstance(status, dict) and status.get("status") == "authorization_pending":
        minutes = max(1, (int(status.get("expires_in") or 0) + 59) // 60)
        lines = [
            "<substrate-connect>",
            "Substrate memory is installed but not connected yet. In your reply, show the "
            "user this approval link and code exactly as written, and ask them to open the "
            "link, sign in, check the code, and choose Approve connection:",
            f"Link: {status.get('verification_uri_complete', '')}",
            f"Code: {status.get('user_code', '')} (valid for {minutes} minutes)",
            "The plugin checked with Substrate just before this message: this code is not "
            "approved yet. If the user says they already approved it, ask them to reopen "
            "the link and confirm the page ends at Connection approved. Do not start a new "
            "sign-in or run onboard.py start while this code is valid. The plugin finishes "
            "by itself after approval; a later turn confirms the account. Never ask for or "
            "accept an API key.",
        ]
        if status.get("previous"):
            lines.append(f"Also tell the user: {status['previous']}")
        if status.get("notice"):
            lines.append(f"Also tell the user: {status['notice']}")
        lines.append("</substrate-connect>")
        return "\n".join(lines)
    if isinstance(status, dict) and status.get("message"):
        return (
            "<substrate-connect>\n"
            "Substrate memory is installed but could not start the sign-in. Tell the user, "
            f"in plain words: {status['message']} (code: {status.get('error_class', '')}). "
            "The plugin retries automatically on a later turn. "
            f"{_cli_hint()} Never ask for or accept an API key.\n"
            "</substrate-connect>"
        )
    return (
        "<substrate-connect>\n"
        "Substrate memory is not connected for this profile. "
        f"{_cli_hint()} Show the user the printed link and code; they open the link, "
        "sign in, and choose Approve connection. Never paste an API key into chat.\n"
        "</substrate-connect>"
    )


def _login_required_result() -> str:
    """Missing-credential tool result: instruct the agent CLI login."""
    return _compact(
        {
            "status": "authorization_required",
            "message": (
                "Substrate memory is not connected for this profile. "
                f"{_cli_hint()} Show the user the printed link and code, ask them to "
                "approve it in a browser, then retry this call. The key is stored "
                "privately in this profile. Never paste an API key into chat."
            ),
        }
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
            "content": _bounded_text(item.get("content", ""), contract.MAX_PAYLOAD_STRING_BYTES),
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
    # Session-absolute indices: the window is the newest 4096 messages, but the
    # index of a message never changes when older ones fall out of it.
    first = max(0, len(raw_history) - 4096)
    messages = [
        message
        for index, item in enumerate(raw_history[first:], first)
        if (message := _capture_message(item, index)) is not None
    ]
    user = _bounded_text(user_content, contract.MAX_PAYLOAD_STRING_BYTES)
    assistant = _bounded_text(assistant_content, contract.MAX_PAYLOAD_STRING_BYTES)
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


def _message_fingerprint(message: Mapping[str, Any]) -> str:
    """Identity of the last message sent for a session: role plus leading content."""
    head = str(message.get("content", "")).encode("utf-8")[:512]
    return hashlib.sha256(str(message.get("role", "")).encode() + b"\0" + head).hexdigest()


def _send_from(session_id: str, clean_messages: list[dict[str, Any]]) -> int:
    """Index to send from: just past the last message already enqueued for this session.

    Falls back to the whole window (the server dedupes by event id) when this
    process has no record for the session, or when the message at the
    recorded position is not the one that was sent (a rewound, edited or
    compressed history).
    """
    first = clean_messages[0]["index"]
    with _SESSION_LOCK:
        state = _SESSION_STATE["sent_tail"].get(session_id)
    if state is None:
        return first
    end, fingerprint = state
    if end <= first:
        return first
    previous = next((m for m in clean_messages if m["index"] == end - 1), None)
    if previous is None or _message_fingerprint(previous) != fingerprint:
        return first
    return end


def _capture_envelopes(
    user_content: Any,
    assistant_content: Any,
    *,
    session_id: Any,
    messages: Any,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Capture envelopes for one completed turn: only new messages, chunked to fit.

    Each turn sends the messages since the last one enqueued for the session
    (not the whole history again), split into parts of at most 128 messages and
    the envelope byte budget, with contiguous offsets. Returns [] when there is
    nothing new or no session.
    """
    clean_messages = _completed_messages(messages, user_content, assistant_content)
    if not clean_messages:
        return []
    sid = _bounded_text(session_id, contract.MAX_SESSION_ID_BYTES)
    if not sid:
        return []
    send_from = _send_from(sid, clean_messages)
    pending = [message for message in clean_messages if message["index"] >= send_from]
    if not pending:
        return []
    turn_id = _bounded_text(kwargs.get("turn_id", ""), contract.MAX_ID_BYTES) or str(uuid.uuid4())
    sender_id = _bounded_text(kwargs.get("sender_id", ""), contract.MAX_SPEAKER_ID_BYTES) or "user"
    envelope: dict[str, Any] = {
        "schema_version": contract.SCHEMA_VERSION,
        "contract_version": contract.CONTRACT_VERSION,
        "event_id": str(uuid.uuid4()),
        "kind": "capture_turn",
        "session_id": sid,
        "offset": {"start": pending[0]["index"], "end": pending[-1]["index"] + 1},
        "capture_origin": "live",
        "batch_id": "",
        "speaker": {"id": sender_id, "role": "owner", "display": ""},
        "created_at": _utc_now(),
        "payload": {"turn_id": turn_id, "messages": pending},
    }
    parts = contract.capture_turn_parts(envelope)
    for part in parts:
        contract.validate_envelope(part, idempotency_key=part["event_id"])
    return parts


_SESSION_LOCK = threading.Lock()
_SESSION_STATE: dict[str, Any] = {"active": None, "high_water": {}, "sent_tail": {}}


def _note_turn(session_id: str, end_offset: int) -> None:
    """Track the live session binding and its message high-water mark."""
    if not session_id:
        return
    with _SESSION_LOCK:
        _SESSION_STATE["active"] = session_id
        water = _SESSION_STATE["high_water"]
        if end_offset > water.get(session_id, 0):
            water[session_id] = end_offset


def _high_water(session_id: str) -> int:
    with _SESSION_LOCK:
        return int(_SESSION_STATE["high_water"].get(session_id, 0))


def _forget_session(session_id: str) -> None:
    """Invalidate cached state for a session that has ended or switched away."""
    if not session_id:
        return
    with _SESSION_LOCK:
        if _SESSION_STATE["active"] == session_id:
            _SESSION_STATE["active"] = None
        _SESSION_STATE["high_water"].pop(session_id, None)
        _SESSION_STATE["sent_tail"].pop(session_id, None)


def _enqueue_spool(
    envelope: dict[str, Any], *, priority: int, kind: str, capture_origin: str
) -> None:
    """Enqueue an envelope durably and lazily ensure the sender runs.

    Never raises: capture and boundary paths must not fail the host turn.
    ``start`` is idempotent; the spool worker owns the sender thread.

    Pre-login durability: when no credential is available the envelope is
    only enqueued — the sender is never started or swapped to an
    unauthenticated client, so a missing key can neither quarantine capture
    (``invalid_config`` is permanent) nor displace a running authenticated
    sender. The next capture after login starts/updates the sender normally
    and replays everything pending.
    """
    try:
        spool = get_spool()
        spool.enqueue(
            envelope, priority=priority, kind=kind, capture_origin=capture_origin
        )
        try:
            client = SubstrateClient.from_env()
            if client.api_key:
                spool.start(client)
        except Exception:
            pass
    except SpoolFull:
        pass
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
    """Capture a completed Hermes turn via the durable spool.

    This is the sole live-capture path. The new messages are validated and
    split into parts the server accepts, the session binding and high-water
    mark are updated, and each part is enqueued at ``PRIORITY_LIVE``. Never
    raises and never waits on I/O.
    """
    try:
        envelopes = _capture_envelopes(
            user_content,
            assistant_content,
            session_id=session_id,
            messages=messages or [],
            **kwargs,
        )
        if not envelopes:
            return
        sid = envelopes[-1]["session_id"]
        last_end = int(envelopes[-1]["offset"]["end"])
        last_fingerprint = _message_fingerprint(envelopes[-1]["payload"]["messages"][-1])
        _note_turn(sid, max(int(part["offset"]["end"]) for part in envelopes))
        for envelope in envelopes:
            _enqueue_spool(
                envelope,
                priority=PRIORITY_LIVE,
                kind="capture_turn",
                capture_origin="live",
            )
        with _SESSION_LOCK:
            _SESSION_STATE["sent_tail"][sid] = (last_end, last_fingerprint)
    except Exception:
        pass


_SUBAGENT_PARENTS: dict[str, str] = {}
_MAX_SUBAGENT_PARENTS = 256


def _capture_session_id(session_id: Any) -> str:
    """Route subagent turns under their parent session when one is tracked."""
    sid = _bounded_text(session_id, contract.MAX_SESSION_ID_BYTES)
    if not sid:
        return ""
    with _SESSION_LOCK:
        return _SUBAGENT_PARENTS.get(sid, sid)


def subagent_start(
    parent_session_id: str = "",
    child_session_id: str = "",
    **kwargs: Any,
) -> None:
    """Track a subagent session so its turns capture under the parent.

    Never raises. The map is bounded; oldest entries are dropped first.
    """
    try:
        parent = _bounded_text(parent_session_id, contract.MAX_SESSION_ID_BYTES)
        child = _bounded_text(child_session_id, contract.MAX_SESSION_ID_BYTES)
        if not parent or not child or parent == child:
            return
        with _SESSION_LOCK:
            _SUBAGENT_PARENTS[child] = parent
            while len(_SUBAGENT_PARENTS) > _MAX_SUBAGENT_PARENTS:
                _SUBAGENT_PARENTS.pop(next(iter(_SUBAGENT_PARENTS)))
    except Exception:
        pass


def subagent_stop(
    parent_session_id: str = "",
    child_session_id: str = "",
    **kwargs: Any,
) -> None:
    """Forget a subagent routing entry. Never raises."""
    try:
        child = _bounded_text(child_session_id, contract.MAX_SESSION_ID_BYTES)
        if not child:
            return
        with _SESSION_LOCK:
            _SUBAGENT_PARENTS.pop(child, None)
    except Exception:
        pass


def post_llm_call(
    session_id: str = "",
    user_message: str = "",
    assistant_response: str = "",
    conversation_history: list[Any] | None = None,
    **kwargs: Any,
) -> None:
    """Host turn-completion adapter. Forwards the completed turn to sync_turn.

    The host dispatches ``post_llm_call`` once per turn with the finalized
    full conversation (agent/turn_finalizer.py); there is no ``sync_turn``
    dispatch on this host. :func:`sync_turn` remains the sole capture
    implementation — this adapter only translates host kwargs into it.
    Subagent turns are captured under their parent session. Never raises
    and never waits on network I/O.
    """
    try:
        extra: dict[str, Any] = {}
        for key in ("sender_id", "parent_session_id"):
            value = kwargs.get(key, "")
            if isinstance(value, str) and value:
                extra[key] = value
        sync_turn(
            user_message,
            assistant_response,
            session_id=_capture_session_id(session_id),
            messages=conversation_history or [],
            turn_id=kwargs.get("turn_id", ""),
            platform=kwargs.get("platform", ""),
            **extra,
        )
    except Exception:
        pass
    return None


def prefetch(**kwargs: Any) -> str:
    """Recall prefetch. Always returns an empty string; never captures.

    Not registered: the host dispatches no ``prefetch`` hook, so this is
    kept as a directly callable helper only.
    """
    return ""


# ---------------------------------------------------------------------------
# Session boundaries (content-free)
# ---------------------------------------------------------------------------


def _session_envelope(
    *,
    session_id: Any,
    boundary: str,
    session_complete: bool,
    next_session_id: Any = "",
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Build a content-free ``capture_session`` boundary envelope, or None."""
    sid = _bounded_text(session_id, contract.MAX_SESSION_ID_BYTES)
    if not sid or boundary not in contract.BOUNDARIES:
        return None
    event_id = str(uuid.uuid4())
    water = _high_water(sid)
    if not isinstance(water, int) or water < 0 or water > contract.MAX_SAFE_INTEGER:
        water = 0
    platform = _bounded_text(kwargs.get("platform", ""), contract.MAX_PLATFORM_BYTES) or "hermes"
    chat_type = _bounded_text(kwargs.get("chat_type", ""), contract.MAX_CHAT_TYPE_BYTES) or "direct"
    sender_id = _bounded_text(kwargs.get("sender_id", ""), contract.MAX_SPEAKER_ID_BYTES) or "user"
    payload: dict[str, Any] = {
        "boundary": boundary,
        "session_complete": bool(session_complete),
        "message_high_water": water,
        "platform": platform,
        "chat_type": chat_type,
    }
    clean_next = _bounded_text(next_session_id, contract.MAX_SESSION_ID_BYTES)
    if clean_next:
        payload["next_session_id"] = clean_next
    clean_parent = _bounded_text(kwargs.get("parent_session_id", ""), contract.MAX_SESSION_ID_BYTES)
    if clean_parent:
        payload["parent_session_id"] = clean_parent
    envelope: dict[str, Any] = {
        "schema_version": contract.SCHEMA_VERSION,
        "contract_version": contract.CONTRACT_VERSION,
        "event_id": event_id,
        "kind": "capture_session",
        "session_id": sid,
        "offset": {"start": water, "end": water},
        "capture_origin": "live",
        "batch_id": "",
        "speaker": {"id": sender_id, "role": "owner", "display": ""},
        "created_at": _utc_now(),
        "payload": payload,
    }
    contract.validate_envelope(envelope, idempotency_key=event_id)
    return envelope


def on_session_end(session_id: str = "", **kwargs: Any) -> None:
    """Per-turn host hook. Intentionally emits nothing.

    The host fires ``on_session_end`` at the end of every turn
    (agent/turn_finalizer.py), not at true session end — emitting a
    ``session_complete`` boundary here would seal the server's extraction
    window after every turn. True completion arrives via
    :func:`on_session_finalize`. Never raises.
    """
    return None


def on_session_finalize(session_id: str = "", **kwargs: Any) -> None:
    """Emit the content-free session-end boundary and clear session state.

    The host fires ``on_session_finalize`` for the OLD session on
    ``/new``/``/reset``, on CLI shutdown, and on gateway session expiry
    (cli.py ``_notify_session_boundary``). The envelope carries no
    messages, only the boundary marker and the message high-water mark.
    Never raises and never waits on I/O.
    """
    try:
        envelope = _session_envelope(
            session_id=session_id,
            boundary="end",
            session_complete=True,
            **kwargs,
        )
        if envelope is None:
            return
        _enqueue_spool(
            envelope,
            priority=PRIORITY_LIVE,
            kind="capture_session",
            capture_origin="live",
        )
        _forget_session(envelope["session_id"])
    except Exception:
        pass


def on_session_reset(session_id: str = "", **kwargs: Any) -> None:
    """Host session-rotation adapter. Emits the switch boundary, rebinds.

    The host fires ``on_session_reset`` with the NEW session id after
    rotating (cli.py ``new_session``); the session being left is the
    tracked live binding. Delegates to :func:`on_session_switch`.
    Never raises and never waits on I/O.
    """
    try:
        new = _bounded_text(session_id, contract.MAX_SESSION_ID_BYTES)
        with _SESSION_LOCK:
            old = _SESSION_STATE["active"]
        if not new:
            return
        if old and old != new:
            on_session_switch(old, new, **kwargs)
            return
        with _SESSION_LOCK:
            _SESSION_STATE["active"] = new
    except Exception:
        pass


def on_session_switch(
    session_id: str = "",
    next_session_id: str = "",
    **kwargs: Any,
) -> None:
    """Emit the boundary for the OLD session, rebind, and clear stale state.

    The ``capture_session`` event names the session being left (with
    ``next_session_id`` pointing at the session being entered), the live
    binding is moved to the new session, and any cached state for the old
    session is invalidated. The envelope carries no messages. This is the
    direct implementation used by :func:`on_session_reset`; it is not
    host-dispatched under this name. Never raises and never waits on I/O.
    """
    try:
        old = kwargs.get("old_session_id", session_id)
        new = kwargs.get("new_session_id", next_session_id)
        envelope = _session_envelope(
            session_id=old,
            boundary="switch",
            session_complete=True,
            next_session_id=new,
            **kwargs,
        )
        if envelope is None:
            return
        _enqueue_spool(
            envelope,
            priority=PRIORITY_LIVE,
            kind="capture_session",
            capture_origin="live",
        )
        _forget_session(envelope["session_id"])
        clean_new = _bounded_text(new, contract.MAX_SESSION_ID_BYTES)
        if clean_new:
            with _SESSION_LOCK:
                _SESSION_STATE["active"] = clean_new
    except Exception:
        pass


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


def _call_tool(tool_name: str, request: dict[str, Any], shape: Any) -> str:
    client = None
    try:
        client = SubstrateClient.from_env()
        if not client.api_key:
            return _login_grant_result()
        structured, _text = client.call_tool(tool_name, request, timeout=3.0)
        return _fit_result(shape(structured))
    except contract.ContractError:
        return _error("invalid_response")
    except ValueError:
        return _error("invalid_request")
    except ClientError as exc:
        if exc.category in ("unauthorized", "forbidden"):
            _note_auth_failure(client.api_key if client is not None else "")
        return _error(exc.category)
    except Exception:
        return _error("transport_error")


def _login_grant_result() -> str:
    """Surface the pending approval link (or a plain error) to the agent."""
    status = _start_login()
    if isinstance(status, dict) and status.get("status") == "finishing":
        return _compact(
            {
                "status": "authorization_finishing",
                "message": f"{status.get('message', '')} Retry this call on a later message.",
            }
        )
    if isinstance(status, dict) and status.get("status") == "authorization_pending":
        result = {
            "status": "authorization_required",
            "message": (
                "Substrate memory is not connected yet. Show the user this link and code, "
                "ask them to open the link, sign in, and choose Approve connection, then "
                "retry this call; the plugin finishes by itself after approval. "
                "Never paste an API key into chat."
            ),
            "verification_uri_complete": str(status.get("verification_uri_complete", "")),
            "user_code": str(status.get("user_code", "")),
            "expires_in": status.get("expires_in", 0),
        }
        if status.get("previous"):
            result["previous"] = str(status["previous"])
        if status.get("notice"):
            result["notice"] = str(status["notice"])
        return _compact(result)
    if isinstance(status, dict) and status.get("message"):
        return _compact(
            {
                "status": "authorization_required",
                "error_class": str(status.get("error_class", "")),
                "message": f"{status['message']} {_cli_hint()}",
            }
        )
    return _login_required_result()


def _note_auth_failure(rejected: str = "") -> None:
    """Best-effort reconnect: heal or drop the stale credential, keep spool."""
    try:
        from . import onboarding as _onboarding

        _onboarding.note_auth_failure(rejected if isinstance(rejected, str) else "")
    except Exception:
        pass


def _shape_search(value: dict[str, Any]) -> dict[str, Any]:
    if not contract.memory_tool_version_ok(value) or not isinstance(value.get("results"), list):
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
    return {"contract_version": contract.MCP_CONTRACT_VERSION, "results": results}


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
    return _call_tool("memory_search", request, _shape_search)


def _shape_expand(value: dict[str, Any], expected: str) -> dict[str, Any]:
    if not contract.memory_tool_version_ok(value) or value.get("handle") != expected:
        raise contract.ContractError("invalid_response")
    if not isinstance(value.get("kind"), str):
        raise contract.ContractError("invalid_response")
    result: dict[str, Any] = {
        "contract_version": contract.MCP_CONTRACT_VERSION,
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
        "memory_expand",
        {"handle": handle},
        lambda value: _shape_expand(value, handle),
    )


def _bounded_excerpt(value: Any) -> Any:
    clean = _safe_value(value)
    if isinstance(clean, dict):
        return {str(key): child for key, child in list(clean.items())[:32]}
    return clean


def _shape_evidence(value: dict[str, Any]) -> dict[str, Any]:
    if not contract.memory_tool_version_ok(value) or not isinstance(value.get("excerpts"), list):
        raise contract.ContractError("invalid_response")
    result: dict[str, Any] = {
        "contract_version": contract.MCP_CONTRACT_VERSION,
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
    return _call_tool("memory_evidence", request, _shape_evidence)


_LEDGER_HANDLE_RE = re.compile(r"^m:[0-9a-f]{8,64}$")


def _ledger_envelope(
    *, kind: str, payload: dict[str, Any], session_id: Any, sender_id: Any
) -> dict[str, Any]:
    """Build a validated explicit ledger envelope (memory_write/memory_forget)."""
    if kind not in {"memory_write", "memory_forget"}:
        raise ValueError(kind)
    sid = _bounded_text(session_id, contract.MAX_SESSION_ID_BYTES) or "explicit"
    speaker_id = _bounded_text(sender_id, contract.MAX_SPEAKER_ID_BYTES) or "user"
    event_id = str(uuid.uuid4())
    envelope: dict[str, Any] = {
        "schema_version": contract.SCHEMA_VERSION,
        "contract_version": contract.CONTRACT_VERSION,
        "event_id": event_id,
        "kind": kind,
        "session_id": sid,
        "offset": {"start": 0, "end": 0},
        "capture_origin": "live",
        "batch_id": "",
        "speaker": {"id": speaker_id, "role": "owner", "display": ""},
        "created_at": _utc_now(),
        "payload": payload,
    }
    contract.validate_envelope(envelope, idempotency_key=event_id)
    return envelope


def _mcp_handle(structured: Any, tool: str) -> str:
    """Extract the server's ``m:`` handle from a write-tool result.

    The spooled ``memory_write``/``memory_forget`` envelope is retired
    separately by the spool sender on its ``memory_import`` per-item action;
    the tool additionally requires the ``m:`` handle the server allocates.
    """
    if not isinstance(structured, dict):
        raise contract.ContractError("invalid_response", f"{tool}: expected object")
    if not contract.memory_tool_version_ok(structured):
        raise contract.ContractError("invalid_response", f"{tool}: unsupported contract_version")
    handle = structured.get("handle")
    if not isinstance(handle, str) or _LEDGER_HANDLE_RE.fullmatch(handle) is None:
        raise contract.ContractError("invalid_response", f"{tool}: missing handle")
    return handle


_QUEUED_REMEMBER = {
    "status": "queued",
    "message": (
        "Saved and queued. It syncs to Substrate automatically once the service is "
        "reachable; there is nothing to retry. Do not call memory_remember again for "
        "this fact."
    ),
}
_QUEUED_FORGET = {
    "status": "queued",
    "message": (
        "Queued. The change syncs to Substrate automatically once the service is "
        "reachable; there is nothing to retry. Do not call memory_forget again for "
        "this handle."
    ),
}


def _queued_or_error(queued: bool, category: str, tool_name: str) -> str:
    """A spooled write is never reported as failed: it will be delivered.

    ``queued`` means the envelope is durable in the spool, so the spool sender
    delivers it (idempotently, by event id) even when this synchronous call
    could not. Only a write that was neither sent nor queued reports failure.
    """
    if not queued:
        return _error(category)
    result = _QUEUED_FORGET if tool_name == "memory_forget" else _QUEUED_REMEMBER
    return json.dumps(result, separators=(",", ":"))


def _ledger_call(envelope: dict[str, Any], tool_name: str, arguments: dict[str, Any]) -> str:
    """Durably enqueue an explicit envelope, call its MCP tool, return its handle.

    The envelope is spooled at ``PRIORITY_EXPLICIT`` (delivered later through
    ``memory_import`` so the write stays durable) and the same operation is
    additionally sent synchronously as ``memory_remember``/``memory_forget``
    under the same ``operation_id`` so the tool can return the server's
    handle. Returns compact JSON ``{"handle": "m:..."}`` when the server
    answered. When the write is spooled but the answer is not available
    (offline, transient or unknown outcome), returns a ``queued`` status: the
    spool delivers it with the same event id, so a retry would duplicate it.
    Returns ``{"error": category}`` only for a write that was neither sent nor
    queued, or that the server refused permanently.
    """
    try:
        contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    except Exception:
        return _error("invalid_request")
    queued = False
    try:
        get_spool().enqueue(
            envelope,
            priority=PRIORITY_EXPLICIT,
            kind=envelope["kind"],
            capture_origin=envelope["capture_origin"],
        )
        queued = True
    except SpoolFull:
        pass
    except Exception:
        pass
    try:
        client = SubstrateClient.from_env()
    except Exception:
        return _queued_or_error(queued, "transport_error", tool_name)
    if queued:
        # Start the sender so a queued write is delivered without waiting for
        # the next capture. Idempotent; a missing key leaves it spooled.
        try:
            if client.api_key:
                get_spool().start(client)
        except Exception:
            pass
    try:
        structured, _text = client.call_tool(
            tool_name,
            arguments,
            timeout=3.0,
            max_response_bytes=65_536,
        )
        handle = _mcp_handle(structured, tool_name)
        return json.dumps({"handle": handle}, separators=(",", ":"))
    except contract.ContractError:
        # The server answered something unusable: the outcome is unknown, so
        # the spooled copy (same event id) is the source of truth.
        return _queued_or_error(queued, "invalid_response", tool_name)
    except ClientError as exc:
        if exc.category in ("unauthorized", "forbidden"):
            _note_auth_failure(client.api_key)
        if exc.category in ("unauthorized", "forbidden") or is_transient_category(exc.category):
            return _queued_or_error(queued, exc.category, tool_name)
        return _error(exc.category)
    except Exception:
        return _queued_or_error(queued, "transport_error", tool_name)


def memory_remember(args: dict[str, Any], **kwargs: Any) -> str:
    """Write a fact to Substrate memory; returns the server's ``m:`` handle.

    Takes ``text`` plus ``durability`` (``durable``, ``time_bounded``, or
    ``transient``) and an optional ``about`` label. Spools ledger kind
    ``memory_write`` and calls the ``memory_remember`` MCP tool. Returns
    compact JSON ``{"handle": "m:..."}`` on success and only
    ``{"error": category}`` on failure.
    """
    try:
        args = _strict_args(args, {"text", "durability", "about"}, {"text", "durability"})
        text = args["text"]
        durability = args["durability"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError
        if not isinstance(durability, str) or durability not in contract.DURABILITIES:
            raise ValueError
        about = args.get("about", "")
        if not isinstance(about, str):
            raise ValueError
        clean_text = _bounded_text(text, contract.MAX_TEXT_BYTES)
        if not clean_text.strip():
            raise ValueError
        payload: dict[str, Any] = {
            "text": clean_text,
            "durability": durability,
            "source": "memory_remember",
        }
        if about:
            payload["about"] = _bounded_text(about, contract.MAX_ABOUT_BYTES)
        envelope = _ledger_envelope(
            kind="memory_write",
            payload=payload,
            session_id=kwargs.get("session_id", ""),
            sender_id=kwargs.get("sender_id", ""),
        )
        tool_args: dict[str, Any] = {
            "operation_id": envelope["event_id"],
            "text": clean_text,
            "durability": durability,
        }
        if about:
            tool_args["about"] = _bounded_text(about, contract.MAX_ABOUT_BYTES)
    except Exception:
        return _error("invalid_request")
    return _ledger_call(envelope, "memory_remember", tool_args)


def memory_forget(args: dict[str, Any], **kwargs: Any) -> str:
    """marks a memory as no longer true; it stays in the record, keeps its evidence, and can be revived by later information.

    Takes exactly one ``handle`` (``m:`` or ``p:``; a list of handles is
    refused) plus a required non-empty ``reason``. Spools ledger kind
    ``memory_forget`` for that atom only (never for related atoms) and calls
    the ``memory_forget`` MCP tool. Returns compact JSON ``{"handle": "m:..."}``
    on success and only ``{"error": category}`` on failure.
    """
    try:
        args = _strict_args(args, {"handle", "reason"}, {"handle", "reason"})
        handle = args["handle"]
        reason = args["reason"]
        if not isinstance(handle, str) or contract.HANDLE_RE.fullmatch(handle) is None:
            raise ValueError
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError
        clean_reason = _bounded_text(reason, contract.MAX_REASON_BYTES)
        if not clean_reason.strip():
            raise ValueError
        envelope = _ledger_envelope(
            kind="memory_forget",
            payload={"handle": handle, "reason": clean_reason},
            session_id=kwargs.get("session_id", ""),
            sender_id=kwargs.get("sender_id", ""),
        )
        tool_args = {
            "operation_id": envelope["event_id"],
            "handle": handle,
            "reason": clean_reason,
        }
    except Exception:
        return _error("invalid_request")
    return _ledger_call(envelope, "memory_forget", tool_args)


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


MEMORY_REMEMBER_SCHEMA = {
    "name": "memory_remember",
    "description": (
        "Write one fact to Substrate memory. Returns the new m: handle. If the result "
        "says queued, the fact is saved and syncs automatically; do not retry. Not for "
        "importing past conversations or sessions: that is substrate_import."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The fact to remember."},
            "durability": {
                "type": "string",
                "enum": ["durable", "time_bounded", "transient"],
                "description": "How long the memory should be kept.",
            },
            "about": {"type": "string", "description": "Optional label for the memory."},
        },
        "required": ["text", "durability"],
        "additionalProperties": False,
    },
}
MEMORY_FORGET_SCHEMA = {
    "name": "memory_forget",
    "description": (
        "marks a memory as no longer true; it stays in the record, keeps its evidence, "
        "and can be revived by later information. "
        "Takes exactly one handle plus a required reason; applies to that atom only, "
        "never to related atoms, and a list of handles is refused. If the result says "
        "queued, the change syncs automatically; do not retry."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "pattern": contract.HANDLE_RE.pattern,
                "description": (
                    "Exactly one memory handle (m: or p:; a list is refused). "
                    "marks a memory as no longer true; it stays in the record, keeps its evidence, "
                    "and can be revived by later information."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Why the memory is no longer true. Required and non-empty. "
                    "marks a memory as no longer true; it stays in the record, keeps its evidence, "
                    "and can be revived by later information."
                ),
            },
        },
        "required": ["handle", "reason"],
        "additionalProperties": False,
    },
}


def register(ctx: Any) -> None:
    """Register the native Hermes surface without I/O or worker startup."""
    ctx.register_hook("pre_llm_call", pre_llm_call)
    # Host dispatch evidence (hermes-agent, read-only):
    # - "post_llm_call" fires once per turn with the finalized conversation
    #   (agent/turn_finalizer.py) and is the live-capture entry point; it
    #   forwards into sync_turn, which remains the sole implementation.
    # - "on_session_end" fires at the end of every turn, so it emits
    #   nothing; true completion arrives via "on_session_finalize".
    # - "on_session_reset" carries the NEW session id after rotation and
    #   drives the switch boundary for the previously bound session.
    # - "subagent_start"/"subagent_stop" carry parent/child session ids for
    #   parent-routed capture (tools/delegate_tool.py).
    # "sync_turn", "prefetch", and "on_session_switch" are NOT registered:
    # the host dispatches no hook under those names (unknown names only log
    # a warning), so they stay directly callable/tested helpers.
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    ctx.register_hook("on_session_reset", on_session_reset)
    ctx.register_hook("subagent_start", subagent_start)
    ctx.register_hook("subagent_stop", subagent_stop)
    ctx.register_tool(
        name="memory_search", toolset=TOOLSET, schema=MEMORY_SEARCH_SCHEMA, handler=memory_search
    )
    ctx.register_tool(
        name="memory_expand", toolset=TOOLSET, schema=MEMORY_EXPAND_SCHEMA, handler=memory_expand
    )
    ctx.register_tool(
        name="memory_evidence", toolset=TOOLSET, schema=MEMORY_EVIDENCE_SCHEMA, handler=memory_evidence
    )
    ctx.register_tool(
        name="memory_remember", toolset=TOOLSET, schema=MEMORY_REMEMBER_SCHEMA, handler=memory_remember
    )
    ctx.register_tool(
        name="memory_forget", toolset=TOOLSET, schema=MEMORY_FORGET_SCHEMA, handler=memory_forget
    )
    ctx.register_tool(
        name="substrate_import", toolset=TOOLSET, schema=SUBSTRATE_IMPORT_SCHEMA,
        handler=substrate_import,
    )
    # This static section is frozen per session; dynamic memory stays in the
    # supported pre_llm_call injection path.
    ctx.register_system_prompt_section(
        "substrate.memory", STATIC_MEMORY_PROMPT, position="after_memory", max_chars=2000
    )
