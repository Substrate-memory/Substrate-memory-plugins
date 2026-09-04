#!/usr/bin/env python3
"""UserPromptSubmit hook: bounded pre-turn Substrate memory context.

Reads the hook JSON from stdin, fetches one validated turn-context block, and
prints ``{"hookSpecificOutput": {"additionalContext": block}}``. First use
with no credential starts RFC 8628 device onboarding and surfaces the exact
``verification_uri_complete`` approval URL as additional context instead.
Always exits 0 with JSON on stdout; every failure injects nothing ({}).
Standard library only. Never prints a token.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from substrate_core import runtime
from substrate_core import transcript


def _prompt_text(event: dict) -> str:
    for key in ("prompt", "user_message", "user_prompt", "message"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}
    if not isinstance(event, dict):
        event = {}
    try:
        session_id = str(event.get("session_id", ""))
        prompt = _prompt_text(event)
        transcript_path = event.get("transcript_path")
        history = (
            transcript.history_for_context(transcript_path)
            if isinstance(transcript_path, str) and transcript_path
            else []
        )
        # The transcript tail already ends with this prompt in most cases;
        # append it when it is not there so the request carries the message.
        # Clip by UTF-8 bytes (not chars) to match the transcript bridge and
        # the Hermes reference bound.
        clipped = runtime._clip_utf8(prompt, 4096)
        if prompt and (not history or history[-1].get("content") != clipped):
            history = history + [{"role": "user", "content": clipped}]
        result = runtime.get_memory_context(
            session_id,
            prompt,
            history,
            platform="cowork",
            chat_type="direct",
            agent_context="claude-cowork",
        )
        if result and result.get("context"):
            print(json.dumps({"hookSpecificOutput": {"additionalContext": result["context"]}}))
        else:
            print("{}")
    except Exception:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
