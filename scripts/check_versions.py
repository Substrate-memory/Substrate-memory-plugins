#!/usr/bin/env python3
"""VERSION is the single source of the plugin version. Check every copy, or set them.

Usage:
  python3 scripts/check_versions.py          list every copy that differs from VERSION (exit 1)
  python3 scripts/check_versions.py --write  copy VERSION into every copy listed below

Standard library only. tests/test_versions.py runs the check in CI.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# Each entry: relative path and the kind of copy it holds.
JSON_COPIES = (
    "plugins/substrate-claude/.claude-plugin/plugin.json",
    "plugins/substrate-mcp/.claude-plugin/plugin.json",
    "plugins/substrate-codex/plugin.json",
    "plugins/substrate-codex/.codex-plugin/plugin.json",
)
MARKETPLACE_COPY = ".claude-plugin/marketplace.json"  # every entry of "plugins" carries a version
YAML_COPIES = ("plugin.yaml", "plugins/substrate-hermes/plugin.yaml")
PY_COPIES = (
    "plugins/substrate-claude/scripts/substrate_sync.py",
    "plugins/substrate-codex/scripts/substrate_sync.py",
    "plugins/substrate-hermes/src/substrate/client.py",
)
README_PIN_COPIES = ("plugins/substrate-hermes/README.md",)  # "... SHA of the vX.Y.Z tag"

_JSON_VERSION = re.compile(r'("version": ")[^"]*(")')
_YAML_VERSION = re.compile(r"(?m)^version: .*$")
_PY_VERSION = re.compile(r'(?m)^PLUGIN_VERSION = "[^"]*"$')
_README_PIN = re.compile(r"(SHA of the v)[0-9.]+( tag)")


def read_version(root: Path = ROOT) -> str:
    return (root / "VERSION").read_text(encoding="utf-8").strip()


def _set_json(text: str, version: str) -> tuple[str, int]:
    return _JSON_VERSION.subn(lambda m: m.group(1) + version + m.group(2), text)


def _set_yaml(text: str, version: str) -> tuple[str, int]:
    return _YAML_VERSION.subn("version: " + version, text)


def _set_py(text: str, version: str) -> tuple[str, int]:
    return _PY_VERSION.subn('PLUGIN_VERSION = "%s"' % version, text)


def _set_readme(text: str, version: str) -> tuple[str, int]:
    return _README_PIN.subn(lambda m: m.group(1) + version + m.group(2), text)


def check(root: Path = ROOT) -> list[str]:
    """Return one line per problem. An empty list means every copy matches VERSION."""
    version = read_version(root)
    problems: list[str] = []
    if not SEMVER.match(version):
        return ["VERSION is not MAJOR.MINOR.PATCH: %r" % version]
    for rel in JSON_COPIES:
        found = json.loads((root / rel).read_text(encoding="utf-8")).get("version")
        if found != version:
            problems.append("%s: version %r, VERSION %r" % (rel, found, version))
    marketplace = json.loads((root / MARKETPLACE_COPY).read_text(encoding="utf-8"))
    entries = marketplace.get("plugins") or []
    if not entries:
        problems.append("%s: no plugin entries" % MARKETPLACE_COPY)
    for entry in entries:
        if entry.get("version") != version:
            problems.append("%s: %s version %r, VERSION %r"
                            % (MARKETPLACE_COPY, entry.get("name"), entry.get("version"), version))
    for rel in YAML_COPIES:
        found = re.findall(r"(?m)^version: (\S+)$", (root / rel).read_text(encoding="utf-8"))
        if found != [version]:
            problems.append("%s: version %r, VERSION %r" % (rel, found, version))
    for rel in PY_COPIES:
        found = re.findall(r'(?m)^PLUGIN_VERSION = "([^"]*)"$', (root / rel).read_text(encoding="utf-8"))
        if found != [version]:
            problems.append("%s: PLUGIN_VERSION %r, VERSION %r" % (rel, found, version))
    for rel in README_PIN_COPIES:
        found = re.findall(r"SHA of the v([0-9.]+) tag", (root / rel).read_text(encoding="utf-8"))
        if not found or any(pin != version for pin in found):
            problems.append("%s: release pin %r, VERSION %r" % (rel, found, version))
    return problems


def write(root: Path = ROOT) -> list[str]:
    """Copy VERSION into every copy. Returns the files that changed."""
    version = read_version(root)
    if not SEMVER.match(version):
        raise SystemExit("VERSION is not MAJOR.MINOR.PATCH: %r" % version)
    changed: list[str] = []
    plans = [(rel, _set_json) for rel in JSON_COPIES]
    plans += [(MARKETPLACE_COPY, _set_json)]
    plans += [(rel, _set_yaml) for rel in YAML_COPIES]
    plans += [(rel, _set_py) for rel in PY_COPIES]
    plans += [(rel, _set_readme) for rel in README_PIN_COPIES]
    for rel, setter in plans:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        new, count = setter(text, version)
        if count == 0:
            raise SystemExit("no version field found in %s" % rel)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(rel)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--write", action="store_true", help="set every copy from VERSION")
    args = parser.parse_args(argv)
    if args.write:
        for rel in write():
            print("updated " + rel)
    problems = check()
    for line in problems:
        print(line, file=sys.stderr)
    if problems:
        return 1
    print("all version copies match VERSION %s" % read_version())
    return 0


if __name__ == "__main__":
    sys.exit(main())
