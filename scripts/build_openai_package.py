#!/usr/bin/env python3
"""Build and validate the OpenAI directory package (ChatGPT and Codex listing, MCP only).

Reads openai/plugin.json and openai/mcp.json, takes assets/logo.png from the repository
root, checks OpenAI's submission limits, and writes dist/substrate-openai-<version>.zip
(fixed timestamps, so two builds of one tree are byte-identical). The version must equal
VERSION. Upload the ZIP in the OpenAI portal when the listing text or tool set changed.

Usage:
  python3 scripts/build_openai_package.py            validate and write the ZIP
  python3 scripts/build_openai_package.py --check    validate only

Standard library only. tests/test_openai_package.py and CI run it.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGO_REF = "./assets/logo.png"
FIXED_TIMESTAMP = (2020, 1, 1, 0, 0, 0)
CORE_TOOLS = {
    "memory_search", "memory_expand", "memory_evidence",
    "memory_shares", "memory_remember", "memory_forget",
}
CATEGORIES = {
    "Productivity", "Creativity", "Developer Tools", "Business & Operations", "Data & Analytics",
    "Communication", "Education & Research", "Security", "Finance", "Healthcare", "Travel",
    "Entertainment", "Other",
}
# OpenAI's listing rules: no other AI assistants or platforms, no pricing, no secrets.
OTHER_PLATFORMS = ("Claude", "Gemini", "Cursor", "Copilot", "Hermes", "ChatGPT", "Codex", "GPT",
                   "OpenAI", "Anthropic")
PRICING = ("$", "/month", "trial", "price", "pricing", "discount", "subscription", "free")
FORBIDDEN = ("clientSecret", "test_credentials", "reviewer_instructions", "hooks", '"apps"',
             "password", "skills")
CONTROL = re.compile("[\x00-\x09\x0b-\x1f\x7f ​-‏⁠﻿]")


def _png_size(path: Path) -> tuple[int, int] | None:
    head = path.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def _luminance(color: str) -> float:
    rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def validate(root: Path = ROOT) -> list[str]:
    """Return one line per problem. An empty list means the package is valid."""
    logo = root / "assets" / "logo.png"
    plugin = json.loads((root / "openai" / "plugin.json").read_text(encoding="utf-8"))
    mcp = json.loads((root / "openai" / "mcp.json").read_text(encoding="utf-8"))
    openai = plugin["extensions"]["com.openai"]
    ui = openai["interface"]
    err: list[str] = []

    def need(ok: bool, msg: str) -> None:
        if not ok:
            err.append(msg)

    def line(text: str) -> bool:
        return bool(text.strip()) and "\n" not in text and "\r" not in text and not CONTROL.search(text)

    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    need(plugin["version"] == version, "version %r must equal VERSION %r" % (plugin["version"], version))
    need(bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", plugin["name"])) and len(plugin["name"]) <= 64,
         "name must be kebab-case, <= 64")
    need(0 < len(plugin["description"]) <= 1024 and not CONTROL.search(plugin["description"]),
         "description <= 1024")
    need(0 < len(plugin["author"]["name"]) <= 120, "author.name <= 120")
    for key, limit in (("displayName", 30), ("shortDescription", 30), ("developerName", 80)):
        need(line(ui[key]) and len(ui[key]) <= limit, "%s: one line, <= %d" % (key, limit))
    need(not re.search(r"(MCP|Plugin)\s*$", ui["displayName"]), "displayName must not end in MCP or Plugin")
    long = ui["longDescription"]
    need(bool(long.strip()) and len(long) <= 4000 and not CONTROL.search(long.replace("\n", "")),
         "longDescription: 1..4000, no control characters")
    for word in PRICING:
        need(word not in long.lower(), "longDescription mentions pricing (%r)" % word)
    text = plugin["description"] + long + ui["shortDescription"] + ui["displayName"]
    for word in OTHER_PLATFORMS:
        need(word not in text, "listing names another AI platform (%r)" % word)
    need("—" not in long, "longDescription has an em dash")
    need(ui["category"] in CATEGORIES, "category not in OpenAI's list")
    need(len(ui["capabilities"]) <= 20 and all(line(c) and len(c) <= 120 for c in ui["capabilities"]),
         "capabilities: <= 20 items, <= 120 chars")
    prompts = ui["defaultPrompt"]
    need(len(prompts) <= 3 and all(line(p) and len(p) <= 128 and "@" not in p for p in prompts)
         and len({" ".join(p.split()).lower() for p in prompts}) == len(prompts),
         "defaultPrompt: <= 3, <= 128 chars, unique, no @")
    for key in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        need(ui[key].startswith("https://") and len(ui[key]) <= 1024, "%s must be https, <= 1024" % key)
    need(plugin["homepage"].startswith("https://"), "homepage must be https")
    color_ok = bool(re.fullmatch(r"#[0-9A-Fa-f]{6}", ui["brandColor"]))
    need(color_ok, "brandColor must be #RRGGBB")
    if color_ok:
        ratio = 1.05 / (_luminance(ui["brandColor"]) + 0.05)
        need(ratio >= 2, "brandColor contrast against white is %.2f, needs >= 2" % ratio)
    for key in ("logo", "composerIcon"):
        need(ui[key] == LOGO_REF, "%s must be %s (the build takes assets/logo.png)" % (key, LOGO_REF))
    size = _png_size(logo) if logo.is_file() else None
    need(size is not None and size[0] == size[1] and 48 <= size[0] <= 4096
         and logo.stat().st_size <= 5 * 1024 * 1024,
         "assets/logo.png must be a square PNG, 48..4096 px, <= 5 MiB")

    servers = list(mcp["mcpServers"].values())
    need(len(servers) == 1 and servers[0]["type"] == "streamable-http"
         and servers[0]["url"].startswith("https://") and "headers" not in servers[0],
         "exactly one https streamable-http server and no headers")

    tests = openai["review"]["test_cases"]
    need(len(tests["positive"]) == 5 and len(tests["negative"]) == 3, "needs 5 positive and 3 negative cases")
    for case in tests["positive"]:
        used = {t.strip() for t in case["tools_triggered"].split(",")}
        need(bool(used) and used <= CORE_TOOLS, "test case %r uses non-core tools" % case["description"])
    blob = json.dumps({k: v for k, v in plugin.items() if k != "extensions"}) + json.dumps(ui) + json.dumps(mcp)
    for word in FORBIDDEN:
        need(word not in blob, "forbidden content: %s" % word)
    return err


def build(root: Path = ROOT) -> Path:
    """Write dist/substrate-openai-<version>.zip. Run validate() first."""
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    out = root / "dist" / ("substrate-openai-%s.zip" % version)
    out.parent.mkdir(exist_ok=True)
    members = (("plugin.json", root / "openai" / "plugin.json"), ("mcp.json", root / "openai" / "mcp.json"),
               ("assets/logo.png", root / "assets" / "logo.png"))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in members:
            info = zipfile.ZipInfo(name, FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="validate only, write nothing")
    args = parser.parse_args(argv)
    problems = validate()
    for text in problems:
        print(text, file=sys.stderr)
    if problems:
        return 1
    if args.check:
        print("OpenAI package is valid")
    else:
        out = build()
        print("wrote %s (%d bytes)" % (out.relative_to(ROOT), out.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
