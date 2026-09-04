"""Codex home resolution for the Substrate plugin.

Standard library only. The credential/state layout mirrors the Hermes
plugin exactly: ``<host home>/substrate/credentials/access-token`` (0600)
and ``SUBSTRATE_API_KEY`` in the host profile ``.env``.
"""

from __future__ import annotations

import os
from pathlib import Path


def codex_home() -> Path:
    """Return the active Codex home directory (resolved, no I/O)."""
    for override in ("SUBSTRATE_HOME", "CODEX_HOME"):
        configured = os.environ.get(override, "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def credential_path(home: Path | None = None) -> Path:
    """Path of the stored access token for *home*."""
    root = home or codex_home()
    return root / "substrate" / "credentials" / "access-token"


def env_path(home: Path | None = None) -> Path:
    """Path of the host profile ``.env`` holding ``SUBSTRATE_API_KEY``."""
    root = home or codex_home()
    return root / ".env"
