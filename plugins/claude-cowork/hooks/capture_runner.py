#!/usr/bin/env python3
"""Detached capture runner for the Stop/SessionEnd hooks (stdlib only).

Usage: capture_runner.py (capture-turn|session-end) <session_id> [transcript_path]

Runs detached from the hook (stdin/stdout/stderr are the caller's DEVNULL):
reads the Claude transcript, builds the contract-validated envelope, and POSTs
it once with a short timeout. Fail-closed and silent: all exceptions are
swallowed, nothing is printed, and a token never appears in output.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from substrate_core import runtime
from substrate_core import transcript


def main(argv: list[str]) -> int:
    try:
        mode = argv[1] if len(argv) > 1 else ""
        session_id = argv[2] if len(argv) > 2 else ""
        transcript_path = argv[3] if len(argv) > 3 else ""
        if not session_id:
            return 0
        if mode == "capture-turn":
            messages = transcript.messages_for_capture(transcript_path) if transcript_path else []
            runtime.post_turn_capture_now(
                "", "", session_id=session_id, messages=messages, platform="cowork",
            )
        elif mode == "session-end":
            runtime.post_session_boundary_now(
                session_id, boundary="end", platform="cowork", chat_type="direct",
            )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
