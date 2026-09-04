#!/usr/bin/env python3
"""SessionEnd hook: queue a session-complete marker without blocking.

Spawns the detached capture runner for the ``capture_session`` envelope and
exits 0 immediately. Prints {}. Standard library only. Never prints a token.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}
    if not isinstance(event, dict):
        event = {}
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        runner = os.path.join(here, "capture_runner.py")
        session_id = str(event.get("session_id", ""))
        transcript_path = event.get("transcript_path")
        args = [sys.executable, runner, "session-end", session_id,
                transcript_path if isinstance(transcript_path, str) else ""]
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
