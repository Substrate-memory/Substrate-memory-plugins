#!/usr/bin/env python3
"""SessionStart hook: deliver the frozen static Substrate memory prompt.

Prints ``{"hookSpecificOutput": {"additionalContext": STATIC_MEMORY_PROMPT}}``.
No network, no config reads, never fails: always exits 0 with JSON on stdout.
Standard library only.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from substrate_core import runtime


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass
    try:
        print(json.dumps({"hookSpecificOutput": {"additionalContext": runtime.static_prompt()}}))
    except Exception:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
