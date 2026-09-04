#!/usr/bin/env python3
"""Stop hook: queue nonblocking completed-turn capture, then exit at once.

The transcript is captured by a detached ``capture_worker.py`` process so this
hook never blocks the turn on network I/O. Always succeeds (fail closed).
"""

from __future__ import annotations

import os
import sys

import hooklib


def main() -> int:
    try:
        data = hooklib.read_input()
        session_id = str(data.get("session_id", ""))
        transcript = str(data.get("transcript_path", ""))
        if session_id and transcript:
            hooklib.spawn_detached(
                [sys.executable, os.path.join(hooklib.PLUGIN_ROOT, "hooks",
                                              "capture_worker.py"),
                 "turn", session_id, transcript]
            )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
