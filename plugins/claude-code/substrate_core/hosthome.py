"""Claude Code home resolution for Substrate state.

Credentials live under ``<home>/substrate/`` exactly like the Hermes plugin:
``credentials/access-token`` (0600) plus ``SUBSTRATE_API_KEY`` in the host
profile env. ``SUBSTRATE_HOME`` overrides the detected home.
"""

from __future__ import annotations

import os
from pathlib import Path


def host_home() -> Path:
    """The one active Claude Code home; no other profile is ever inspected."""
    for key in ("SUBSTRATE_HOME", "CLAUDE_HOME"):
        configured = os.environ.get(key, "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
    return (Path.home() / ".claude").resolve()
