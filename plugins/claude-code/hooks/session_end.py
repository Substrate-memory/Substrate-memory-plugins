#!/usr/bin/env python3
"""SessionEnd hook: queue the session-complete marker, then exit at once.

Host equivalent of ``on_session_finalize`` (``reason != "clear"``) and
``on_session_reset`` (``reason == "clear"``). Detached worker, fail closed.
"""

from __future__ import annotations

import os
import sys

import hooklib


def main() -> int:
    try:
        data = hooklib.read_input()
        session_id = str(data.get("session_id", ""))
        reason = str(data.get("reason", ""))
        boundary = "reset" if reason == "clear" else "end"
        if session_id:
            hooklib.spawn_detached(
                [sys.executable, os.path.join(hooklib.PLUGIN_ROOT, "hooks",
                                              "capture_worker.py"),
                 "session", session_id, "", boundary]
            )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
