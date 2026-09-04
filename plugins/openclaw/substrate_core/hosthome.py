"""OpenClaw home resolution for the Substrate plugin.

Single active home, never another profile. Precedence:
1. ``SUBSTRATE_HOME`` explicit override (portable, test-friendly).
2. ``OPENCLAW_STATE_DIR`` (OpenClaw state directory when set).
3. Installed layout: ``<HOME>/plugins/openclaw/substrate_core/hosthome.py``.
4. ``~/.openclaw`` default.
"""

from __future__ import annotations

import os
from pathlib import Path


def openclaw_home() -> Path:
    """Return the one active OpenClaw home directory (resolved)."""
    configured = os.environ.get("SUBSTRATE_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    state_dir = os.environ.get("OPENCLAW_STATE_DIR", "").strip()
    if state_dir:
        return Path(state_dir).expanduser().resolve()
    location = Path(__file__).resolve()
    # Installed layout: <OPENCLAW_HOME>/plugins/openclaw/substrate_core/hosthome.py.
    if location.parent.name == "substrate_core" and location.parent.parent.name == "openclaw":
        return location.parents[2]
    return (Path.home() / ".openclaw").resolve()
