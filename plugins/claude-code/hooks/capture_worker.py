#!/usr/bin/env python3
"""Detached Substrate capture worker for the Stop/SessionEnd hooks. Stdlib only.

Usage: capture_worker.py turn <session_id> <transcript_path>
       capture_worker.py session <session_id> "" [reset|end] [next_session_id]

Reads the transcript, builds the validated capture envelope, and POSTs it to
``/api/v1/ledger/events``. Started detached by the hook scripts so hook latency
stays near zero; this process itself waits briefly for the POST to finish.
Every failure is swallowed; capture never blocks a turn.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hooklib  # noqa: E402
import runtime  # noqa: E402


def _capture_turn(session_id: str, transcript_path: str) -> None:
    messages = hooklib.load_transcript(transcript_path)
    if not messages:
        return
    user_text, assistant_text = hooklib.last_texts(messages)
    if not user_text and not assistant_text:
        return
    runtime.queue_turn_capture(
        user_text,
        assistant_text,
        session_id=session_id,
        messages=messages,
        platform="claude-code",
        chat_type="direct",
    )


def _capture_session(session_id: str, boundary: str, next_session_id: str) -> None:
    if boundary not in ("reset", "end"):
        boundary = "end"
    runtime.queue_session_boundary(
        session_id,
        boundary=boundary,
        platform="claude-code",
        chat_type="direct",
        next_session_id=next_session_id,
    )


def main(argv: list[str]) -> int:
    try:
        mode = argv[1] if len(argv) > 1 else ""
        session_id = argv[2] if len(argv) > 2 else ""
        if not session_id:
            return 0
        if mode == "turn":
            _capture_turn(session_id, argv[3] if len(argv) > 3 else "")
        elif mode == "session":
            _capture_session(session_id, argv[4] if len(argv) > 4 else "end",
                             argv[5] if len(argv) > 5 else "")
        else:
            return 0
        try:
            runtime.flush_capture(timeout=8.0)
        except Exception:
            pass
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(argv=sys.argv))
