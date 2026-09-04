#!/usr/bin/env python3
"""One-prompt setup for the Substrate Grok Bot plugin (stdlib only).

Mirrors plugins/substrate/setup.py: connects the active Grok profile via
RFC 8628 device onboarding (prints the clickable approval URL, polls,
validates with capabilities + search health check, stores the key
privately). Run with ``--grok-home`` or let it resolve SUBSTRATE_HOME >
GROK_HOME > ~/.grok > ~/.config/grok.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from substrate_core import onboarding
from substrate_core.client import _profile_homes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect the active Grok profile to Substrate")
    parser.add_argument("--grok-home", type=Path)
    parser.add_argument("--origin")
    parser.add_argument("--name", help="display name shown for this agent in Substrate")
    parser.add_argument("--no-wait", action="store_true", help="print the approval link and exit")
    args = parser.parse_args(argv)
    homes = _profile_homes()
    active_home = (homes[0] if homes else Path.home() / ".grok").resolve()
    home = (args.grok_home or active_home).resolve()
    if home != active_home:
        print(json.dumps({"status": "failed", "error": "active_profile_mismatch"}), flush=True)
        return 1
    if args.name:
        os.environ["SUBSTRATE_AGENT_NAME"] = str(args.name)[:64]
    try:
        origin = onboarding.check_origin(onboarding.resolve_origin() if not args.origin else args.origin)
    except onboarding.OnboardingError as exc:
        print(json.dumps({"status": "failed", "error": exc.category}, sort_keys=True))
        return 1
    try:
        result = onboarding.run_cli(home, origin, wait=not args.no_wait)
    except onboarding.OnboardingError as exc:
        print(json.dumps({"status": "failed", "error": exc.category}, sort_keys=True))
        return 1
    if result.get("status") not in {"ready"}:
        print(json.dumps(result, sort_keys=True), flush=True)
        return 1
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
