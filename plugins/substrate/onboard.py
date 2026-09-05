#!/usr/bin/env python3
"""Substrate Hermes login CLI (stable file-path executable, stdlib only).

Usage:
    python /path/to/hermes-substrate/onboard.py start --json
    python /path/to/hermes-substrate/onboard.py status --json
    python /path/to/hermes-substrate/onboard.py poll --json [--timeout 900]

Works from the installed plugin directory with no PYTHONPATH or editable
install: this shim adds the bundled ``src/`` to ``sys.path`` for its own
process only (the host plugin loader is unaffected).
"""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap() -> None:
    plugin_dir = Path(__file__).resolve().parent
    src = plugin_dir / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


_bootstrap()

from substrate.onboarding import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
