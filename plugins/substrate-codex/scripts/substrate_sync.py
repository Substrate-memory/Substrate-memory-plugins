#!/usr/bin/env python3
"""Substrate transcript sync: local Claude/Codex history to memory_import items.

Reads the host's own local session transcripts and prints ``memory_import``
items in batches (one JSON batch per line). Standard library only.

Byte-identical in ``substrate-claude`` and ``substrate-codex``: keep this
file host-neutral. Host differences are flags, not forks.

Usage:
    substrate_sync.py --host claude|codex --list
    substrate_sync.py --host claude|codex --session <id> [--after-index N]
                      [--origin catchup|history_replay]

Paths:
    Claude transcripts: $CLAUDE_CONFIG_DIR/projects/**/*.jsonl
        (fallback ~/.claude/projects/**/*.jsonl).
    Codex rollouts: $CODEX_HOME/sessions/**/*.jsonl
        (fallback ~/.codex/sessions/**/*.jsonl).

The agent passes each printed batch to the ``memory_import`` MCP tool.
This script never prints credentials and never contacts the network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

MAX_ITEMS_PER_BATCH = 64
MAX_BATCH_BYTES = 240 * 1024
MAX_TOOL_CALL_BYTES = 4096
MAX_TOOL_RESULT_BYTES = 8192
MAX_ARGS_PREVIEW_BYTES = 1024

RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")

_TEXT_SECRET_RE = re.compile(
    r"(?i)(\b(?:authorization|api[_-]?key|access[_-]?token|token|password|secret)\b\s*[:=]\s*)"
    r"(?:bearer\s+)?[^\s,;]+"
)
_SK_RE = re.compile(r"\bsk_[A-Za-z0-9_-]{8,}\b")
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|private[_-]?key)"
)
SAFE_INT = 2 ** 53 - 1


def _clean_unicode(value: str) -> str:
    return value.encode("utf-8", "replace").decode("utf-8")


def _clip_utf8(value: str, maximum: int) -> str:
    value = _clean_unicode(value)
    raw = value.encode("utf-8")
    if len(raw) <= maximum:
        return value
    return raw[:maximum].decode("utf-8", "ignore")


def _redact_text(value: str) -> str:
    value = _clean_unicode(value)
    value = _TEXT_SECRET_RE.sub(r"\1[REDACTED]", value)
    return _SK_RE.sub("[REDACTED]", value)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return str(value)


def _bounded_text(value: Any, maximum: int) -> str:
    return _clip_utf8(_redact_text(_as_text(value)), maximum)


def _safe_value(value: Any, depth: int = 0) -> Any:
    """Redact and bound arbitrary tool arguments into canonical JSON values."""
    if depth >= 8:
        return "[TRUNCATED]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value if abs(value) <= SAFE_INT else str(value)
    if isinstance(value, float):
        return str(value) if math.isfinite(value) else "[NONFINITE]"
    if isinstance(value, str):
        return _bounded_text(value, 8192)
    if isinstance(value, dict):
        result = {}
        for key, child in list(value.items())[:128]:
            name = _clip_utf8(str(key), 256)
            result[name] = "[REDACTED]" if _SENSITIVE_KEY_RE.search(name) else _safe_value(
                child, depth=depth + 1
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1) for item in value[:128]]
    return _bounded_text(value, 1024)


def _canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _valid_timestamp(value: Any) -> Optional[str]:
    if isinstance(value, str) and RFC3339_RE.match(value):
        return value
    return None


def _tool_call(call_id: Any, name: Any, raw_args: Any, ordinal: int) -> Optional[Dict[str, Any]]:
    cid = _clip_utf8(_redact_text(_as_text(call_id)), 128) if call_id else "call-%d" % ordinal
    tname = _clip_utf8(_redact_text(_as_text(name)), 128) if name else ""
    if not cid or not tname:
        return None
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except (ValueError, TypeError):
            raw_args = {"value": raw_args}
    if not isinstance(raw_args, dict):
        raw_args = {"value": raw_args}
    args = _safe_value(raw_args)
    assert isinstance(args, dict)
    encoded = _canonical_bytes(args)
    if len(encoded) <= MAX_TOOL_CALL_BYTES:
        return {"id": cid, "tool_name": tname, "args": args}
    return {
        "id": cid,
        "tool_name": tname,
        "args_truncated": True,
        "args_sha256": hashlib.sha256(encoded).hexdigest(),
        "args_preview": _clip_utf8(encoded.decode("utf-8"), MAX_ARGS_PREVIEW_BYTES),
    }


def _tool_message(
    index: int,
    content: Any,
    call_id: Any = "",
    name: Any = "",
    timestamp: Any = None,
) -> Dict[str, Any]:
    full = _redact_text(_as_text(content))
    raw = full.encode("utf-8")
    excerpt = _clip_utf8(full, MAX_TOOL_RESULT_BYTES)
    message = {
        "index": index,
        "role": "tool",
        "content": excerpt,
        "result_digest": hashlib.sha256(raw).hexdigest(),
        "result_bytes": min(len(raw), SAFE_INT),
    }  # type: Dict[str, Any]
    if len(excerpt.encode("utf-8")) != len(raw):
        message["result_truncated"] = True
    cid = _clip_utf8(_redact_text(_as_text(call_id)), 128) if call_id else ""
    tname = _clip_utf8(_redact_text(_as_text(name)), 128) if name else ""
    if cid:
        message["tool_call_id"] = cid
    if tname:
        message["tool_name"] = tname
    ts = _valid_timestamp(timestamp)
    if ts:
        message["timestamp"] = ts
    return message


def _text_message(
    index: int, role: str, content: Any, timestamp: Any = None
) -> Dict[str, Any]:
    message = {
        "index": index,
        "role": role,
        "content": _bounded_text(content, 32768),
    }  # type: Dict[str, Any]
    ts = _valid_timestamp(timestamp)
    if ts:
        message["timestamp"] = ts
    return message


# ---------------------------------------------------------------------------
# Transcript readers: each returns (session_id, turns) where a turn is a dict
# with keys: texts (list of (role, text, timestamp)), tool_calls (list of
# (call_id, name, args)), tools (list of (content, call_id, name, timestamp)),
# created_at. Message indices are assigned later, globally per session.
# ---------------------------------------------------------------------------

def _read_jsonl_lines(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _claude_block_text(block: Any) -> str:
    if not isinstance(block, dict):
        return ""
    btype = block.get("type")
    if btype == "text":
        text = block.get("text", "")
        return text if isinstance(text, str) else _as_text(text)
    return ""


def _parse_claude_file(path: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Parse one Claude transcript file into ordered turns."""
    session_id = ""
    turns = []  # type: List[Dict[str, Any]]
    current = None  # type: Optional[Dict[str, Any]]
    tool_names = {}  # type: Dict[str, str]

    def new_turn() -> Dict[str, Any]:
        return {"texts": [], "tool_calls": [], "tools": [], "created_at": ""}

    def ensure_turn() -> Dict[str, Any]:
        nonlocal current
        if current is None:
            current = new_turn()
            turns.append(current)
        return current

    for record in _read_jsonl_lines(path):
        if not isinstance(record, dict):
            continue
        rtype = record.get("type")
        if not session_id:
            for key in ("sessionId", "session_id"):
                value = record.get(key)
                if isinstance(value, str) and value:
                    session_id = value
                    break
        if rtype == "user":
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            ts = record.get("timestamp")
            if isinstance(content, str):
                text = content.strip()
                if text:
                    current = new_turn()
                    turns.append(current)
                    current["texts"].append(("user", text, ts))
            elif isinstance(content, list):
                texts = []
                results = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_result":
                        results.append(block)
                    elif btype == "text":
                        text = _claude_block_text(block)
                        if text.strip():
                            texts.append(text)
                if texts:
                    current = new_turn()
                    turns.append(current)
                    current["texts"].append(("user", "\n".join(texts), ts))
                    for block in results:
                        turn = ensure_turn()
                        turn["tools"].append(
                            (
                                block.get("content", ""),
                                block.get("tool_use_id", ""),
                                tool_names.get(block.get("tool_use_id", ""), ""),
                                ts,
                            )
                        )
                else:
                    for block in results:
                        turn = ensure_turn()
                        turn["tools"].append(
                            (
                                block.get("content", ""),
                                block.get("tool_use_id", ""),
                                tool_names.get(block.get("tool_use_id", ""), ""),
                                ts,
                            )
                        )
        elif rtype == "assistant":
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            ts = record.get("timestamp")
            texts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = _claude_block_text(block)
                    if text.strip():
                        texts.append(text)
                elif btype == "tool_use":
                    turn = ensure_turn()
                    call_id = block.get("id", "")
                    name = block.get("name", "")
                    if isinstance(call_id, str) and call_id:
                        tool_names[call_id] = name if isinstance(name, str) else ""
                    turn["tool_calls"].append((call_id, name, block.get("input", {})))
            if texts:
                turn = ensure_turn()
                turn["texts"].append(("assistant", "\n".join(texts), ts))
    if not session_id:
        session_id = os.path.splitext(os.path.basename(path))[0]
    return session_id, [t for t in turns if t["texts"] or t["tool_calls"] or t["tools"]]


def _codex_text_parts(content: Any) -> str:
    texts = []
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type", "")
            if ptype in ("input_text", "output_text", "text"):
                text = part.get("text", "")
                texts.append(text if isinstance(text, str) else _as_text(text))
    return "".join(texts)


def _parse_codex_file(path: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Parse one Codex rollout JSONL file into ordered turns."""
    session_id = ""
    turns = []  # type: List[Dict[str, Any]]
    current = None  # type: Optional[Dict[str, Any]]

    def new_turn() -> Dict[str, Any]:
        return {"texts": [], "tool_calls": [], "tools": [], "created_at": ""}

    def ensure_turn() -> Dict[str, Any]:
        nonlocal current
        if current is None:
            current = new_turn()
            turns.append(current)
        return current

    for record in _read_jsonl_lines(path):
        if not isinstance(record, dict):
            continue
        if not session_id:
            meta = record.get("payload") if isinstance(record.get("payload"), dict) else None
            if record.get("type") == "session_meta" and meta:
                for key in ("session_id", "id"):
                    value = meta.get(key)
                    if isinstance(value, str) and value:
                        session_id = value
                        break
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")
        ts = record.get("timestamp")
        if ptype == "message":
            role = payload.get("role")
            if role == "user":
                text = _codex_text_parts(payload.get("content")).strip()
                if text:
                    current = new_turn()
                    turns.append(current)
                    current["texts"].append(("user", text, ts))
            elif role == "assistant":
                text = _codex_text_parts(payload.get("content")).strip()
                if text:
                    turn = ensure_turn()
                    turn["texts"].append(("assistant", text, ts))
        elif ptype in ("function_call", "custom_tool_call"):
            turn = ensure_turn()
            turn["tool_calls"].append(
                (payload.get("id", payload.get("call_id", "")), payload.get("name", ""),
                 payload.get("arguments", payload.get("input", {})))
            )
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            turn = ensure_turn()
            output = payload.get("output", payload.get("content", ""))
            turn["tools"].append(
                (output, payload.get("call_id", payload.get("id", "")),
                 payload.get("name", ""), ts)
            )
    if not session_id:
        base = os.path.splitext(os.path.basename(path))[0]
        session_id = base.replace("rollout-", "")
    return session_id, [t for t in turns if t["texts"] or t["tool_calls"] or t["tools"]]


PARSERS = {"claude": _parse_claude_file, "codex": _parse_codex_file}


def _base_dir(host: str) -> str:
    home = os.path.expanduser("~")
    if host == "claude":
        override = os.environ.get("CLAUDE_CONFIG_DIR")
        if override:
            return os.path.join(override, "projects")
        return os.path.join(home, ".claude", "projects")
    override = os.environ.get("CODEX_HOME")
    if override:
        return os.path.join(override, "sessions")
    return os.path.join(home, ".codex", "sessions")


def _iter_transcript_files(host: str):
    root = _base_dir(host)
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".jsonl"):
                found.append(os.path.join(dirpath, name))
    found.sort()
    return found


def _build_envelopes(
    session_id: str,
    turns: List[Dict[str, Any]],
    origin: str,
    after_index: int,
) -> List[Dict[str, Any]]:
    """Build capture_turn import items with global message indices."""
    # First pass: count messages per turn so indices are stable.
    items = []
    index = 0
    for ordinal, turn in enumerate(turns):
        messages = []  # type: List[Dict[str, Any]]
        cursor = index
        assistant_texts = []
        assistant_ts = None
        calls = []
        for role, text, ts in turn["texts"]:
            if role == "assistant":
                assistant_texts.append(text)
                assistant_ts = assistant_ts or ts
            else:
                messages.append(_text_message(cursor, role, text, ts))
                cursor += 1
        for ordinal_call, (call_id, name, args) in enumerate(turn["tool_calls"]):
            call = _tool_call(call_id, name, args, ordinal_call)
            if call is not None:
                calls.append(call)
        if assistant_texts or calls:
            assistant = _text_message(cursor, "assistant", "\n".join(assistant_texts),
                                      assistant_ts)
            if calls:
                assistant["tool_calls"] = calls
            messages.append(assistant)
            cursor += 1
        for content, call_id, name, ts in turn["tools"]:
            messages.append(_tool_message(cursor, content, call_id, name, ts))
            cursor += 1
        start, end = index, cursor
        index = cursor
        if not messages:
            continue
        created = turn.get("created_at") or None
        for message in messages:
            if isinstance(message.get("timestamp"), str):
                created = message["timestamp"]
        item = {
            "kind": "capture_turn",
            "session_id": session_id,
            "offset": {"start": start, "end": end},
            "capture_origin": origin,
            "batch_id": "",
            "speaker": {"id": "local", "role": "owner", "display": ""},
            "created_at": created or _utc_now(),
            "payload": {"turn_id": "t%05d" % ordinal, "messages": messages},
        }  # type: Dict[str, Any]
        if end > after_index:
            items.append(item)
    return items


def _cmd_list(host: str) -> int:
    for path in _iter_transcript_files(host):
        try:
            session_id, turns = PARSERS[host](path)
        except Exception:
            continue
        count = sum(1 for t in turns if t["texts"] or t["tool_calls"] or t["tools"])
        messages = sum(
            len(t["texts"]) + (1 if t["tool_calls"] else 0) + len(t["tools"]) for t in turns
        )
        print(
            json.dumps(
                {"session_id": session_id, "turns": count, "messages": messages,
                 "path": path},
                ensure_ascii=False,
            )
        )
    return 0


def _cmd_session(host: str, session: str, after_index: int, origin: str) -> int:
    match = None
    for path in _iter_transcript_files(host):
        try:
            session_id, turns = PARSERS[host](path)
        except Exception:
            continue
        if session_id == session or os.path.splitext(os.path.basename(path))[0] == session:
            match = (session_id, turns)
            break
    if match is None:
        print(json.dumps({"error": "session_not_found", "session_id": session}))
        return 1
    session_id, turns = match
    items = _build_envelopes(session_id, turns, origin, after_index)
    batch = []  # type: List[Dict[str, Any]]
    batch_bytes = len(_canonical_bytes({"items": []}))
    for item in items:
        size = len(_canonical_bytes(item)) + (1 if batch else 0)
        if batch and (len(batch) >= MAX_ITEMS_PER_BATCH or
                      batch_bytes + size > MAX_BATCH_BYTES):
            print(json.dumps({"items": batch}, ensure_ascii=False))
            batch = []
            batch_bytes = len(_canonical_bytes({"items": []}))
        batch.append(item)
        batch_bytes += size
    if batch:
        print(json.dumps({"items": batch}, ensure_ascii=False))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Substrate transcript sync")
    parser.add_argument("--host", required=True, choices=("claude", "codex"))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--session", default="")
    parser.add_argument("--after-index", type=int, default=0)
    parser.add_argument("--origin", default="catchup",
                        choices=("catchup", "history_replay"))
    args = parser.parse_args(argv)
    if args.list:
        return _cmd_list(args.host)
    if args.session:
        return _cmd_session(args.host, args.session, args.after_index, args.origin)
    parser.error("need --list or --session <id>")
    return 2


if __name__ == "__main__":
    sys.exit(main())
