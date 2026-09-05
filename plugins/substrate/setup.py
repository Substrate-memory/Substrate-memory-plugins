#!/usr/bin/env python3
"""One-prompt connect for the Substrate Hermes plugin (stdlib only).

Compat entry point matching the public plugin workflow
(``python <plugin-dir>/setup.py --hermes-home <home>``): prints the
device-approval link as JSON, waits for browser approval, and stores the
tenant-scoped key privately in the active profile. For the staged
start/status/poll flow use ``onboard.py`` instead.

Works from the installed plugin directory with no PYTHONPATH or editable
install.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _bootstrap() -> None:
    plugin_dir = Path(__file__).resolve().parent
    src = plugin_dir / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


_bootstrap()

from substrate import credentials  # noqa: E402
from substrate.onboarding import (  # noqa: E402
    OnboardingManager,
    _load_state,
    check_origin,
    resolve_origin,
    token_is_valid,
)


def connect(home: Path, origin: str, *, poll_timeout: float = 900.0) -> dict:
    """Connect the profile; prints the pending link, then waits boundedly."""
    existing = os.environ.get(credentials.ENV_KEY) or credentials.stored_api_key(home)
    if existing and token_is_valid(origin, existing):
        try:
            credentials.save_origin(home, origin)
        except credentials.CredentialError:
            pass
        return {"status": "ready", "credential": "existing", "origin": origin}
    manager = OnboardingManager(home, origin)
    status = manager.ensure(force=True)
    if status is None:
        return {"status": "ready", "credential": "existing", "origin": origin}
    if status.get("status") != "authorization_pending":
        raise RuntimeError(str(status.get("error_class", "authorization_failed")))
    # Content-free link for the agent to show the user; no secrets printed.
    print(json.dumps(status, sort_keys=True), flush=True)
    deadline = time.monotonic() + max(0.0, poll_timeout)
    while time.monotonic() < deadline:
        time.sleep(1.0)
        state = _load_state(home)
        phase = state.get("phase")
        if phase == "connected":
            return {
                "status": "ready",
                "credential": "device_authorization",
                "origin": origin,
            }
        if phase in {"failed", "declined", "invalid"}:
            raise RuntimeError(
                str(manager.describe(state).get("error_class", "authorization_failed"))
            )
    raise RuntimeError("authorization_expired")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Connect the active Hermes profile to Substrate"
    )
    parser.add_argument("--hermes-home", type=Path, default=None)
    parser.add_argument("--origin", default=None)
    parser.add_argument("--name", default=None,
                        help="display name shown for this agent during approval")
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="maximum seconds to wait for approval")
    args = parser.parse_args(argv)
    if args.name:
        os.environ["SUBSTRATE_AGENT_NAME"] = str(args.name)[:64]
    try:
        active = credentials.active_home()
        home = Path(args.hermes_home).expanduser().resolve() if args.hermes_home else active
        origin = check_origin(args.origin or resolve_origin())
    except Exception:  # noqa: BLE001 - CLI reports only a safe category
        print(json.dumps({"status": "failed", "error": "invalid_config"}),
              file=sys.stderr)
        return 1
    try:
        result = connect(home, origin, poll_timeout=float(args.timeout))
    except RuntimeError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True),
              file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - never leak internals
        print(json.dumps({"status": "failed", "error": "internal_error"}),
              file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
