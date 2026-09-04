"""Shared helpers for the Substrate Claude Code hook scripts. Stdlib only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)


def read_input() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        value = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, UnicodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def extract_text(content: Any) -> str:
    """Flatten a Claude Code transcript content block to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif isinstance(block, str) and block:
                parts.append(block)
        return "\n".join(parts)
    return ""


def load_transcript(path: Any) -> list[dict[str, str]]:
    """Load user/assistant text rows from a Claude Code JSONL transcript."""
    if not isinstance(path, str) or not path:
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            lines = stream.read().splitlines()[-4096:]
    except OSError:
        return []
    messages: list[dict[str, str]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind not in ("user", "assistant"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = extract_text(message.get("content"))
        if text:
            messages.append({"role": role, "content": text})
    return messages


def last_texts(messages: list[dict[str, str]]) -> tuple[str, str]:
    """Return the last user text and last assistant text after it."""
    user = ""
    assistant = ""
    for item in messages:
        if item["role"] == "user":
            user = item["content"]
            assistant = ""
        elif item["role"] == "assistant" and user:
            assistant = item["content"]
    first_user = next((item["content"] for item in messages if item["role"] == "user"), "")
    return (user or first_user, assistant)


def spawn_detached(args: list[str]) -> None:
    """Start a background worker without blocking the hook. Never raises."""
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass
