#!/usr/bin/env python3
"""Substrate Hermes login CLI at the repository root (stdlib only).

Same as ``plugins/substrate-hermes/onboard.py``; present here so the path is
``<profile>/plugins/substrate/onboard.py`` whether Hermes installed the
repository root (by URL) or the ``plugins/substrate-hermes`` directory.

Usage:
    python onboard.py start      # prints the approval link and code
    python onboard.py poll       # waits for approval, then "Connected to Substrate as ..."
    python onboard.py status
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "plugins" / "substrate-hermes" / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from substrate.onboarding import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
