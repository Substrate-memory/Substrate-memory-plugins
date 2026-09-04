#!/usr/bin/env python3
"""One-prompt connect for the Substrate Claude Code plugin (setup equivalent).

Prints the RFC 8628 approval URL as JSON, waits for browser approval, then
stores the tenant-scoped key privately under the Claude home. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from substrate_core import onboarding  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect Claude Code to Substrate")
    parser.add_argument("--name", help="display name shown for this agent in Substrate")
    parser.add_argument("--no-wait", action="store_true")
    args = parser.parse_args(argv)
    if args.name:
        os.environ["SUBSTRATE_AGENT_NAME"] = str(args.name)[:64]
    try:
        home = onboarding.active_home()
        origin = onboarding.check_origin(onboarding.resolve_origin())
        result = onboarding.run_cli(home, origin, wait=not args.no_wait)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_class": str(exc)}), flush=True)
        return 1
    if result.get("status") == "authorization_pending":
        print(json.dumps(result, sort_keys=True), flush=True)
        print("Open the verification_uri_complete URL above in a browser.", flush=True)
        return 0
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result.get("status") == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
