#!/usr/bin/env python3
"""SessionStart hook: static memory prompt, onboarding notice, reset markers.

- Always injects ``STATIC_MEMORY_PROMPT`` so the session has the static memory
  section (Claude Code has no system-prompt-section API for plugins).
- Starts RFC 8628 device onboarding on first use and surfaces the exact
  ``verification_uri_complete`` approval URL when one is issued.
- On ``source == "clear"``, queues a ``capture_session`` reset marker for the
  previous session (host equivalent of ``on_session_reset``).
- Every failure is silent; the session always starts (fail closed).
"""

from __future__ import annotations

import json
import os
import sys

import hooklib
import runtime
from substrate_core import onboarding


def _last_session_path(home: str) -> str:
    return os.path.join(home, "substrate", "claude-last-session.json")


def _handle_reset_marker(session_id: str, source: str) -> None:
    if source != "clear" or not session_id:
        return
    try:
        home = str(onboarding.active_home())
        path = _last_session_path(home)
        try:
            with open(path, encoding="utf-8") as stream:
                previous = json.load(stream).get("session_id", "")
        except (OSError, ValueError, AttributeError):
            previous = ""
        if isinstance(previous, str) and previous and previous != session_id:
            hooklib.spawn_detached(
                [sys.executable, os.path.join(hooklib.PLUGIN_ROOT, "hooks",
                                              "capture_worker.py"),
                 "session", previous, "", "reset", session_id]
            )
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"session_id": session_id}, stream)
    except Exception:
        pass


def main() -> int:
    try:
        data = hooklib.read_input()
        session_id = str(data.get("session_id", ""))
        source = str(data.get("source", ""))
        _handle_reset_marker(session_id, source)
        try:
            status = onboarding.ensure_started()
        except Exception:
            status = None
        notice = ""
        if isinstance(status, dict) and status.get("status") == "authorization_pending":
            notice = runtime._onboarding_notice(status)
        context = runtime.STATIC_MEMORY_PROMPT + ("\n" + notice if notice else "")
        output: dict = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }
        if notice:
            output["systemMessage"] = notice
        hooklib.emit(output)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
