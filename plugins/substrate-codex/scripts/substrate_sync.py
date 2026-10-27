#!/usr/bin/env python3
"""Substrate transcript sync: local Claude/Codex history into Substrate memory.

Reads the host's own local session transcripts, builds redacted
``memory_import`` items, and either prints them (catch-up) or uploads them
straight to Substrate with a short-lived import ticket (history import), so
bulk history never passes through the model. Standard library only.

Byte-identical in ``substrate-claude`` and ``substrate-codex``: keep this
file host-neutral. Host differences are flags, not forks.

Usage:
    substrate_sync.py --host H --list [--exclude-session ID]
    substrate_sync.py --host H --session ID [--after-index N] [--origin O]
    substrate_sync.py --host H --preview [--exclude-session ID]
    substrate_sync.py --host H --upload (--all | --session ID...)
                      --ticket-file PATH [--exclude-session ID]
                      [--background] [--force]
    substrate_sync.py --host H --status [--wait SECONDS]
    substrate_sync.py --host H --offer-check          (UserPromptSubmit hook)
    substrate_sync.py --host H --record-decision yes|no|picked|none
    substrate_sync.py --host H --record-connected     (after memory_search worked)
    substrate_sync.py --host H --pause SECONDS        (wait <= 60 s, e.g. for sign-in)
    substrate_sync.py --host H --signin-start         (PostToolUse hook: authenticate ran)
    substrate_sync.py --host H --stop-check           (Stop hook: keep a pending sign-in polling)
    (every mode also takes --data-dir DIR)

Upload reads the import ticket from --ticket-file: a file the agent wrote with
its file-write tool (the ticket from ``memory_import_ticket``). The script
reads it, makes it 0600 and deletes it at once; a ticket is never taken from
argv or the environment, so it never shows up in a command line. Background
runs hand the ticket to the child process over stdin. The endpoint comes from
SUBSTRATE_MCP_URL (not a secret).

Paths:
    Claude transcripts: $CLAUDE_CONFIG_DIR/projects/**/*.jsonl
        (fallback ~/.claude/projects; subagent transcripts are skipped).
    Codex rollouts: $CODEX_HOME/sessions/**/*.jsonl (fallback ~/.codex/sessions).
    State (decision, status, progress): $CLAUDE_CONFIG_DIR|~/.claude or
        $CODEX_HOME|~/.codex, then /substrate-memory/ (or $SUBSTRATE_DATA_DIR).

This script never prints credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import errno
import json
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

MAX_ITEMS_PER_BATCH = 64
MAX_BATCH_BYTES = 240 * 1024
MAX_TOOL_CALL_BYTES = 4096
MAX_TOOL_RESULT_BYTES = 8192
MAX_ARGS_PREVIEW_BYTES = 1024

RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")

# Contract section 6 (keep in step with agent-api/redaction.mjs; both must pass
# contract/redaction-fixtures.json). Rule 1: NAME ends in a credential word.
_TEXT_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]*(?:authorization|api[_-]?key|secret(?:[_-]access[_-]key)?|access[_-]?token|token|password|passwd|pwd|private[_-]?key|client[_-]?secret|auth)(?![A-Za-z0-9_])\\?[\"']?\s*(?::|=(?!=))\s*)"
    r"(?:(?:bearer|basic)\s+)?(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)",
    re.IGNORECASE,
)
# Rule 2: PEM private key blocks, passwords in scheme://user:pw@host, and secret shapes.
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|(?![\s\S]))"
)
_URL_PASSWORD_RE = re.compile(r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]*:\/\/[^\s:/@\"']+:)[^\s/\"',;]+(?=@)")
_SECRET_TOKEN_RES = (
    re.compile(r"(?<![A-Za-z0-9_])sk_[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_-])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9_-])gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_-])github_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"(?<![A-Za-z0-9_-])xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?<![A-Za-z0-9_-])(?:rk|pk)_(?:live|test)_[A-Za-z0-9]{8,}"),
    re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
)
# Rule 3: sensitive keys (substring), plus keys ending in auth or pwd.
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|private[_-]?key)|(?:auth|pwd)$"
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
    value = _PEM_PRIVATE_KEY_RE.sub("[REDACTED]", value)
    value = _TEXT_SECRET_RE.sub(r"\1[REDACTED]", value)
    value = _URL_PASSWORD_RE.sub(r"\1[REDACTED]", value)
    for pattern in _SECRET_TOKEN_RES:
        value = pattern.sub("[REDACTED]", value)
    return value


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
        if record.get("isSidechain") is True:
            # Subagent (sidechain) messages repeat the parent's session id;
            # their result already reaches the parent as a tool result.
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
    for dirpath, dirnames, filenames in os.walk(root):
        if host == "claude":
            # Subagent transcripts repeat the parent's session id; their
            # results already arrive in the parent as tool results.
            dirnames[:] = [d for d in dirnames if d != "subagents"]
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            if host == "claude" and name.startswith("agent-"):
                continue  # older/flattened subagent transcripts
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


# ---------------------------------------------------------------------------
# Item fitting: a turn must satisfy the server limits (one event <= 262144
# bytes, <= 64 tool calls per message, <= 128 messages per turn). Items that
# already fit are returned unchanged, so their deterministic ids never move.
# ---------------------------------------------------------------------------

MAX_ITEM_BYTES = MAX_BATCH_BYTES - 1024
MAX_CALLS_PER_MESSAGE = 64
MAX_MESSAGES_PER_ITEM = 128  # server redaction bound: longer arrays arrive as "[TRUNCATED]"


def _item_fits(item: Dict[str, Any]) -> bool:
    messages = item["payload"]["messages"]
    if len(messages) > MAX_MESSAGES_PER_ITEM:
        return False
    for message in messages:
        if len(message.get("tool_calls", ())) > MAX_CALLS_PER_MESSAGE:
            return False
    return len(_canonical_bytes(item)) <= MAX_ITEM_BYTES


def _shrink_tool_results(item: Dict[str, Any], maximum: int) -> None:
    for message in item["payload"]["messages"]:
        if message.get("role") != "tool":
            continue
        content = message.get("content", "")
        clipped = _clip_utf8(content, maximum)
        if clipped != content:
            message["content"] = clipped
            message["result_truncated"] = True


def _shrink_tool_calls(item: Dict[str, Any]) -> None:
    for message in item["payload"]["messages"]:
        calls = message.get("tool_calls")
        if not calls:
            continue
        smaller = []
        for call in calls[:MAX_CALLS_PER_MESSAGE]:
            if "args" in call:
                encoded = _canonical_bytes(call["args"])
                call = {
                    "id": call["id"],
                    "tool_name": call["tool_name"],
                    "args_truncated": True,
                    "args_sha256": hashlib.sha256(encoded).hexdigest(),
                    "args_preview": _clip_utf8(encoded.decode("utf-8"), 256),
                }
            elif "args_preview" in call:
                call = dict(call, args_preview=_clip_utf8(call["args_preview"], 256))
            smaller.append(call)
        message["tool_calls"] = smaller


def _split_messages(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Split one turn into consecutive parts that each fit (offsets stay exact)."""
    parts = []  # type: List[Dict[str, Any]]
    chunk = []  # type: List[Dict[str, Any]]
    base = dict(item, payload=dict(item["payload"], messages=[]))
    overhead = len(_canonical_bytes(base)) + 64

    def flush() -> None:
        if not chunk:
            return
        number = len(parts) + 1
        part = json.loads(json.dumps(base))
        part["payload"]["messages"] = list(chunk)
        part["payload"]["turn_id"] = "%s.p%d" % (item["payload"]["turn_id"], number)
        part["offset"] = {"start": chunk[0]["index"], "end": chunk[-1]["index"] + 1}
        created = None
        for message in chunk:
            if isinstance(message.get("timestamp"), str):
                created = message["timestamp"]
        part["created_at"] = created or item["created_at"]
        parts.append(part)
        chunk.clear()

    size = overhead
    for message in item["payload"]["messages"]:
        message_bytes = len(_canonical_bytes(message)) + 1
        if chunk and (size + message_bytes > MAX_ITEM_BYTES or
                      len(chunk) >= MAX_MESSAGES_PER_ITEM):
            flush()
            size = overhead
        chunk.append(message)
        size += message_bytes
    flush()
    return parts


def _fit_item(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return ``[item]`` when it fits, else a reduced item or consecutive parts.

    Order of reduction (memory value first): cap tool calls at 64 per message
    and shorten their argument previews, then shorten tool-result excerpts
    (digests and byte counts stay), then split the turn by message ranges.
    """
    if _item_fits(item):
        return [item]
    work = json.loads(json.dumps(item))
    _shrink_tool_calls(work)
    if _item_fits(work):
        return [work]
    for maximum in (2048, 512, 0):
        _shrink_tool_results(work, maximum)
        if _item_fits(work):
            return [work]
    return _split_messages(work)


def _seal_item(session_id: str, items: List[Dict[str, Any]], platform: str) -> Dict[str, Any]:
    """``capture_session`` end boundary that seals an imported session.

    The server extracts a session only once it is sealed. Its event id is
    derived from kind, session, offset and payload, so a rerun is a duplicate.
    """
    water = max((item["offset"]["end"] for item in items), default=0)
    created = max((item["created_at"] for item in items), default=None) or _utc_now()
    return {
        "kind": "capture_session",
        "session_id": session_id,
        "offset": {"start": water, "end": water},
        "capture_origin": "history_replay",
        "batch_id": "",
        "speaker": {"id": "local", "role": "owner", "display": ""},
        "created_at": created,
        "payload": {"boundary": "end", "session_complete": True, "message_high_water": water,
                    "platform": platform, "chat_type": "direct"},
    }


def _split_batches(
    items: List[Dict[str, Any]],
    max_items: int = MAX_ITEMS_PER_BATCH,
    max_bytes: int = MAX_BATCH_BYTES,
) -> List[List[Dict[str, Any]]]:
    batches = []  # type: List[List[Dict[str, Any]]]
    batch = []  # type: List[Dict[str, Any]]
    empty = len(_canonical_bytes({"items": []}))
    batch_bytes = empty
    for item in items:
        size = len(_canonical_bytes(item)) + (1 if batch else 0)
        if batch and (len(batch) >= max_items or batch_bytes + size > max_bytes):
            batches.append(batch)
            batch = []
            batch_bytes = empty
            size = len(_canonical_bytes(item))
        batch.append(item)
        batch_bytes += size
    if batch:
        batches.append(batch)
    return batches


# ---------------------------------------------------------------------------
# Local session catalogue
# ---------------------------------------------------------------------------

def _turn_times(turns: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    first = last = None  # type: Optional[str]
    for turn in turns:
        stamps = [ts for _role, _text, ts in turn["texts"]]
        stamps += [entry[3] for entry in turn["tools"]]
        for ts in stamps:
            ts = _valid_timestamp(ts) or _loose_timestamp(ts)
            if not ts:
                continue
            if first is None or ts < first:
                first = ts
            if last is None or ts > last:
                last = ts
    return first, last


def _loose_timestamp(value: Any) -> Optional[str]:
    """Normalise ``2026-09-01T10:00:00.123+00:00`` style stamps for ordering."""
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _title(turns: List[Dict[str, Any]]) -> str:
    for turn in turns:
        for role, text, _ts in turn["texts"]:
            if role == "user" and isinstance(text, str) and text.strip() and \
                    not text.lstrip().startswith("<"):
                line = " ".join(_redact_text(text).split())
                return line[:80] + ("..." if len(line) > 80 else "")
    return ""


INSTALL_PROMPT_RE = re.compile(r"Install Substrate memory in this agent by following https?://\S*install\.md")


def _has_install_prompt(path: str) -> bool:
    """True when a user message of this transcript holds the install prompt.

    The session where the user pasted the install prompt is the setup session itself:
    it is never imported, whatever its id (ids can change across /reload-plugins).
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if INSTALL_PROMPT_RE.search(line) and ('"user' in line or "user_message" in line):
                    return True
    except OSError:
        return False
    return False


def _catalogue(host: str, exclude: Optional[set] = None,
               only: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Parse local transcripts once; one entry per session id (first file wins)."""
    exclude = exclude or set()
    wanted = set(only) if only else None
    seen = set()  # type: set
    sessions = []  # type: List[Dict[str, Any]]
    for path in _iter_transcript_files(host):
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem in exclude or stem.replace("rollout-", "") in exclude:
            continue
        if _has_install_prompt(path):
            continue
        try:
            session_id, turns = PARSERS[host](path)
        except Exception:
            continue
        if session_id in seen or session_id in exclude or not turns:
            continue
        if wanted is not None and session_id not in wanted and stem not in wanted:
            continue
        seen.add(session_id)
        first, last = _turn_times(turns)
        try:
            info = os.stat(path)
            size, fingerprint = info.st_size, "%d:%d" % (info.st_size, info.st_mtime_ns)
        except OSError:
            size, fingerprint = 0, "0:0"
        sessions.append({"session_id": session_id, "path": path, "turns": turns,
                         "first_at": first, "last_at": last, "bytes": size,
                         "fingerprint": fingerprint})
    sessions.sort(key=lambda s: (s["first_at"] or "", s["session_id"]))
    return sessions


def _session_items(host: str, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The exact items an upload sends for one session (also used to recognise repeats)."""
    items = []  # type: List[Dict[str, Any]]
    for item in _build_envelopes(entry["session_id"], entry["turns"], "history_replay", 0):
        items.extend(_fit_item(item))
    if items:
        # Last, after every turn: seal the session so it is extracted.
        items.append(_seal_item(entry["session_id"], items, host))
    return items


def _digest(items: List[Dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical_bytes(items)).hexdigest()


def _uploaded_state(data_dir: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    progress = _read_json(os.path.join(data_dir, PROGRESS_FILE))
    digests = progress.get("sessions") if isinstance(progress.get("sessions"), dict) else {}
    prints = progress.get("fingerprints") if isinstance(progress.get("fingerprints"), dict) else {}
    return digests, prints


def _already_uploaded(host: str, entry: Dict[str, Any], digests: Dict[str, Any],
                      prints: Dict[str, Any]) -> bool:
    """True when this session was uploaded and its transcript has not changed since.

    An unchanged file is recognised by its fingerprint (size and mtime). A file
    that changed is still the same conversation when its upload items match.
    """
    stored = digests.get(entry["session_id"])
    if not isinstance(stored, str) or not stored:
        return False
    if prints.get(entry["session_id"]) == entry["fingerprint"]:
        return True
    return stored == _digest(_session_items(host, entry))


def _split_uploaded(host: str, data_dir: str,
                    sessions: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Sessions not imported yet, and how many were already imported."""
    digests, prints = _uploaded_state(data_dir)
    pending = []  # type: List[Dict[str, Any]]
    done = 0
    for entry in sessions:
        if _already_uploaded(host, entry, digests, prints):
            done += 1
        else:
            pending.append(entry)
    return pending, done


def _cmd_list(host: str, data_dir: str, exclude: set) -> int:
    sessions, _done = _split_uploaded(host, data_dir, _catalogue(host, exclude))
    for entry in sessions:
        turns = entry["turns"]
        messages = sum(
            len(t["texts"]) + (1 if t["tool_calls"] else 0) + len(t["tools"]) for t in turns
        )
        print(
            json.dumps(
                {"session_id": entry["session_id"], "turns": len(turns),
                 "messages": messages, "first_at": entry["first_at"],
                 "last_at": entry["last_at"], "title": _title(turns),
                 "path": entry["path"]},
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
    for batch in _split_batches(items):
        print(json.dumps({"items": batch}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# Plugin state: decision marker, last hook session, import status/progress.
# One directory per host profile so the hook, the agent's commands and any
# second copy of the plugin agree on it.
# ---------------------------------------------------------------------------

OFFER_FILE = "import-offer.json"
STATUS_FILE = "import-status.json"
PROGRESS_FILE = "import-progress.json"
LOG_FILE = "import.log"
DECISIONS = ("yes", "no", "picked", "none")
LAST_SESSION_MAX_AGE = 12 * 3600
# The offer hook nags only while the model can act on it. Each nag kind stops
# after NAG_LIMIT injections when the connection is still not recorded (the
# model never ran, or cannot run), so no state can keep the hook talking forever.
NAG_LIMIT = 12
# Connection probe budget. The hook has a 5 s timeout; the probe must leave room.
PROBE_TIMEOUT = 2.0
# Sign-in continuation (Claude Code). After the authenticate tool shows the sign-in link,
# the Stop hook keeps the setup turn polling until the plugin's own memory_search works.
# Bounded by block count and wall time, ended by a cancel-style prompt, and keyed to the
# session, so it can never hold a normal turn for long or forever.
SIGNIN_KEY = "signin"
SIGNIN_MAX_BLOCKS = 8
SIGNIN_MAX_SECONDS = 600.0
SIGNIN_STOP_WAIT = 30.0
SIGNIN_CANCEL_RE = re.compile(r"^\s*(stop|cancel|never ?mind|forget it|skip|not now)\b", re.I)
SIGNIN_LINK_RE = re.compile(r"https://[^\s\"'<>\\]+/authorize[^\s\"'<>\\]*")
MCP_SERVER_NAME = "substrate-memory"
MCP_SERVER_URL = "https://app.trysubstrate.co/mcp"


def _data_dir(host: str, override: str = "") -> str:
    if override:
        return override
    env = os.environ.get("SUBSTRATE_DATA_DIR", "")
    if env:
        return env
    if host == "claude":
        base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude")
    else:
        base = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(base, "substrate-memory")


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: str, value: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temp = "%s.%d.tmp" % (path, os.getpid())
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
    os.replace(temp, path)


def _decision(data_dir: str) -> Optional[str]:
    value = _read_json(os.path.join(data_dir, OFFER_FILE)).get("decision")
    return value if value in DECISIONS else None


def _record_decision(data_dir: str, decision: str) -> None:
    path = os.path.join(data_dir, OFFER_FILE)
    state = _read_json(path)
    state["decision"] = decision
    state["decided_at"] = _utc_now()
    _write_json(path, state)


def _connected(data_dir: str) -> bool:
    return isinstance(_read_json(os.path.join(data_dir, OFFER_FILE)).get("connected_at"), str)


def _record_connected(data_dir: str) -> None:
    path = os.path.join(data_dir, OFFER_FILE)
    state = _read_json(path)
    if not isinstance(state.get("connected_at"), str):
        state["connected_at"] = _utc_now()
        _write_json(path, state)


def _mark_disconnected(data_dir: str) -> None:
    path = os.path.join(data_dir, OFFER_FILE)
    state = _read_json(path)
    if not state.get("disconnected_at"):
        state["disconnected_at"] = _utc_now()
        _write_json(path, state)


def _take_disconnected(data_dir: str) -> bool:
    """True once when this computer had been disconnected (a reconnect); clears the marker."""
    path = os.path.join(data_dir, OFFER_FILE)
    state = _read_json(path)
    if not state.get("disconnected_at"):
        return False
    state.pop("disconnected_at", None)
    _write_json(path, state)
    return True


def _claude_mcp_signed_in() -> bool:
    """True when Claude Code holds an MCP OAuth credential for the Substrate server.

    Claude Code keeps MCP OAuth entries under ``mcpOAuth`` in
    ``$CLAUDE_CONFIG_DIR/.credentials.json`` (or ``~/.claude``). The file is read
    only to see whether such an entry exists: no token is printed, sent, or
    refreshed, so the probe cannot race the running session's own refresh. A
    store that is not a readable file (the macOS keychain) reads as not signed in.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    entries = _read_json(os.path.join(base, ".credentials.json")).get("mcpOAuth")
    if not isinstance(entries, dict):
        return False
    for entry in entries.values():
        if not isinstance(entry, dict) or entry.get("serverUrl") != MCP_SERVER_URL:
            continue
        for field in ("accessToken", "refreshToken"):
            if isinstance(entry.get(field), str) and entry[field]:
                return True
    return False


def _codex_mcp_signed_in() -> bool:
    """True when ``codex mcp login`` stored OAuth for the substrate-memory server.

    ``codex mcp list --json`` reports ``auth_status`` from Codex's own credential
    store; the token itself is never read or printed. Anything other than
    ``not_logged_in`` or ``unsupported`` means signed in. The call is bounded by
    PROBE_TIMEOUT, and any failure reads as not signed in.
    """
    try:
        proc = subprocess.run(["codex", "mcp", "list", "--json"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False)
        rows = json.loads(proc.stdout or "[]") if proc.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    if not isinstance(rows, list):
        return False
    for row in rows:
        if isinstance(row, dict) and row.get("name") == MCP_SERVER_NAME:
            status = row.get("auth_status")
            return isinstance(status, str) and status not in ("", "not_logged_in", "unsupported")
    return False


def _probe_connected(host: str) -> bool:
    """Connection check that needs no model turn: the host's own sign-in state."""
    if host == "codex":
        return _codex_mcp_signed_in()
    if host == "claude":
        return _claude_mcp_signed_in()
    return False


def _take_nag(data_dir: str, kind: str) -> bool:
    """Count one injected nag of ``kind``. False once NAG_LIMIT is reached.

    A data dir that cannot be written keeps the old behaviour: the nag still
    shows (there is no state to count in), but nothing is ever recorded.
    """
    path = os.path.join(data_dir, OFFER_FILE)
    state = _read_json(path)
    counts = state.get("nags") if isinstance(state.get("nags"), dict) else {}
    count = counts.get(kind) if isinstance(counts.get(kind), int) else 0
    if count >= NAG_LIMIT:
        return False
    counts[kind] = count + 1
    state["nags"] = counts
    try:
        _write_json(path, state)
    except OSError:
        pass
    return True


def _remember_session(data_dir: str, session_id: str) -> None:
    path = os.path.join(data_dir, OFFER_FILE)
    state = _read_json(path)
    last = state.get("last_session")
    if isinstance(last, dict) and last.get("id") == session_id:
        return
    state["last_session"] = {"id": session_id, "at": time.time()}
    _write_json(path, state)


RECENT_TRANSCRIPT_SECONDS = 600


def _exclusions(data_dir: str, explicit: List[str], host: str = "") -> set:
    """Sessions never imported: the current one, captured live by the hooks.

    The current session is the one named with ``--exclude-session``, plus the
    session the offer hook last saw. When neither is known (for example an
    older copy of the plugin without the hook), the transcript written in the
    last few minutes is taken to be the current one.
    """
    excluded = set(s for s in explicit if s)
    last = _read_json(os.path.join(data_dir, OFFER_FILE)).get("last_session")
    if isinstance(last, dict) and isinstance(last.get("id"), str) and last["id"]:
        at = last.get("at")
        if isinstance(at, (int, float)) and time.time() - at <= LAST_SESSION_MAX_AGE:
            excluded.add(last["id"])
    if not excluded and host:
        newest, newest_at = "", 0.0
        for path in _iter_transcript_files(host):
            try:
                modified = os.path.getmtime(path)
            except OSError:
                continue
            if modified > newest_at:
                newest, newest_at = path, modified
        if newest and time.time() - newest_at <= RECENT_TRANSCRIPT_SECONDS:
            excluded.add(os.path.splitext(os.path.basename(newest))[0])
    return excluded


def _human_date(stamp: Optional[str]) -> str:
    if not stamp:
        return "?"
    try:
        parsed = datetime.strptime(stamp[:10], "%Y-%m-%d")
    except ValueError:
        return stamp[:10]
    return "%d %s %d" % (parsed.day, parsed.strftime("%b"), parsed.year)


def _preview(host: str, data_dir: str, explicit: List[str], force: bool = False) -> Dict[str, Any]:
    """What the import offer shows: conversations not imported yet, and their range.

    With ``force`` every conversation is listed again: used when this account has none of
    the conversations this computer imported earlier (they went to another account).
    """
    excluded = _exclusions(data_dir, explicit, host)
    catalogue = _catalogue(host, excluded)
    if force:
        sessions, already = catalogue, 0
    else:
        sessions, already = _split_uploaded(host, data_dir, catalogue)
    turns = sum(len(s["turns"]) for s in sessions)
    firsts = [s["first_at"] for s in sessions if s["first_at"]]
    lasts = [s["last_at"] for s in sessions if s["last_at"]]
    return {
        "sessions": len(sessions),
        "turns": turns,
        "first_at": min(firsts) if firsts else None,
        "last_at": max(lasts) if lasts else None,
        "bytes_estimate": sum(s["bytes"] for s in sessions),
        "already_imported": already,
        "excluded": sorted(excluded),
        "decision": _decision(data_dir),
    }


def _cmd_preview(host: str, data_dir: str, explicit: List[str], force: bool = False) -> int:
    summary = _preview(host, data_dir, explicit, force)
    print(json.dumps(summary, ensure_ascii=False))
    count = summary["sessions"]
    if count:
        sys.stderr.write("Found %s (%s) from %s to %s.\n" % (
            _plural(count, "past conversation"), _plural(summary["turns"], "turn"),
            _human_date(summary["first_at"]), _human_date(summary["last_at"])))
    else:
        sys.stderr.write("No past conversations left to import on this computer.\n")
    return 0


# ---------------------------------------------------------------------------
# Offer check (UserPromptSubmit command hook). Silent once decided. Never
# fails visibly: any problem means "say nothing" and exit 0.
# ---------------------------------------------------------------------------

def _quote(value: str) -> str:
    if os.name == "nt":
        value = value.replace("\\", "/")
    return '"%s"' % value.replace('"', '\\"')


# The plugin's own server, as Claude Code names its tools. Tools from a claude.ai
# connector (mcp__claude_ai_*) are a different server and prove nothing here.
CLAUDE_SEARCH_TOOL = "mcp__plugin_substrate-claude_substrate-memory__memory_search"


def _search_tool(host: str) -> str:
    return CLAUDE_SEARCH_TOOL if host == "claude" else "memory_search"


def _import_invoke(host: str) -> str:
    """How the agent must start the import skill: Codex has no Skill tool, and its skill is substrate-import."""
    if host == "codex":
        return "use the `$substrate-import` skill"
    return "invoke the Skill tool with skill `substrate-claude:substrate-import`"


def _sync_base(host: str, data_dir: str, session_id: str) -> str:
    script = os.path.abspath(__file__)
    base = "%s %s --host %s --data-dir %s" % (
        _quote(sys.executable or "python3"), _quote(script), host, _quote(data_dir))
    if session_id:
        base += " --exclude-session %s" % _quote(session_id)
    return base


def _offer_sentence(summary: Dict[str, Any]) -> str:
    count = summary["sessions"]
    turns = summary["turns"]
    return ("I found %s on this computer (%s, %s to %s). Import them into Substrate? "
            "Import all / Let me pick / Not now") % (
        _plural(count, "past conversation"), _plural(turns, "turn", "{:,}".format(turns)),
        _human_date(summary["first_at"]), _human_date(summary["last_at"]))


def _import_summary(sessions: int, tally: "Tally") -> str:
    """The one line the user sees when an import ends. Counts only when something was refused."""
    if sessions <= 0:
        text = "No conversations were left to import."
    elif tally.stored == 0 and tally.duplicate > 0 and tally.rejected == 0:
        text = ("That conversation was already in Substrate, so nothing new was added." if sessions == 1
                else "Those %s were already in Substrate, so nothing new was added." % _plural(sessions, "conversation"))
    else:
        text = ("Imported %s. Substrate is reading them now; ask about them in a few minutes."
                % _plural(sessions, "conversation"))
    if tally.rejected:
        text += " %s could not be saved." % _plural(tally.rejected, "item")
    return text


def _plural(count: int, word: str, text: Optional[str] = None) -> str:
    shown = str(count) if text is None else text
    return "%s %s%s" % (shown, word, "" if count == 1 else "s")


def _ticket_path(data_dir: str) -> str:
    """A new, unpredictable file name for one ticket; the upload deletes it after reading."""
    return os.path.join(os.path.abspath(data_dir),
                        "import-ticket-%s.txt" % uuid.uuid4().hex[:8])


def _offer_text(host: str, data_dir: str, session_id: str,
                summary: Optional[Dict[str, Any]]) -> str:
    base = _sync_base(host, data_dir, session_id)
    if summary is not None:
        offer = ('say, word for word: "%s"' % _offer_sentence(summary))
    else:
        offer = ("run `SYNC --preview` and say its conversation count and date range, then "
                 "Import all / Let me pick / Not now")
    return (
        "[substrate] Import offer pending (asked once per computer). When the user chooses, invoke "
        "%(invoke)s and follow it; its permissions "
        "apply to that turn. Ignore this note until "
        "the Substrate memory tools work in this session. Once the plugin's own "
        "`%(search)s` has succeeded and you have said \"Connected to Substrate.\", in that same "
        "reply and without waiting for the user, %(offer)s. SYNC means: `%(base)s`.\n"
        "Import all: run `SYNC --new-claim` and keep the claim it prints. Call memory_import_ticket "
        "{\"platform\": \"%(host)s\", \"claim\": \"<claim>\"} (if refused because of the claim, send only the platform). If the result has claim_pending, run "
        "`SYNC --upload --all --background --claim \"<claim>\" --mcp-url \"<mcp_url>\"` with mcp_url "
        "from that result. If the result has a ticket instead (an older server), use the Write tool to "
        "create the file `%(ticket)s` containing only the ticket string, then run "
        "`SYNC --upload --all --background --ticket-file \"%(ticket)s\" --mcp-url \"<mcp_url>\"`. The "
        "claim and the ticket must never appear in your reply. Then keep running `SYNC --status --wait 60` "
        "and report each message until it is done; end with its summary line. Do not stop and wait for "
        "the user while it runs.\n"
        "Let me pick: `SYNC --list`, show titles and dates, then upload the same way (a new "
        "ticket file named import-ticket-<8 random letters>.txt in the same folder) with "
        "`--session <id>` per chosen conversation instead of `--all`.\n"
        "Not now: `SYNC --record-decision no`; say they can ask \"import my past "
        "conversations\" any time."
    ) % {"base": base, "host": host, "search": _search_tool(host), "offer": offer,
         "ticket": _ticket_path(data_dir), "invoke": _import_invoke(host)}


def _disconnect_text() -> str:
    return (
        "[substrate] The Substrate connection on this computer was removed. Answer the user's "
        "request normally without Substrate memory. Do not search Notion, Drive, Gmail or other "
        "apps on your own because of this, and do not mention hook errors. In one short line, "
        "once in this session, tell the user: Substrate is disconnected on this computer. To "
        "reconnect: open /mcp, select plugin:substrate-claude:substrate-memory, choose Enable if "
        "shown, then Authenticate."
    )


def _setup_text(host: str, data_dir: str, session_id: str,
                summary: Optional[Dict[str, Any]], offer: bool) -> str:
    base = _sync_base(host, data_dir, session_id)
    search = _search_tool(host)
    connector = ""
    if host == "claude":
        connector = (" Only this plugin's own tool counts: a claude.ai connector's tools (named "
                     "mcp__claude_ai_*) do not prove this plugin is connected.")
    text = (
        "[substrate] Substrate setup is not finished on this computer. Finish it now, in this "
        "reply, before anything else, in a few short lines. First invoke the Skill tool with skill "
        "`substrate-claude:substrate-connect` and follow it: its permissions apply to this turn. If "
        "the Skill tool is not available, follow the steps below. A bare \"continue\", \"done\", "
        "\"ok\" or \"approved\" from the user just means: carry on with Substrate setup. Do not "
        "comment on it or ask what to work on. SYNC means: `%(base)s`.\n"
        "1. Check the connection with `%(search)s` (load it with ToolSearch if it is deferred), "
        "a short, non-secret query.%(connector)s If that call works (even empty): say "
        "run `SYNC --record-connected`, then say \"Connected to Substrate.\" (or \"Reconnected to "
        "Substrate.\" when it prints \"reconnected\": true). Say \"Connected\" only "
        "after that call succeeds. If a \"[substrate] Offer this once\" note appears, ignore it: "
        "this is the plugin, and the plugin handles the import.\n"
        "2. If the plugin's tool is missing or fails with \"not connected\", \"needs "
        "authentication\" or \"unauthorized\" (or only an `authenticate` tool for substrate-memory "
        "exists): if that tool is not loaded, load it with ToolSearch first. If the server is listed "
        "as disabled, or Claude Code says it is not connected, say exactly: \"Open /mcp, select "
        "plugin:substrate-claude:substrate-memory, choose Enable if shown, then Authenticate.\" "
        "Then call the tool. In your reply, paste the full sign-in URL it returns "
        "as plain text (the user cannot see tool output), then say: \"Open this link, sign in and "
        "choose Approve connection. I'll continue automatically.\" Omit the tool's notes about error pages, "
        "localhost, redirects or pasting the URL. Paste the link again whenever "
        "the user says they cannot see it. If the result has no link, say so and ask the user to "
        "run /mcp, choose plugin:substrate-claude:substrate-memory, Authenticate, and send you the "
        "link it shows. Never describe this session or computer as remote, local or SSH, and never "
        "mention ports, callback addresses or localhost, even if the tool's result says so. Only if "
        "the user reports an error page, ask them to paste the full address-bar URL. Then do NOT end your turn: run `SYNC --pause 15`, then call `%(search)s` "
        "again (load it with ToolSearch if it is deferred); repeat up to 20 times (about 5 "
        "minutes). As soon as it works, do step 1 and continue. If it never works, say: "
        "\"Substrate is not connected yet. Open /mcp, choose plugin:substrate-claude:"
        "substrate-memory, finish Authenticate, then say continue.\" Never ask for a token or "
        "key, and never say Connected in that case.\n"
        "3. If no Substrate memory tools exist at all, tell the user to type /reload-plugins "
        "and then say \"continue\"."
    ) % {"base": base, "search": search, "connector": connector}
    if offer:
        if summary is not None:
            offer_text = 'say, word for word: "%s"' % _offer_sentence(summary)
        else:
            offer_text = ("run `SYNC --preview` and say its conversation count and date range, "
                          "then Import all / Let me pick / Not now")
        follow = ("$substrate-import" if host == "codex"
                  else "/substrate-claude:substrate-import (or $substrate-import)")
        text += ("\n4. Right after \"Connected to Substrate.\", in the same reply: %s. Then, if "
                 "they choose, follow %s; "
                 "the import ticket never goes in a command or in your reply. For the user's choice, %s "
                 "(its permissions apply to that turn)." % (offer_text, follow, _import_invoke(host)))
    return text


def _signed_out_locally(host: str) -> bool:
    """Claude Code only: this computer's MCP credential store lists sign-ins, but not Substrate's.

    Only a readable ``.credentials.json`` with an ``mcpOAuth`` map counts. On the macOS
    keychain there is no such file, so the check stays silent there. It cannot see a
    sign-in revoked on the server; that shows up only as hook errors.
    """
    if host != "claude":
        return False
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    entries = _read_json(os.path.join(base, ".credentials.json")).get("mcpOAuth")
    return isinstance(entries, dict) and not _claude_mcp_signed_in()


def _notice_once(data_dir: str, session_id: str) -> bool:
    """True the first time this session needs the disconnect note (state is recorded first)."""
    path = os.path.join(data_dir, OFFER_FILE)
    state = _read_json(path)
    key = session_id or "-"
    if state.get("disconnect_notice") == key:
        return False
    state["disconnect_notice"] = key
    try:
        _write_json(path, state)
    except OSError:
        return False
    return True


def _nag_open(data_dir: str, kind: str) -> bool:
    counts = _read_json(os.path.join(data_dir, OFFER_FILE)).get("nags")
    count = counts.get(kind) if isinstance(counts, dict) else None
    return not (isinstance(count, int) and count >= NAG_LIMIT)


def _offer_summary(host: str, data_dir: str, session_id: str) -> Optional[Dict[str, Any]]:
    try:
        return _preview(host, data_dir, [session_id] if session_id else [])
    except Exception:  # noqa: BLE001 - the hook still gives the offer, without counts
        return None


def _emit_context(context: str) -> None:
    output = {"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": context,
    }}
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()


def _cmd_offer_check(host: str, data_dir: str) -> int:
    try:
        session_id = ""
        payload = _hook_payload()
        prompt = payload.get("prompt")
        if isinstance(prompt, str) and SIGNIN_CANCEL_RE.match(prompt):
            # The user moved on or declined: stop holding the setup turn open.
            _signin_end(data_dir, "cancelled")
        value = payload.get("session_id")
        if isinstance(value, str) and 0 < len(value) <= 512:
            session_id = value
        decided = _decision(data_dir) is not None
        if session_id:
            try:
                _remember_session(data_dir, session_id)
            except OSError:
                pass  # read-only sandbox: the offer still works
        connected = _connected(data_dir)
        if not connected and _probe_connected(host):
            # The host already holds a working sign-in (`codex mcp login` or
            # Claude Code's /mcp Authenticate). Record it here so the setup
            # nag clears without the model having to run --record-connected.
            try:
                _record_connected(data_dir)
            except OSError:
                pass
            connected = True
        if connected and _signed_out_locally(host):
            # Removed on this computer after it worked: one quiet line per session.
            if _notice_once(data_dir, session_id):
                _mark_disconnected(data_dir)
                _emit_context(_disconnect_text())
            return 0
        if connected and decided:
            return 0
        kind = "offer" if connected else "setup"
        if not _nag_open(data_dir, kind):
            return 0
        summary = None  # type: Optional[Dict[str, Any]]
        if not decided:
            summary = _offer_summary(host, data_dir, session_id)
            if summary is not None and summary["sessions"] == 0:
                try:
                    _record_decision(data_dir, "none")
                except OSError:
                    pass
                decided = True
                if connected:
                    return 0
        if not _take_nag(data_dir, kind):
            return 0
        if connected:
            context = _offer_text(host, data_dir, session_id, summary)
        else:
            context = _setup_text(host, data_dir, session_id, summary, offer=not decided)
        _emit_context(context)
    except BaseException:  # noqa: BLE001 - a hook never fails the prompt
        return 0
    return 0


# ---------------------------------------------------------------------------
# Sign-in continuation (Claude Code hooks, fail open).
#   PostToolUse on the plugin's authenticate tool: record a pending sign-in for this
#     session and hand the model the exact link sentence (--signin-start).
#   Stop: while that sign-in is pending and inside its bounds, block the turn end
#     with a short instruction to keep polling (--stop-check). Anything unexpected
#     (no state, other session, bound reached, unreadable state) lets the turn end.
# ---------------------------------------------------------------------------

def _hook_payload() -> Dict[str, Any]:
    """The hook's JSON input on stdin, or {} when there is none or it is not an object."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read(1 << 20)
        parsed = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _hook_session(payload: Dict[str, Any]) -> str:
    value = payload.get("session_id")
    return value if isinstance(value, str) and 0 < len(value) <= 512 else ""


def _signin_load(data_dir: str) -> Dict[str, Any]:
    value = _read_json(os.path.join(data_dir, OFFER_FILE)).get(SIGNIN_KEY)
    return value if isinstance(value, dict) else {}


def _signin_save(data_dir: str, value: Dict[str, Any]) -> None:
    path = os.path.join(data_dir, OFFER_FILE)
    state = _read_json(path)
    state[SIGNIN_KEY] = value
    _write_json(path, state)


def _signin_end(data_dir: str, outcome: str) -> None:
    """Close a pending sign-in with ``outcome``. Writes only when one is pending."""
    current = _signin_load(data_dir)
    if current.get("state") != "pending":
        return
    current["state"] = outcome
    try:
        _signin_save(data_dir, current)
    except OSError:
        pass


def _plain_command(args: str) -> str:
    """A command in the pre-approved form the connect skill uses (python3 + this script)."""
    return 'python3 "%s" --host claude %s' % (os.path.abspath(__file__), args)


def _signin_link(payload: Dict[str, Any]) -> str:
    """The authorize URL from the tool result, or '' when there is none."""
    found = SIGNIN_LINK_RE.search(json.dumps(payload.get("tool_response"), ensure_ascii=False))
    return found.group(0) if found else ""


def _signin_start_text(link: str, reconnect: bool) -> str:
    if link:
        show = ("Reply now with exactly this sentence, then the link as plain text on its own line, and "
                "nothing else about the sign-in: \"Open this link, sign in and choose **Approve "
                "connection**. I'll continue automatically.\" Then the link: %s") % link
    else:
        show = ("No sign-in link was found in the tool result. Say so, and ask the user to run /mcp, "
                "choose plugin:substrate-claude:substrate-memory, Authenticate, and send you the link it shows.")
    history = "This computer was connected before" if reconnect else "This is a first connection"
    return (
        "[substrate] Sign-in started. %s Omit the tool's other notes (browser error pages, localhost, "
        "redirects, ports, pasting the address-bar URL, remote or SSH); the user will say if they see an "
        "error page. Then do not end your turn: run `%s`, call the plugin's memory_search again (load it "
        "with ToolSearch if deferred), and repeat until it works. When it works, run `%s` and say what its "
        "JSON says: \"Reconnected to Substrate.\" when \"reconnected\": true, otherwise \"Connected to "
        "Substrate.\" %s. Never say either word before that command has run."
    ) % (show, _plain_command("--pause 15"), _plain_command("--record-connected"), history + ".")


def _cmd_signin_start(host: str, data_dir: str) -> int:
    payload = _hook_payload()
    session_id = _hook_session(payload)
    link = _signin_link(payload)
    state = _read_json(os.path.join(data_dir, OFFER_FILE))
    reconnect = isinstance(state.get("connected_at"), str) or isinstance(state.get("disconnected_at"), str)
    if session_id:
        try:
            _signin_save(data_dir, {"state": "pending", "session_id": session_id, "started": time.time(),
                                    "blocks": 0, "reconnect": reconnect})
        except OSError:
            pass  # no state: no continuation, but the link text still reaches the model
    if not link:
        _emit_hook_context("PostToolUse", _signin_start_text(link, reconnect))
        return 0
    # The model sees the tool result it is given, so replace the tool's notes (error
    # pages, pasting URLs) with the clean link. The user sees the same link through
    # systemMessage, whatever the model writes. Older hosts ignore both fields.
    sentence = "Open this link, sign in and choose **Approve connection**. I'll continue automatically."
    output = {
        "systemMessage": "Substrate sign-in: %s %s" % (sentence, link),
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": _signin_start_text(link, reconnect),
            "updatedToolOutput": "Sign-in started. The user has the sign-in link: %s" % link,
        },
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()
    return 0


def _signin_wait_seconds() -> float:
    """How long the Stop hook waits for the approval. Never more than SIGNIN_STOP_WAIT."""
    try:
        value = float(os.environ.get("SUBSTRATE_SIGNIN_STOP_WAIT", SIGNIN_STOP_WAIT))
    except ValueError:
        value = SIGNIN_STOP_WAIT
    return max(0.0, min(value, SIGNIN_STOP_WAIT))


def _stop_reason(approved: bool, blocks: int) -> str:
    base = ("Substrate sign-in is pending (continuation %d of %d). " % (blocks, SIGNIN_MAX_BLOCKS))
    check = ("Call the plugin's memory_search now (load it with ToolSearch if deferred). If it works, run "
             "`%s` and reply with \"Reconnected to Substrate.\" or \"Connected to Substrate.\" as its "
             "JSON says, then continue setup. " % _plain_command("--record-connected"))
    if approved:
        return base + "The browser approval is in. " + check + "Do not paste the sign-in link again."
    return (base + "Not approved yet: run `%s`, then do the memory_search check. " % _plain_command("--pause 15")
            + check + "Do not paste the sign-in link again and do not say Connected before memory_search succeeds.")


def _cmd_stop_check(host: str, data_dir: str) -> int:
    payload = _hook_payload()
    session_id = _hook_session(payload)
    if not session_id:
        return 0
    current = _signin_load(data_dir)
    if current.get("state") != "pending" or current.get("session_id") != session_id:
        return 0
    started = current.get("started")
    blocks = current.get("blocks") if isinstance(current.get("blocks"), int) else 0
    if (not isinstance(started, (int, float)) or blocks >= SIGNIN_MAX_BLOCKS
            or time.time() - started > SIGNIN_MAX_SECONDS):
        _signin_end(data_dir, "expired")
        return 0
    # Give the browser approval a short head start before the model polls again.
    deadline = min(time.time() + _signin_wait_seconds(), started + SIGNIN_MAX_SECONDS)
    while not _claude_mcp_signed_in() and time.time() < deadline:
        _sleep(2.0)
    approved = _claude_mcp_signed_in()
    current["blocks"] = blocks + 1
    try:
        _signin_save(data_dir, current)  # count first: if this fails, never block
    except OSError:
        return 0
    sys.stdout.write(json.dumps({"decision": "block", "reason": _stop_reason(approved, blocks + 1)}))
    sys.stdout.flush()
    return 0


def _emit_hook_context(event: str, context: str) -> None:
    output = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Direct upload over MCP Streamable HTTP with an import ticket.
# ---------------------------------------------------------------------------

USER_AGENT = "substrate-sync/0.10.1"
MAX_RETRIES = 8
REQUEST_TIMEOUT = 120.0
# Every network step has a time budget, so a server that accepts the connection and never
# answers fails in well under a minute, with a message, instead of retrying for 20 minutes.
CLAIM_TIMEOUT = 10.0    # one import-claim request
CLAIM_BUDGET = 25.0     # all tries of the claim exchange (foreground, before the upload starts)
SETUP_TIMEOUT = 20.0    # one MCP initialize request
SETUP_BUDGET = 45.0     # all tries of the handshake
UPLOAD_BUDGET = 300.0   # all tries of one import request in the background run


class AuthStop(Exception):
    """Ticket expired or revoked: stop cleanly, the run can be resumed."""


class Retryable(Exception):
    def __init__(self, reason: str, retry_after: Optional[float] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class Fatal(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BadBatch(Exception):
    """The server refused this batch as a whole (schema or size)."""


class NetworkBlocked(Exception):
    """No route to the server from here (DNS, refused, sandbox denial). Retrying cannot help."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Errors that mean this process cannot reach the network at all (Codex's sandbox, a firewall).
_BLOCKED_ERRNOS = {errno.EPERM, errno.EACCES, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN}


def _network_error(error: BaseException) -> Exception:
    """Classify a failed request made without an HTTP response.

    Blocked (no route out: DNS failure, refused connection, permission denied) is
    not retryable. A timeout (request sent, no answer in time) and anything else
    stay Retryable, as before.
    """
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, TimeoutError):
        return Retryable("network: %s" % type(error).__name__)
    if isinstance(reason, (socket.gaierror, ConnectionError)):
        return NetworkBlocked("network_blocked")
    if isinstance(reason, OSError) and reason.errno in _BLOCKED_ERRNOS:
        return NetworkBlocked("network_blocked")
    return Retryable("network: %s" % type(error).__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _is_loopback(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or not parsed.netloc or parsed.fragment:
        raise Fatal("invalid_url")
    if parsed.scheme == "https" or (parsed.scheme == "http" and _is_loopback(parsed.hostname or "")):
        return url
    raise Fatal("invalid_url")


def _retry_after(headers: Any) -> Optional[float]:
    try:
        value = headers.get("Retry-After") if headers is not None else None
        seconds = float(value) if value else None
    except (TypeError, ValueError):
        return None
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _parse_body(raw: bytes) -> Any:
    """Plain JSON or SSE ``data:`` lines (last JSON-RPC message wins)."""
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        pass
    found = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            datum = line[5:].strip()
            try:
                value = json.loads(datum)
            except ValueError:
                continue
            if isinstance(value, dict) and ("result" in value or "error" in value):
                found = value
    if found is None:
        raise Retryable("invalid_response")
    return found


class McpClient:
    """Minimal MCP JSON-RPC client: initialize once, then tools/call."""

    def __init__(self, url: str, token: str) -> None:
        self.url = _check_url(url)
        self.token = token
        self.session_id = ""
        self.protocol = ""
        self._ids = 0
        self.timeout = REQUEST_TIMEOUT
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _post(self, body: Dict[str, Any]) -> Tuple[Any, Any]:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.token,
            "User-Agent": USER_AGENT,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                if not 200 <= int(status) < 300:
                    raise Retryable("http_%s" % status)
                raw = response.read(8 * 1024 * 1024)
                return raw, response.headers
        except urllib.error.HTTPError as error:
            code = error.code
            if code in (401, 403):
                raise AuthStop("http_%d" % code) from None
            if code == 413:
                raise BadBatch("payload_too_large") from None
            if code == 402:
                raise Fatal("payment_required") from None
            if code in (408, 425, 429) or 500 <= code <= 599:
                raise Retryable("http_%d" % code, _retry_after(error.headers)) from None
            if 300 <= code < 400:
                raise Fatal("redirect_refused") from None
            raise Fatal("http_%d" % code) from None
        except (urllib.error.URLError, OSError, socket.timeout) as error:
            raise _network_error(error) from None

    def rpc(self, method: str, params: Dict[str, Any]) -> Any:
        self._ids += 1
        raw, headers = self._post({"jsonrpc": "2.0", "id": self._ids,
                                   "method": method, "params": params})
        session = headers.get("Mcp-Session-Id") if headers is not None else None
        if session and not self.session_id:
            self.session_id = session
        if not raw:
            raise Retryable("empty_response")
        envelope = _parse_body(raw)
        if not isinstance(envelope, dict):
            raise Retryable("invalid_response")
        error = envelope.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            message = str(error.get("message", "")) if isinstance(error, dict) else ""
            if "unauthorized" in message.lower():
                raise AuthStop("unauthorized")
            if code == -32602 or "valid" in message.lower():
                raise BadBatch("invalid_params")
            if code in (-32601,):
                raise Fatal("method_not_found")
            raise Retryable("rpc_error_%s" % code)
        return envelope.get("result")

    def initialize(self) -> Dict[str, Any]:
        result = self.rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "substrate-sync", "version": USER_AGENT.split("/")[1]},
        })
        if not isinstance(result, dict):
            raise Fatal("invalid_initialize")
        info = result.get("serverInfo") or {}
        if info.get("name") != "substrate-memory":
            raise Fatal("not_substrate")
        version = result.get("protocolVersion")
        if isinstance(version, str):
            self.protocol = version
        if self.session_id:
            try:
                self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            except (Retryable, BadBatch):
                pass
        return result

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise Retryable("invalid_response")
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            structured = {}
            content = result.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                try:
                    candidate = json.loads(content[0].get("text", ""))
                    if isinstance(candidate, dict):
                        structured = candidate
                except (TypeError, ValueError):
                    structured = {}
        if result.get("isError"):
            category = structured.get("error") if isinstance(structured.get("error"), str) else ""
            if category in ("unauthorized", "forbidden"):
                raise AuthStop(category)
            if category == "payment_required":
                raise Fatal("payment_required")
            if category in ("rate_limited", "internal", "conflict"):
                raise Retryable(category)
            raise BadBatch(category or "invalid_request")
        return structured


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _backoff(attempt: int, hint: Optional[float]) -> float:
    try:
        cap = float(os.environ.get("SUBSTRATE_SYNC_BACKOFF_MAX", "60"))
    except ValueError:
        cap = 60.0
    wait = hint if hint is not None else float(2 ** attempt)
    return max(0.0, min(cap, wait))


class Unavailable(Fatal):
    """The server did not answer within this step's time budget."""


def _with_retries(action, on_wait=None, budget=None):  # noqa: ANN001, ANN202
    attempt = 0
    started = time.monotonic()
    while True:
        try:
            return action()
        except Retryable as error:
            attempt += 1
            if attempt > MAX_RETRIES:
                raise Fatal("server_unavailable (%s)" % error.reason) from None
            wait = _backoff(attempt, error.retry_after)
            if budget is not None and time.monotonic() - started + wait > budget:
                raise Unavailable("server_unavailable (%s)" % error.reason) from None
            if on_wait is not None:
                on_wait(error.reason, wait)
            _sleep(wait)


class Tally:
    """Turn outcomes (the summary line) and session seals, counted apart."""

    def __init__(self) -> None:
        self.stored = 0
        self.duplicate = 0
        self.rejected = 0
        self.sealed = 0
        self.seal_failed = 0

    def _count(self, item: Dict[str, Any], action: Any) -> None:
        if item.get("kind") == "capture_session":
            if action in ("stored", "queued", "sealed", "duplicate"):
                self.sealed += 1
            else:
                self.seal_failed += 1
        elif action in ("stored", "queued", "sealed"):
            self.stored += 1
        elif action == "duplicate":
            self.duplicate += 1
        else:
            self.rejected += 1

    def add(self, result: Dict[str, Any], batch: List[Dict[str, Any]]) -> None:
        rows = result.get("results")
        if isinstance(rows, list) and rows:
            for position, row in enumerate(rows):
                row = row if isinstance(row, dict) else {}
                index = row.get("index", position)
                if not isinstance(index, int) or not 0 <= index < len(batch):
                    index = min(position, len(batch) - 1)
                self._count(batch[index], row.get("action"))
            return
        rejected = result.get("rejected") if isinstance(result.get("rejected"), int) else 0
        self.rejected += rejected
        for item in batch[: max(0, len(batch) - rejected)]:
            self._count(item, "stored")


def _send_batch(client: McpClient, batch: List[Dict[str, Any]], batch_id: str,
                tally: Tally, on_wait=None) -> None:  # noqa: ANN001
    """Send one batch; a refused batch is bisected so one bad item never sinks the rest."""
    try:
        result = _with_retries(
            lambda: client.call_tool("memory_import", {"items": batch, "batch_id": batch_id}),
            on_wait, budget=UPLOAD_BUDGET,
        )
    except BadBatch:
        if len(batch) == 1:
            tally._count(batch[0], "rejected")
            return
        middle = len(batch) // 2
        _send_batch(client, batch[:middle], batch_id, tally, on_wait)
        _send_batch(client, batch[middle:], batch_id, tally, on_wait)
        return
    tally.add(result, batch)


def _pid_alive(pid: Any) -> Optional[bool]:
    if not isinstance(pid, int) or pid <= 0:
        return None
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes  # noqa: PLC0415 - Windows only

            kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel.CloseHandle(handle)
            return code.value == 259
        except Exception:
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


STALE_SECONDS = 15 * 60


def _status_message(status: Dict[str, Any]) -> str:
    state = status.get("state")
    done = status.get("sessions_done", 0)
    total = status.get("sessions_total", 0)
    if state in ("starting", "running"):
        if not total:
            return "Import starting: reading past conversations on this computer."
        return "Importing past conversations: %d of %d done (%d stored, %d already there)." % (
            done, total, status.get("stored", 0), status.get("duplicate", 0))
    if state == "done":
        return status.get("summary") or "Import finished."
    if state == "stopped":
        return status.get("message") or "Import stopped."
    if state == "failed":
        return status.get("message") or "Import failed."
    return "No import has run on this computer yet."


def _effective_status(data_dir: str) -> Dict[str, Any]:
    status = _read_json(os.path.join(data_dir, STATUS_FILE))
    if not status:
        status = {"state": "none"}
    if status.get("state") in ("starting", "running"):
        alive = _pid_alive(status.get("pid"))
        updated = status.get("updated_ts")
        stale = isinstance(updated, (int, float)) and time.time() - updated > STALE_SECONDS
        no_pid_yet = status.get("pid") is None and isinstance(updated, (int, float)) and \
            time.time() - updated > 60
        if alive is False or stale or no_pid_yet:
            status["state"] = "stopped"
            status["message"] = ("The import stopped before finishing. Ask me to continue the "
                                 "import; conversations already imported are skipped.")
    status["message"] = _status_message(status)
    status["done"] = status.get("state") in ("done", "stopped", "failed", "none")
    return status


def _cmd_status(data_dir: str, wait: float) -> int:
    deadline = time.time() + max(0.0, min(wait, 110.0))
    first = _effective_status(data_dir)
    status = first
    while not status["done"] and time.time() < deadline:
        _sleep(1.0)
        status = _effective_status(data_dir)
        if status.get("sessions_done") != first.get("sessions_done") or \
                status.get("state") != first.get("state"):
            break
    print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    return 0


class StatusWriter:
    def __init__(self, data_dir: str, base: Dict[str, Any]) -> None:
        self.path = os.path.join(data_dir, STATUS_FILE)
        self.state = dict(base)

    def update(self, **changes: Any) -> None:
        self.state.update(changes)
        self.state["updated_at"] = _utc_now()
        self.state["updated_ts"] = time.time()
        try:
            _write_json(self.path, self.state)
        except OSError:
            pass


TICKET_MAX_BYTES = 4096


def _without_flag(argv: List[str], flag: str, takes_value: bool) -> List[str]:
    """argv minus ``flag``; with ``takes_value`` its separate value word goes too."""
    out = []  # type: List[str]
    skip = False
    for word in argv:
        if skip:
            skip = False
            continue
        if word == flag:
            skip = takes_value
            continue
        if word.startswith(flag + "="):
            continue
        out.append(word)
    return out


def _clean_ticket(raw: str) -> str:
    value = raw.strip()
    return value if value and len(value.split()) == 1 else ""


def _take_ticket(path: str) -> str:
    """Read the import ticket from the file the agent wrote, then delete that file.

    The file is made 0600 first (best effort). It is deleted even when its
    content is refused, so a ticket never lingers on disk. A symlink or a
    non-file is ignored and left alone.
    """
    if not path or os.path.islink(path) or not os.path.isfile(path):
        return ""
    try:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read(TICKET_MAX_BYTES + 1)
        if len(raw) > TICKET_MAX_BYTES:
            raw = ""  # far too long to be a ticket: refuse rather than truncate
    except OSError:
        raw = ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return _clean_ticket(raw)


def _take_child_ticket() -> str:
    """A background run receives its ticket on stdin from the parent, never on argv."""
    try:
        raw = sys.stdin.read(TICKET_MAX_BYTES)
    except (OSError, ValueError):
        raw = ""
    return _clean_ticket(raw)


def _billing_message(url: str) -> str:
    """Plain words for HTTP 402 from the MCP endpoint: the trial is over, memory needs a card."""
    parsed = urllib.parse.urlsplit(url)
    origin = "%s://%s" % (parsed.scheme or "https", parsed.netloc or "app.trysubstrate.co")
    return "Your Substrate trial has ended. Add a card at %s/billing to keep using memory." % origin


def _mcp_url() -> str:
    return os.environ.get("SUBSTRATE_MCP_URL", "").strip()


CLAIM_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")


def _new_claim() -> str:
    """A fresh claim for memory_import_ticket. It is not a credential: it unlocks one ticket, once, for 10 minutes."""
    return secrets.token_urlsafe(32)


def _clean_claim(raw: str) -> str:
    value = raw.strip()
    return value if CLAIM_RE.fullmatch(value) else ""


def _claim_ticket(mcp_url: str, claim: str) -> str:
    """Exchange a claim for the import ticket, so the ticket never reaches the chat."""
    parsed = urllib.parse.urlsplit(_check_url(mcp_url))
    url = _check_url("%s://%s/mcp/import-ticket/claim" % (parsed.scheme, parsed.netloc))
    request = urllib.request.Request(
        url, data=json.dumps({"claim": claim}).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": USER_AGENT})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=CLAIM_TIMEOUT) as response:
            raw = response.read(64 * 1024)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise Fatal("claim_not_found") from None
        if error.code == 429 or 500 <= error.code <= 599:
            raise Retryable("http_%d" % error.code, _retry_after(error.headers)) from None
        raise Fatal("http_%d" % error.code) from None
    except (urllib.error.URLError, OSError, socket.timeout) as error:
        raise _network_error(error) from None
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise Retryable("invalid_response") from None
    ticket = value.get("ticket") if isinstance(value, dict) else None
    ticket = _clean_ticket(ticket) if isinstance(ticket, str) else ""
    if not ticket:
        raise Fatal("claim_not_found")
    return ticket


def _spawn_background(argv: List[str], data_dir: str, ticket: str) -> int:
    os.makedirs(data_dir, exist_ok=True)
    child_argv = _without_flag(_without_flag(_without_flag(argv, "--background", False),
                                             "--ticket-file", True), "--claim", True)
    command = [sys.executable, os.path.abspath(__file__)] + child_argv + ["--_child"]
    StatusWriter(data_dir, {"state": "starting", "pid": None, "started_at": _utc_now()}).update()
    log = open(os.path.join(data_dir, LOG_FILE), "ab")
    kwargs = {}  # type: Dict[str, Any]
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                 cwd=data_dir, env=dict(os.environ), close_fds=True, **kwargs)
    finally:
        log.close()
    if child.stdin is not None:
        try:
            child.stdin.write((ticket + "\n").encode("utf-8"))
            child.stdin.close()
        except OSError:
            pass
    status_path = os.path.join(data_dir, STATUS_FILE)
    print(json.dumps({"started": True, "pid": child.pid, "status_file": status_path,
                      "message": "Import started in the background."}))
    return 0


def _cmd_upload(host: str, data_dir: str, args: argparse.Namespace, argv: List[str]) -> int:
    url = _mcp_url()
    claim = _clean_claim(args.claim) if args.claim else ""
    ticket = ""
    if args._child:
        ticket = _take_child_ticket()
    elif claim and url:
        try:
            ticket = _with_retries(lambda: _claim_ticket(url, claim), budget=CLAIM_BUDGET)
        except NetworkBlocked:
            print(json.dumps({"error": "network_blocked", "message":
                              "Network is blocked here. Rerun this command with network access. "
                              "Nothing was uploaded."}))
            return 2
        except Unavailable:
            print(json.dumps({"error": "claim_timeout", "message":
                              "Substrate did not answer the import claim within about %d seconds, so "
                              "nothing was uploaded. Tell the user the server did not answer and ask "
                              "whether to try again later." % CLAIM_BUDGET}))
            return 2
        except Fatal:
            print(json.dumps({"error": "claim_failed", "message":
                              "The import claim was not accepted (it expires after 10 minutes and "
                              "works once; a server restart drops pending claims). Run --new-claim again, call "
                              "memory_import_ticket with the new claim, and run the upload once more."}))
            return 2
    else:
        # The ticket file is read first, so it is gone whatever happens next.
        ticket = _take_ticket(args.ticket_file)
    if not ticket or not url:
        print(json.dumps({"error": "missing_ticket", "message":
                          "Write the ticket from memory_import_ticket to the file given by "
                          "--ticket-file (the script deletes it) and set SUBSTRATE_MCP_URL before "
                          "uploading."}))
        return 2
    try:
        _check_url(url)
    except Fatal:
        print(json.dumps({"error": "invalid_url", "message": "SUBSTRATE_MCP_URL must be https."}))
        return 2
    current = _effective_status(data_dir)
    running_pid = current.get("pid")
    if current.get("state") in ("starting", "running") and running_pid != os.getpid() and \
            (running_pid is not None or not args._child):
        print(json.dumps({"error": "already_running", "message":
                          "An import is already running. Check it with --status."}))
        return 4
    if not args.all and not args.session:
        print(json.dumps({"error": "nothing_selected", "message": "Pass --all or --session <id>."}))
        return 2
    _record_connected(data_dir)
    if _decision(data_dir) is None:
        _record_decision(data_dir, "yes" if args.all else "picked")
    if args.background:
        return _spawn_background(argv, data_dir, ticket)
    excluded = _exclusions(data_dir, args.exclude_session or [], host)
    batch_id = uuid.uuid4().hex
    status = StatusWriter(data_dir, {
        "state": "running", "pid": os.getpid(), "started_at": _utc_now(), "batch_id": batch_id,
        "sessions_total": 0, "sessions_done": 0, "turns": 0, "items_sent": 0,
        "stored": 0, "duplicate": 0, "rejected": 0, "skipped_sessions": 0,
    })
    status.update()
    sessions = _catalogue(host, excluded, None if args.all else args.session)
    status.update(sessions_total=len(sessions))
    progress_path = os.path.join(data_dir, PROGRESS_FILE)
    sent_digests, sent_prints = _uploaded_state(data_dir)
    tally = Tally()
    turns_total = 0

    def on_wait(reason: str, seconds: float) -> None:
        status.update(note="Server busy (%s); retrying in %ds." % (reason, int(seconds)))

    def finish(state: str, message: str, code: int) -> int:
        summary = _import_summary(status.state.get("sessions_done", 0), tally)
        status.update(state=state, message=message, summary=summary, stored=tally.stored,
                      duplicate=tally.duplicate, rejected=tally.rejected,
                      sessions_sealed=tally.sealed, seal_failed=tally.seal_failed, note="")
        print(summary if state == "done" else message)
        return code

    client = McpClient(url, ticket)
    try:
        client.timeout = SETUP_TIMEOUT
        _with_retries(client.initialize, on_wait, budget=SETUP_BUDGET)
        client.timeout = REQUEST_TIMEOUT
        for entry in sessions:
            items = _session_items(host, entry)
            digest = _digest(items)
            turns_total += len(entry["turns"])
            status.update(uploading_session=entry["session_id"], turns=turns_total)
            if not args.force and sent_digests.get(entry["session_id"]) == digest:
                tally.duplicate += sum(1 for i in items if i["kind"] == "capture_turn")
                tally.sealed += sum(1 for i in items if i["kind"] == "capture_session")
                sent_prints[entry["session_id"]] = entry["fingerprint"]
                status.update(sessions_done=status.state["sessions_done"] + 1,
                              skipped_sessions=status.state["skipped_sessions"] + 1,
                              duplicate=tally.duplicate)
                continue
            for item in items:
                item["batch_id"] = batch_id
            for batch in _split_batches(items):
                _send_batch(client, batch, batch_id, tally, on_wait)
                status.update(items_sent=status.state["items_sent"] + len(batch),
                              stored=tally.stored, duplicate=tally.duplicate,
                              rejected=tally.rejected)
            sent_digests[entry["session_id"]] = digest
            sent_prints[entry["session_id"]] = entry["fingerprint"]
            try:
                _write_json(progress_path, {"sessions": sent_digests,
                                            "fingerprints": sent_prints})
            except OSError:
                pass
            status.update(sessions_done=status.state["sessions_done"] + 1)
    except AuthStop:
        return finish("stopped", "The import ticket expired or was revoked after %d of %d "
                      "conversations. Ask me to continue the import; conversations already "
                      "imported are skipped." % (status.state["sessions_done"], len(sessions)), 3)
    except NetworkBlocked:
        return finish("failed", "Network is blocked here. Rerun this command with network access. "
                      "Nothing new was imported.", 1)
    except Unavailable:
        return finish("failed", "Substrate did not answer, so the import stopped and nothing new was "
                      "imported. Ask me to try again later; conversations already imported are "
                      "skipped.", 1)
    except Fatal as error:
        if error.reason == "payment_required":
            return finish("failed", _billing_message(url) + " Nothing new was imported; "
                          "conversations already imported are skipped.", 1)
        return finish("failed", "The import stopped: %s. Ask me to try again later; "
                      "conversations already imported are skipped." % error.reason, 1)
    return finish("done", "", 0)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Substrate transcript sync")
    parser.add_argument("--host", required=True, choices=("claude", "codex"))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--session", action="append", default=[])
    parser.add_argument("--after-index", type=int, default=0)
    parser.add_argument("--origin", default="catchup",
                        choices=("catchup", "history_replay"))
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--exclude-session", action="append", default=[])
    parser.add_argument("--ticket-file", default="")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--wait", type=float, default=0.0)
    parser.add_argument("--offer-check", action="store_true")
    parser.add_argument("--record-decision", choices=DECISIONS)
    parser.add_argument("--record-connected", action="store_true")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--claim", default="")
    parser.add_argument("--new-claim", action="store_true")
    parser.add_argument("--mcp-url", default="")
    parser.add_argument("--pause", type=float, default=0.0)
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    if "--offer-check" in argv:
        try:
            args, _unknown = parser.parse_known_args(argv)
            return _cmd_offer_check(args.host, _data_dir(args.host, args.data_dir))
        except BaseException:  # noqa: BLE001 - a hook never fails the prompt
            return 0
    for hook_flag, hook_cmd in (("--signin-start", _cmd_signin_start), ("--stop-check", _cmd_stop_check)):
        if hook_flag in argv:
            # Hook entry points never exit non-zero: a Stop hook exiting 2 would block the turn.
            try:
                args, _unknown = parser.parse_known_args(argv)
                return hook_cmd(args.host, _data_dir(args.host, args.data_dir))
            except BaseException:  # noqa: BLE001 - fail open
                return 0
    args = parser.parse_args(argv)
    if args.new_claim:
        print(json.dumps({"claim": _new_claim()}))
        return 0
    if args.mcp_url:
        os.environ["SUBSTRATE_MCP_URL"] = args.mcp_url.strip()
    data_dir = _data_dir(args.host, args.data_dir)
    if args.pause:
        # Lets the agent wait for a browser sign-in without ending its turn.
        _sleep(max(0.0, min(args.pause, 60.0)))
        print(json.dumps({"paused": max(0.0, min(args.pause, 60.0))}))
        return 0
    if args.record_connected:
        # "reconnected": the disconnect notice ran on this computer before this successful check,
        # or the sign-in that this check closes was started on a computer that had been connected.
        reconnected = _take_disconnected(data_dir)
        reconnected = reconnected or _signin_load(data_dir).get("reconnect") is True
        _record_connected(data_dir)
        _signin_end(data_dir, "done")
        print(json.dumps({"connected": True, "reconnected": reconnected}))
        return 0
    if args.record_decision:
        _record_decision(data_dir, args.record_decision)
        print(json.dumps({"decision": args.record_decision}))
        return 0
    if args.status:
        return _cmd_status(data_dir, args.wait)
    if args.preview:
        return _cmd_preview(args.host, data_dir, args.exclude_session, args.force)
    if args.upload:
        return _cmd_upload(args.host, data_dir, args, argv)
    if args.list:
        return _cmd_list(args.host, data_dir, _exclusions(data_dir, args.exclude_session))
    if args.session:
        return _cmd_session(args.host, args.session[0], args.after_index, args.origin)
    parser.error("need --list, --session <id>, --preview, --upload, --status, "
                 "--offer-check or --record-decision")
    return 2


if __name__ == "__main__":
    sys.exit(main())
