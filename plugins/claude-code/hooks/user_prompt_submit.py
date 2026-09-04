#!/usr/bin/env python3
"""UserPromptSubmit hook: bounded pre-turn Substrate memory context. Stdlib only.

Reads the hook JSON from stdin, fetches the validated turn-context block, and
returns it as ``hookSpecificOutput.additionalContext``. Any failure injects
nothing (fail closed). When device onboarding is pending, the approval URL is
surfaced in both ``additionalContext`` and ``systemMessage`` so the agent shows
the exact clickable link to the user.
"""

from __future__ import annotations

import sys

import hooklib
import runtime


def main() -> int:
    try:
        data = hooklib.read_input()
        session_id = str(data.get("session_id", ""))
        prompt = str(data.get("prompt", ""))
        history = hooklib.load_transcript(data.get("transcript_path"))
        result = runtime.get_turn_context(
            session_id,
            prompt,
            history,
            platform="claude-code",
            chat_type="direct",
        )
    except Exception:
        return 0
    if not result or not result.get("context"):
        return 0
    block = result["context"]
    output: dict = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": block,
        }
    }
    if block.startswith("<substrate-connect>"):
        # Onboarding notice: make the approval URL user-visible as well.
        output["systemMessage"] = block
    hooklib.emit(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
