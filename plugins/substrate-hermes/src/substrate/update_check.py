"""Once-a-day check for a newer Substrate plugin on main. Silent on any failure.

The refresh runs in a background thread at session start, so no turn waits for it.
The line is shown at most once a day. The cache holds only a version number.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

from .client import PLUGIN_VERSION

UPDATE_URL = "https://github.com/Substrate-memory/Substrate-memory-plugins/raw/main/VERSION"
UPDATE_TIMEOUT = 2.0
UPDATE_INTERVAL = 24 * 3600
UPDATE_OPT_OUT = "SUBSTRATE_UPDATE_CHECK"
STATE_NAME = "update-check.json"
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def _tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _fetch() -> str:
    try:
        request = urllib.request.Request(UPDATE_URL, headers={"User-Agent": "substrate-hermes/" + PLUGIN_VERSION})
        with urllib.request.urlopen(request, timeout=UPDATE_TIMEOUT) as response:  # noqa: S310 fixed https URL
            raw = response.read(64).decode("ascii", "replace").strip()
    except Exception:  # noqa: BLE001 - silent on any failure
        return ""
    return raw if _VERSION.match(raw) else ""


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: Path, value: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".%d.tmp" % os.getpid())
        temp.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


def refresh(home: Path, now: float | None = None) -> None:
    """Fetch the latest version at most once a day and cache it. Never raises."""
    try:
        if os.environ.get(UPDATE_OPT_OUT, "1") == "0":
            return
        current = time.time() if now is None else now
        path = home / STATE_NAME
        state = _read(path)
        checked = state.get("checked_at")
        if isinstance(checked, (int, float)) and 0 <= current - checked < UPDATE_INTERVAL:
            return
        latest = _fetch() or (state.get("latest") if isinstance(state.get("latest"), str) else "")
        _write(path, {**state, "checked_at": current, "latest": latest})
    except Exception:  # noqa: BLE001
        return


def refresh_in_background(home: Path) -> None:
    threading.Thread(target=refresh, args=(home,), name="substrate-update-check", daemon=True).start()


def notice(home: Path, now: float | None = None) -> str:
    """One line when a newer version is cached and was not shown in the last day. Never raises."""
    try:
        if os.environ.get(UPDATE_OPT_OUT, "1") == "0":
            return ""
        path = home / STATE_NAME
        state = _read(path)
        latest = state.get("latest")
        if not isinstance(latest, str) or not _VERSION.match(latest):
            return ""
        if _tuple(latest) <= _tuple(PLUGIN_VERSION):
            return ""
        current = time.time() if now is None else now
        shown = state.get("notified_at")
        if isinstance(shown, (int, float)) and 0 <= current - shown < UPDATE_INTERVAL:
            return ""
        _write(path, {**state, "notified_at": current})
        return ("Substrate %s is available (you have %s). Tell the user once, in one line: update with "
                "`hermes plugins update substrate`, or set plugins.auto_apply: true in ~/.hermes/config.yaml."
                % (latest, PLUGIN_VERSION))
    except Exception:  # noqa: BLE001
        return ""
