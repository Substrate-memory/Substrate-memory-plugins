"""Cowork host-home adaptation for the vendored Substrate core.

Claude Cowork shares the Claude Code plugin, skill, hook, and MCP extension
surface, and the same per-user configuration home (``~/.claude``). This module
is the single place that knows that home so the vendored ``client.py`` and
``onboarding.py`` stay byte-identical to the Hermes reference except for their
clearly-marked delegation patches. Standard library only.
"""

from __future__ import annotations

import os
from pathlib import Path

# Local ground truth (Claude Code CLI 2.1.259, ``claude --version``): the
# per-user home holding settings, plugin caches, and session state is
# ``~/.claude`` (observed at ``/home/substrateops/.claude`` with a
# ``plugins/marketplaces`` cache). ``CLAUDE_CONFIG_DIR`` overrides it for the
# CLI; ``SUBSTRATE_HOME`` overrides everything for this plugin.
CLAUDE_HOME_NAME = ".claude"
SUBSTRATE_HOME_ENV = "SUBSTRATE_HOME"
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"


def candidate_homes(location_file: str | None = None) -> list[Path]:
    """Ordered candidate homes; the first known one wins (never scan others)."""
    values: list[Path] = []
    configured = os.environ.get(SUBSTRATE_HOME_ENV, "").strip()
    if configured:
        values.append(Path(configured).expanduser())
    claude_dir = os.environ.get(CLAUDE_CONFIG_DIR_ENV, "").strip()
    if claude_dir:
        values.append(Path(claude_dir).expanduser())
    if location_file is not None:
        location = Path(location_file).resolve()
        # Installed layout: .../plugins/claude-cowork/substrate_core/<module>.py
        # has no host home above it (marketplace paths vary), so unlike Hermes
        # there is nothing to derive here; the check is kept explicit on purpose.
        _ = location
    values.append(Path.home() / CLAUDE_HOME_NAME)
    result: list[Path] = []
    for value in values:
        try:
            resolved = value.resolve()
        except OSError:
            continue
        if resolved not in result:
            result.append(resolved)
    return result


def profile_homes(location_file: str | None = None) -> list[Path]:
    """The one active home; a fallback is never inspected when one is known."""
    values = candidate_homes(location_file)
    return values[:1]


def active_home() -> Path:
    """The one active Cowork/Claude home; no other profile is ever inspected."""
    homes = profile_homes()
    return homes[0] if homes else Path.home() / CLAUDE_HOME_NAME


def token_path(home: Path) -> Path:
    return home / "substrate" / "credentials" / "access-token"


def env_path(home: Path) -> Path:
    return home / ".env"
