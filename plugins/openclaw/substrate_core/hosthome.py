"""OpenClaw home resolution for the Substrate plugin.

Single active home, never another profile. Precedence:
1. ``SUBSTRATE_HOME`` explicit override (portable, test-friendly).
2. ``OPENCLAW_STATE_DIR`` (OpenClaw state directory when set).
3. ``~/.openclaw`` default.

There is deliberately no installed-layout walk: a repo checkout also looks
like ``.../plugins/openclaw/...`` and must never resolve as a home.
"""

from __future__ import annotations

import os
from pathlib import Path


def openclaw_home() -> Path:
    """Return the one active OpenClaw home directory (resolved)."""
    # OPENCLAW HOST-HOME PATCH: single active home only (SUBSTRATE_HOME >
    # OPENCLAW_STATE_DIR > ~/.openclaw). No installed-layout walk: a repo
    # checkout path (which also looks like .../plugins/openclaw/...) must
    # never resolve as a home. No HERMES_HOME, no ~/.hermes fallback.
    configured = os.environ.get("SUBSTRATE_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    state_dir = os.environ.get("OPENCLAW_STATE_DIR", "").strip()
    if state_dir:
        return Path(state_dir).expanduser().resolve()
    return (Path.home() / ".openclaw").resolve()
