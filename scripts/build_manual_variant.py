#!/usr/bin/env python3
"""Generate plugins/substrate-claude-manual from plugins/substrate-claude.

The Manual capture mode is its own plugin rather than a setting. Claude Code
calls the capture hooks (``mcp_tool`` hooks) straight on the Substrate server,
so a local config value cannot turn them off; the only way is to ship a plugin
whose hooks.json does not contain them. Manual keeps recall
(``memory_turn_context``, sent with ``capture_mode: "manual"`` so the server opens
no pending turn and never stores the prompt) and every local hook (setup offer,
session id, sign-in).

Never edit plugins/substrate-claude-manual by hand. Change substrate-claude,
then run this script. CI runs ``--check``
(tests/test_substrate_claude_manual_plugin.py).

Usage:
  python3 scripts/build_manual_variant.py          write the variant
  python3 scripts/build_manual_variant.py --check  list stale files (exit 1)

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugins" / "substrate-claude"
TARGET = ROOT / "plugins" / "substrate-claude-manual"
SOURCE_NAME = "substrate-claude"
TARGET_NAME = "substrate-claude-manual"

# The mcp_tool hooks that send conversation content (or capture bookkeeping) to the server.
CAPTURE_TOOLS = frozenset({"memory_capture_tool", "memory_capture_turn", "memory_session_boundary"})
# Byte-for-byte copies. scripts/ must stay identical: substrate_sync.py reads its plugin
# name from the manifest at run time, and session_id_hook.py does not depend on the name.
VERBATIM_DIRS = ("scripts", "assets")
VERBATIM_FILES = (".mcp.json",)
# Text whose tool, skill and command names change with the plugin name.
RENAMED_DIRS = ("commands", "skills")
SKIPPED_PARTS = frozenset({"__pycache__", ".pytest_cache"})

README = """# Substrate Memory for Claude Code: Manual capture

This is the **Manual** build of [`substrate-claude`](../substrate-claude/README.md). It is generated from that
plugin by `scripts/build_manual_variant.py`, so do not edit it by hand.

| | Automatic (`substrate-claude`) | Manual (`substrate-claude-manual`) |
|---|---|---|
| Recall on every prompt (`memory_turn_context`) | yes | yes |
| Live capture of tool calls (`memory_capture_tool`) | yes | **no** |
| Live capture of assistant turns (`memory_capture_turn`) | yes | **no** |
| Compaction boundary (`memory_session_boundary`) | yes | **no** |
| Prompt stored as a captured turn | yes | **no** (`capture_mode: "manual"`) |
| `memory_search` / `memory_remember` / import on request | yes | yes |

In Manual, nothing from a conversation is saved unless you ask: tell Claude to remember something, or run
`/substrate-claude-manual:substrate-import`. Recall still sends each prompt to Substrate to find relevant memory, but the server does not store it
(this needs a Substrate server that accepts `capture_mode`).

Install exactly one of the two plugins:

```bash
claude plugin marketplace add Substrate-memory/Substrate-memory-plugins
claude plugin install substrate-claude-manual@substrate-marketplace
```

To switch from Automatic, uninstall `substrate-claude@substrate-marketplace` first, then restart Claude Code.
`/substrate-claude-manual:substrate-connect` asks for the mode and prints the switch commands.
Everything else (sign-in, import, scope, permissions) works as described in the
[substrate-claude README](../substrate-claude/README.md).
"""


def _rename(text: str) -> str:
    """Plugin-name references: MCP tool prefix (`..._substrate-claude_...`) and the
    skill, command and server namespace (`substrate-claude:`)."""
    return (text.replace(SOURCE_NAME + "_", TARGET_NAME + "_")
                .replace(SOURCE_NAME + ":", TARGET_NAME + ":"))


def _rename_json(value):
    if isinstance(value, str):
        return _rename(value)
    if isinstance(value, list):
        return [_rename_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _rename_json(item) for key, item in value.items()}
    return value


def _dump(doc) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def manual_hooks(source: dict) -> dict:
    """Source hooks.json minus the capture mcp_tool hooks; empty groups and events are dropped."""
    events = {}
    for event, groups in source["hooks"].items():
        kept_groups = []
        for group in groups:
            hooks = [hook for hook in group.get("hooks", [])
                     if not (hook.get("type") == "mcp_tool" and hook.get("tool") in CAPTURE_TOOLS)]
            # Recall would still open a pending turn that the server seals with the prompt in it.
            hooks = [{**hook, "input": {**hook["input"], "capture_mode": "manual"}}
                     if hook.get("tool") == "memory_turn_context" else hook for hook in hooks]
            if hooks:
                kept_groups.append({**group, "hooks": hooks})
        if kept_groups:
            events[event] = kept_groups
    return _rename_json({**source, "hooks": events})


def manual_manifest(source: dict) -> dict:
    manifest = dict(source)
    manifest["name"] = TARGET_NAME
    manifest["displayName"] = "Substrate Memory (Manual)"
    manifest["description"] = ("Manual capture: Substrate recall without live conversation capture. "
                               + source.get("description", ""))
    return manifest


def _files(directory: Path):
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not SKIPPED_PARTS.intersection(path.relative_to(directory).parts):
            yield path


def expected() -> dict[str, bytes]:
    """Relative path -> exact bytes of every file in the generated plugin."""
    out: dict[str, bytes] = {}
    for name in VERBATIM_FILES:
        out[name] = (SOURCE / name).read_bytes()
    for name in VERBATIM_DIRS:
        for path in _files(SOURCE / name):
            out[path.relative_to(SOURCE).as_posix()] = path.read_bytes()
    for name in RENAMED_DIRS:
        for path in _files(SOURCE / name):
            out[path.relative_to(SOURCE).as_posix()] = _rename(path.read_text(encoding="utf-8")).encode("utf-8")
    source_hooks = json.loads((SOURCE / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    out["hooks/hooks.json"] = _dump(manual_hooks(source_hooks))
    source_manifest = json.loads((SOURCE / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    out[".claude-plugin/plugin.json"] = _dump(manual_manifest(source_manifest))
    out["README.md"] = README.encode("utf-8")
    return out


def stale(target: Path = TARGET) -> list[str]:
    """Files that are missing, different or extra in the generated plugin."""
    want = expected()
    have = {path.relative_to(target).as_posix(): path for path in _files(target)} if target.exists() else {}
    problems = []
    for rel, data in sorted(want.items()):
        if rel not in have:
            problems.append("missing: " + rel)
        elif have[rel].read_bytes() != data:
            problems.append("differs: " + rel)
    problems += ["extra: " + rel for rel in sorted(set(have) - set(want))]
    return problems


def write(target: Path = TARGET) -> None:
    want = expected()
    if target.exists():
        for path in _files(target):
            if path.relative_to(target).as_posix() not in want:
                path.unlink()
    for rel, data in want.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)
        if rel.startswith("scripts/"):
            path.chmod((SOURCE / rel).stat().st_mode & 0o777)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        problems = stale()
        for line in problems:
            print(line)
        if problems:
            print("Run: python3 scripts/build_manual_variant.py")
        return 1 if problems else 0
    write()
    return 0


if __name__ == "__main__":
    sys.exit(main())
