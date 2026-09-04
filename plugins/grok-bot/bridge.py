#!/usr/bin/env python3
"""Conversation-capture bridge for Grok Bot surfaces.

Standard library only. MCP carries tool calls but no turn hooks, so this
module gives the harness three plain functions with exact parity to the
Hermes plugin hooks:

- ``pre_turn_context(...)``  — bounded ``<memory-context>`` injection for
  the next model request (fail closed: returns ``""`` on any failure, or
  the onboarding notice when device approval is pending).
- ``capture_turn(...)``      — nonblocking completed-turn capture.
- ``session_reset(...)`` / ``session_end(...)`` — session boundary markers.

Plus ``STATIC_MEMORY_PROMPT`` (re-exported) for the system/instructions
channel, and ``tool_manifest()`` returning the xAI/OpenAI-compatible
function-calling manifest for the three tools.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from substrate_core import runtime

STATIC_MEMORY_PROMPT = runtime.STATIC_MEMORY_PROMPT


def pre_turn_context(
    session_id: str = "",
    user_message: str = "",
    conversation_history: list[Any] | None = None,
    **kwargs: Any,
) -> str:
    """Return the ``<memory-context>`` block (or onboarding notice), else ``""``."""
    try:
        result = runtime.pre_llm_call(
            session_id, user_message, conversation_history or [], **kwargs
        )
    except Exception:
        return ""
    if not isinstance(result, dict):
        return ""
    context = result.get("context", "")
    return context if isinstance(context, str) else ""


def capture_turn(
    user_message: str = "",
    assistant_response: str = "",
    *,
    session_id: str = "",
    conversation_history: list[Any] | None = None,
    **kwargs: Any,
) -> None:
    """Enqueue a completed turn for background capture. Never blocks, never raises."""
    try:
        runtime.post_llm_call(
            session_id, user_message, assistant_response, conversation_history or [], **kwargs
        )
    except Exception:
        pass


def session_reset(*, session_id: str = "", **kwargs: Any) -> None:
    """Mark the previous session complete on conversation reset. Never raises."""
    try:
        runtime.on_session_reset(session_id=session_id, **kwargs)
    except Exception:
        pass


def session_end(*, session_id: str = "", **kwargs: Any) -> None:
    """Mark the session complete on shutdown/expiry. Never raises."""
    try:
        runtime.on_session_finalize(session_id=session_id, **kwargs)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    """Minimal CLI for Grok Build hooks (`bridge.py pre-turn`, ...)."""
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="Substrate memory turn bridge")
    parser.add_argument("command", choices=["pre-turn", "capture-turn", "session-end", "session-reset"])
    parser.add_argument("--session-id", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--history", default="[]", help="JSON array of {role, content} rows")
    args = parser.parse_args(argv)
    try:
        history = _json.loads(args.history)
    except ValueError:
        history = []
    if args.command == "pre-turn":
        print(pre_turn_context(args.session_id, args.message, history))
    elif args.command == "capture-turn":
        capture_turn(args.message, "", session_id=args.session_id, conversation_history=history)
    elif args.command == "session-reset":
        session_reset(session_id=args.session_id)
    else:
        session_end(session_id=args.session_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def tool_manifest() -> list[dict[str, Any]]:
    """Return the xAI/OpenAI function-calling ``tools`` list for the 3 tools."""
    manifest = []
    for schema in (
        runtime.MEMORY_SEARCH_SCHEMA,
        runtime.MEMORY_EXPAND_SCHEMA,
        runtime.MEMORY_EVIDENCE_SCHEMA,
    ):
        manifest.append(
            {
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["parameters"],
                },
            }
        )
    return manifest
