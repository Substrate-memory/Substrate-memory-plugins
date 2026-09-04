#!/usr/bin/env python3
"""Stdio JSON bridge between the OpenClaw JS adapter and the vendored core.

Protocol: read one JSON object from stdin ``{"command": ..., "payload": {...}}``
and write one JSON object to stdout. Exit 0 with ``{"ok": true, ...}`` on
success; exit 0 with ``{"ok": false, "error": "<category>"}`` on any failure.
Stderr is for fatal framing errors only. Never prints credentials.

Commands:
  turn_context  pre-turn bounded memory context (payload: session_id,
                user_message, conversation_history, + optional kwargs)
  search        memory_search handler (payload: {"args": {...}})
  expand        memory_expand handler (payload: {"args": {...}})
  evidence      memory_evidence handler (payload: {"args": {...}})
  capture       completed-turn capture envelope + synchronous POST, best
                effort (payload: user_content, assistant_content, session_id,
                messages, + optional kwargs); returns {"posted": bool}
  session       session boundary marker (payload: session_id, boundary,
                platform, chat_type, next_session_id, parent_session_id,
                high_water); returns {"posted": bool, "queued": bool}

Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from substrate_core import runtime  # noqa: E402

ERRORS = frozenset(
    {
        "invalid_request",
        "invalid_response",
        "invalid_config",
        "timeout",
        "transport_error",
    }
)


def _payload(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _do_turn_context(payload: dict[str, Any]) -> dict[str, Any]:
    result = runtime.get_turn_context(
        payload.get("session_id", ""),
        payload.get("user_message", ""),
        payload.get("conversation_history", []),
        **{k: v for k, v in payload.items()
           if k not in {"session_id", "user_message", "conversation_history"}},
    )
    return {"ok": True, "context": result}


def _do_tool(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    handler = {
        "search": runtime.memory_search,
        "expand": runtime.memory_expand,
        "evidence": runtime.memory_evidence,
    }[name]
    text = handler(payload.get("args", {}))
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError, UnicodeError):
        return {"ok": False, "error": "invalid_response"}
    if isinstance(value, dict) and set(value) == {"error"}:
        category = value.get("error")
        return {"ok": False, "error": category if category in ERRORS else "transport_error"}
    return {"ok": True, "result": value}


def _do_capture(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        envelope = runtime._capture_envelope(
            payload.get("user_content", ""),
            payload.get("assistant_content", ""),
            session_id=payload.get("session_id", ""),
            messages=payload.get("messages", []),
            **{k: v for k, v in payload.items()
               if k not in {"user_content", "assistant_content", "session_id", "messages"}},
        )
    except Exception:
        return {"ok": True, "posted": False}
    if envelope is None:
        return {"ok": True, "posted": False}
    posted = runtime.post_envelope_now(envelope)
    return {"ok": True, "posted": posted}


def _do_session(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        boundary = payload.get("boundary", "end")
        if boundary not in runtime.contract.BOUNDARIES:
            return {"ok": False, "error": "invalid_request"}
        envelope = runtime._session_envelope(
            payload.get("session_id", ""),
            boundary,
            platform=payload.get("platform", ""),
            chat_type=payload.get("chat_type", ""),
            next_session_id=payload.get("next_session_id", ""),
            parent_session_id=payload.get("parent_session_id", ""),
            high_water=int(payload.get("high_water", 0) or 0),
        )
    except Exception:
        return {"ok": False, "error": "invalid_request"}
    if envelope is None:
        return {"ok": False, "error": "invalid_request"}
    posted = runtime.post_envelope_now(envelope)
    return {"ok": True, "posted": posted}


def main() -> int:
    try:
        raw = sys.stdin.read()
        request = json.loads(raw)
    except Exception:
        sys.stderr.write("bridge: invalid request\n")
        return 2
    if not isinstance(request, dict) or not isinstance(request.get("command"), str):
        sys.stderr.write("bridge: invalid request\n")
        return 2
    command = request["command"]
    payload = _payload(request.get("payload"))
    try:
        if command == "turn_context":
            response = _do_turn_context(payload)
        elif command in {"search", "expand", "evidence"}:
            response = _do_tool(command, payload)
        elif command == "capture":
            response = _do_capture(payload)
        elif command == "session":
            response = _do_session(payload)
        else:
            return _finish({"ok": False, "error": "invalid_request"})
    except Exception:
        response = {"ok": False, "error": "transport_error"}
    return _finish(response)


def _finish(response: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(response, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    # Guard: never let a traceback (which could echo local paths only, never
    # tokens) escape; the adapter treats any failure as fail-closed.
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        try:
            sys.stdout.write(json.dumps({"ok": False, "error": "transport_error"}) + "\n")
        except Exception:
            pass
        raise SystemExit(0)
