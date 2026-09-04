"""Claude transcript bridge for the Cowork hooks (stdlib only).

Claude Code/Cowork session transcripts are JSONL: one object per line with
``type`` (``"user"``/``"assistant"``/...) and ``message.content`` as either a
string or a list of blocks (``text``, ``tool_use``, ``tool_result``). This
module converts that shape into the Hermes-shaped message lists the vendored
runtime already understands. Best effort and bounded; any failure yields ``[]``
so hooks fail closed. Tokens are never printed or logged.
"""

from __future__ import annotations

import json
from typing import Any

_MAX_LINES = 400
_MAX_TEXT_BYTES = 32_768


def _clip(value: Any, maximum: int = _MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except Exception:
            value = str(value)
    raw = value.encode("utf-8", "replace")
    if len(raw) <= maximum:
        return value
    return raw[:maximum].decode("utf-8", "ignore")


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    kind = block.get("type")
    if kind == "text" and isinstance(block.get("text"), str):
        return block["text"]
    return ""


def _read_lines(path: str) -> list[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            lines = stream.readlines()[-_MAX_LINES:]
    except (OSError, ValueError):
        return []
    items: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            items.append(value)
    return items


def history_for_context(transcript_path: str) -> list[dict[str, str]]:
    """Simple user/assistant text history for pre-turn context requests."""
    history: list[dict[str, str]] = []
    for item in _read_lines(transcript_path):
        kind = item.get("type")
        if kind not in ("user", "assistant"):
            continue
        message = item.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            text = "".join(_block_text(block) for block in content).strip()
        elif isinstance(content, str):
            text = content
        else:
            continue
        if text:
            history.append({"role": kind, "content": _clip(text, 4096)})
    return history[-6:]


def _assistant_item(text: str, tool_calls: list[dict[str, Any]], timestamp: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"role": "assistant", "content": _clip(text)}
    if tool_calls:
        item["tool_calls"] = tool_calls
    if isinstance(timestamp, str):
        item["timestamp"] = timestamp
    return item


def messages_for_capture(transcript_path: str) -> list[dict[str, Any]]:
    """Full-fidelity Hermes-shaped messages (text, tool calls, tool results)."""
    messages: list[dict[str, Any]] = []
    for item in _read_lines(transcript_path):
        kind = item.get("type")
        if kind not in ("user", "assistant"):
            continue
        message = item.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        timestamp = item.get("timestamp")
        if kind == "user":
            if isinstance(content, str):
                if content.strip():
                    messages.append({"role": "user", "content": _clip(content)})
                continue
            if not isinstance(content, list):
                continue
            texts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        result_text = "".join(_block_text(part) for part in result_content)
                    elif isinstance(result_content, str):
                        result_text = result_content
                    else:
                        result_text = _clip(result_content, 8192)
                    tool_item: dict[str, Any] = {
                        "role": "tool",
                        "content": _clip(result_text, 8192),
                    }
                    if isinstance(block.get("tool_use_id"), str):
                        tool_item["tool_call_id"] = block["tool_use_id"][:128]
                    if isinstance(timestamp, str):
                        tool_item["timestamp"] = timestamp
                    messages.append(tool_item)
                else:
                    text = _block_text(block)
                    if text:
                        texts.append(text)
            joined = "".join(texts).strip()
            if joined:
                messages.append({"role": "user", "content": _clip(joined)})
        else:
            if isinstance(content, str):
                messages.append(_assistant_item(content, [], timestamp))
                continue
            if not isinstance(content, list):
                continue
            texts = []
            calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    call: dict[str, Any] = {
                        "id": str(block.get("id", ""))[:128],
                        "tool_name": str(block.get("name", ""))[:128],
                        "args": block.get("input", {}),
                    }
                    if call["id"] and call["tool_name"]:
                        calls.append(call)
                else:
                    text = _block_text(block)
                    if text:
                        texts.append(text)
            joined = "".join(texts)
            if joined.strip() or calls:
                messages.append(_assistant_item(joined, calls, timestamp))
    return messages[-512:]
