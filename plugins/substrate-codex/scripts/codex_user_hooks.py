#!/usr/bin/env python3
"""Install, check and remove the Substrate hooks in the user's Codex hook file.

Why: Codex 0.154 does not load the hooks of a local plugin install
(hooks/hooks.json), so `/hooks` shows 0 for every event. Codex does load user
hooks from `$CODEX_HOME/hooks.json` (hook source "user"; see CDX-04). This
module writes the plugin's hook table there, tags each entry it writes, and
removes exactly those entries again. Codex then asks the user to trust the
new hooks once in `/hooks`.

Usage:
  codex_user_hooks.py install [--codex-home DIR]
  codex_user_hooks.py remove  [--codex-home DIR]
  codex_user_hooks.py status  [--codex-home DIR]

Guarantees:
  * Foreign entries (anything without our tag) are never changed or reordered
    relative to each other. Our entries go first in each event list, so
    Codex's positional trust keys for them stay stable.
  * Running install twice gives byte-identical output and no write.
  * The first write keeps a one-time copy at hooks.json.substrate-backup.
  * Writes are atomic (temp file in the same directory, then os.replace).
  * A malformed hooks.json is reported and left untouched.
  * If Codex already runs this plugin's own hooks (a Codex update or a
    remote-catalog install), install skips the user entries to avoid
    duplicate captures, and removes any it wrote earlier.

Standard library only; exit status 1 means "not changed, see message".
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TAG_KEY = "x-substrate-managed"
TAG_VALUE = "substrate-codex"
HOOKS_FILE = "hooks.json"
BACKUP_FILE = "hooks.json.substrate-backup"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class HookConfigError(Exception):
    """The existing user hook file has a shape this module will not edit."""


# --------------------------------------------------------------------------
# Desired entries (derived from the plugin's own hooks/hooks.json)
# --------------------------------------------------------------------------

def _script_command(script: Path, args: str) -> str:
    quoted = shlex.quote(str(script))
    return (f"python3 {quoted} {args} 2>/dev/null || "
            f"python {quoted} {args} 2>/dev/null || true")


def desired_hooks(plugin_root: Path = PLUGIN_ROOT) -> dict:
    """Plugin hook table with absolute script paths and the ownership tag.

    `mcp_tool` handlers are copied unchanged. The plugin's `${CLAUDE_PLUGIN_ROOT}`
    command handler is rewritten with the script's absolute path, because a
    user hook file has no plugin root variable.
    """
    plugin_hooks = json.loads((plugin_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    script = (plugin_root / "scripts" / "substrate_sync.py").resolve()
    out: dict = {}
    for event, groups in plugin_hooks.items():
        new_groups = []
        for group in groups:
            group = copy.deepcopy(group)
            for handler in group["hooks"]:
                if handler.get("type") == "command":
                    command = handler.get("command", "")
                    flags = {"--offer-check": "UserPromptSubmit", "--session-start": "SessionStart"}
                    flag = next((f for f in flags if f in command), None)
                    if flag is None or flags[flag] != event:
                        raise HookConfigError(f"unexpected plugin command hook under {event}")
                    handler["command"] = _script_command(script, f"--host codex {flag}")
                handler[TAG_KEY] = TAG_VALUE
            new_groups.append(group)
        out[event] = new_groups
    return out


# --------------------------------------------------------------------------
# Document handling
# --------------------------------------------------------------------------

def _is_ours(handler: object) -> bool:
    return isinstance(handler, dict) and handler.get(TAG_KEY) == TAG_VALUE


def _load(path: Path) -> tuple[dict, bool]:
    """Return (document, existed). A missing or empty file is an empty document."""
    if not path.exists():
        return {}, False
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}, True
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise HookConfigError(f"{path} is not valid JSON ({exc}); left unchanged") from exc
    if not isinstance(doc, dict):
        raise HookConfigError(f"{path} must contain a JSON object; left unchanged")
    hooks = doc.get("hooks", {})
    if not isinstance(hooks, dict):
        raise HookConfigError(f"{path}: 'hooks' must be an object; left unchanged")
    for event, groups in hooks.items():
        if not isinstance(groups, list) or not all(
            isinstance(g, dict) and isinstance(g.get("hooks", []), list) for g in groups
        ):
            raise HookConfigError(f"{path}: unexpected shape under '{event}'; left unchanged")
    return doc, True


def _state_key(path: Path, event: str, group_index: int, handler_index: int) -> str:
    """Trust-state key Codex uses for a hook in a user hook file (see hooks/list `key`)."""
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", event).lower()
    return f"{path}:{snake}:{group_index}:{handler_index}"


def owned_state_keys(doc: dict, path: Path) -> list[str]:
    """Trust-state keys of the entries we tagged, computed from the file as it is now."""
    keys = []
    for event, groups in doc.get("hooks", {}).items():
        for gi, group in enumerate(groups):
            for hi, handler in enumerate(group.get("hooks", [])):
                if _is_ours(handler):
                    keys.append(_state_key(path, event, gi, hi))
    return keys


def _strip_ours(doc: dict) -> dict:
    """Return a copy of doc without our tagged handlers; foreign entries keep their order."""
    doc = copy.deepcopy(doc)
    hooks = doc.get("hooks", {})
    for event in list(hooks):
        groups = hooks[event]
        if not groups:
            continue
        kept = []
        for group in groups:
            handlers = group.get("hooks", [])
            foreign = [h for h in handlers if not _is_ours(h)]
            if len(foreign) == len(handlers):
                kept.append(group)
            elif foreign:
                group["hooks"] = foreign
                kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return doc


def _merge(doc: dict, desired: dict) -> dict:
    merged = _strip_ours(doc)
    hooks = merged.setdefault("hooks", {})
    for event, groups in desired.items():
        hooks[event] = copy.deepcopy(groups) + hooks.get(event, [])
    return merged


def _is_empty_shell(doc: dict) -> bool:
    return set(doc) <= {"hooks"} and not doc.get("hooks")


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _backup_once(path: Path) -> None:
    backup = path.with_name(BACKUP_FILE)
    if path.exists() and not backup.exists():
        _write_bytes_atomic(backup, path.read_bytes())


def _write_doc(path: Path, doc: dict) -> None:
    _write_bytes_atomic(path, (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


CONFIG_FILE = "config.toml"
CONFIG_BACKUP_FILE = "config.toml.substrate-backup"
_STATE_HEADER = re.compile(r'^\[hooks\.state\."([^"\\]*)"\]\s*$')


def _forget_trust_state(codex_home: Path, keys: list[str]) -> bool:
    """Delete the [hooks.state."<key>"] tables Codex wrote for our entries.

    Only tables whose exact key we own are removed; other tables and all other
    lines of config.toml are kept byte for byte. Returns True when it wrote.
    """
    path = codex_home / CONFIG_FILE
    if not keys or not path.exists():
        return False
    owned = set(keys)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept: list[str] = []
    dropping = False
    removed = 0
    for line in lines:
        match = _STATE_HEADER.match(line.strip())
        if match:
            dropping = match.group(1) in owned
            if dropping:
                removed += 1
                continue
        elif line.lstrip().startswith("["):
            dropping = False
        if not dropping:
            kept.append(line)
    if not removed:
        return False
    backup = codex_home / CONFIG_BACKUP_FILE
    if not backup.exists():
        _write_bytes_atomic(backup, path.read_bytes())
    _write_bytes_atomic(path, "".join(kept).encode("utf-8"))
    return True


# --------------------------------------------------------------------------
# Codex check: is this plugin's own hook already active?
# --------------------------------------------------------------------------

def _plugin_hooks_active(timeout: float = 20.0) -> bool | None:
    """True when Codex lists this plugin's hooks as active; None when it cannot tell.

    Asks the local app server with `hooks/list` (the same call the TUI makes).
    The child process is started here and killed through its handle.
    """
    exe = shutil.which("codex")
    if not exe:
        return None
    try:
        proc = subprocess.Popen(
            [exe, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, cwd=os.getcwd(),
        )
    except OSError:
        return None
    lines: queue.Queue = queue.Queue()

    def _pump() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.put(line)

    threading.Thread(target=_pump, daemon=True).start()

    def _send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    try:
        _send({"id": 1, "method": "initialize",
               "params": {"clientInfo": {"name": "substrate-codex-hooks", "version": "1"}}})
        deadline = time.monotonic() + timeout
        sent_handshake = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                msg = json.loads(lines.get(timeout=remaining))
            except queue.Empty:
                return None
            except ValueError:
                continue
            if msg.get("id") == 1 and not sent_handshake:
                _send({"method": "initialized"})
                _send({"id": 2, "method": "hooks/list", "params": {"cwds": [os.getcwd()]}})
                sent_handshake = True
            elif msg.get("id") == 2:
                if "result" not in msg:
                    return None
                return any(
                    hook.get("source") == "plugin" and hook.get("enabled")
                    and "substrate-codex" in str(hook.get("pluginId") or "")
                    for entry in msg["result"].get("data", [])
                    for hook in entry.get("hooks", [])
                )
    except (OSError, ValueError):
        return None
    finally:
        proc.kill()
        proc.wait(timeout=5)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def install(codex_home: Path, plugin_root: Path = PLUGIN_ROOT, check_plugin: bool = True) -> dict:
    path = codex_home / HOOKS_FILE
    doc, existed = _load(path)
    plugin_active = _plugin_hooks_active() if check_plugin else None
    if plugin_active:
        # Codex already runs the plugin's own hooks: do not add a second copy.
        return _remove_from(path, doc, existed, reason="plugin hooks already active in Codex")
    new_doc = _merge(doc, desired_hooks(plugin_root))
    if existed and new_doc == doc:
        return {"action": "install", "changed": False, "path": str(path),
                "events": _counts(new_doc), "plugin_hooks_active": plugin_active}
    if existed:
        _backup_once(path)
    _write_doc(path, new_doc)
    return {"action": "install", "changed": True, "path": str(path),
            "events": _counts(new_doc), "plugin_hooks_active": plugin_active}


def remove(codex_home: Path) -> dict:
    path = codex_home / HOOKS_FILE
    doc, existed = _load(path)
    return _remove_from(path, doc, existed, reason=None)


def _remove_from(path: Path, doc: dict, existed: bool, reason: str | None) -> dict:
    keys = owned_state_keys(doc, path.resolve())
    if not keys:
        result = {"action": "remove", "changed": False, "path": str(path)}
    else:
        new_doc = _strip_ours(doc)
        if _is_empty_shell(new_doc) and not path.with_name(BACKUP_FILE).exists():
            path.unlink()  # the file was created by install; leave nothing behind
        else:
            _write_doc(path, new_doc)
        trust_cleared = _forget_trust_state(path.parent, keys)
        result = {"action": "remove", "changed": True, "path": str(path),
                  "removed_entries": len(keys), "trust_state_cleared": trust_cleared}
    if reason:
        result["action"] = "install"
        result["skipped"] = reason
    return result


def _counts(doc: dict) -> dict:
    out = {}
    for event, groups in doc.get("hooks", {}).items():
        n = sum(1 for g in groups for h in g.get("hooks", []) if _is_ours(h))
        if n:
            out[event] = n
    return out


def status(codex_home: Path) -> dict:
    path = codex_home / HOOKS_FILE
    doc, existed = _load(path)
    return {"path": str(path), "present": existed,
            "backup": path.with_name(BACKUP_FILE).exists(), "events": _counts(doc)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("install", "remove", "status"))
    parser.add_argument("--codex-home", type=Path, default=None,
                        help="defaults to $CODEX_HOME, then ~/.codex")
    parser.add_argument("--no-plugin-check", action="store_true",
                        help="skip the 'plugin hooks already active' check")
    args = parser.parse_args(argv)
    home = args.codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        if args.command == "install":
            result = install(home, check_plugin=not args.no_plugin_check)
        elif args.command == "remove":
            result = remove(home)
        else:
            result = status(home)
    except (HookConfigError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, **result}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
