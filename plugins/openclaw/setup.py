#!/usr/bin/env python3
"""One-prompt setup for the Substrate OpenClaw plugin (stdlib only).

Connects the active OpenClaw home via RFC 8628 device authorization and
stores the tenant-scoped key privately. Prints one JSON object.

Usage:
  python3 setup.py [--openclaw-home PATH] [--origin URL] [--name NAME]

With no flags it uses SUBSTRATE_HOME / OPENCLAW_STATE_DIR / installed layout /
~/.openclaw, exactly like the plugin runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from substrate_core import onboarding  # noqa: E402
from substrate_core.hosthome import openclaw_home  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect OpenClaw to Substrate")
    parser.add_argument("--openclaw-home", type=Path)
    parser.add_argument("--origin")
    parser.add_argument("--name", help="display name shown for this agent in Substrate")
    args = parser.parse_args(argv)
    active = openclaw_home()
    home = args.openclaw_home.resolve() if args.openclaw_home else active
    if home != active:
        print(json.dumps({"status": "failed", "error": "active_profile_mismatch"}), flush=True)
        return 1
    if args.name:
        os.environ["SUBSTRATE_AGENT_NAME"] = str(args.name)[:64]
    try:
        origin = onboarding.check_origin(onboarding.resolve_origin() if not args.origin else args.origin)
    except onboarding.OnboardingError:
        print(json.dumps({"status": "failed", "error": "invalid_config"}), flush=True)
        return 1
    try:
        result = onboarding.run_cli(home, origin)
    except onboarding.OnboardingError as exc:
        print(json.dumps({"status": "failed", "error": exc.category}), flush=True)
        return 1
    ok = isinstance(result, dict) and result.get("status") in {"ready", "authorization_pending"}
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
