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
                      [--exclude-session ID] [--background] [--force]
    substrate_sync.py --host H --status [--wait SECONDS]
    substrate_sync.py --host H --offer-check          (UserPromptSubmit hook)
    substrate_sync.py --host H --record-decision yes|no|picked|none
    substrate_sync.py --host H --record-connected     (after memory_search worked)
    substrate_sync.py --host H --pause SECONDS        (wait <= 60 s, e.g. for sign-in)
    (every mode also takes --data-dir DIR)

Upload reads the ticket from SUBSTRATE_IMPORT_TICKET and the endpoint from
SUBSTRATE_MCP_URL (both from the ``memory_import_ticket`` tool), never argv.

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
import json
import math
import os
import re
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
# bytes, <= 64 tool calls per message, <= 4096 messages per turn). Items that
# already fit are returned unchanged, so their deterministic ids never move.
# ---------------------------------------------------------------------------

MAX_ITEM_BYTES = MAX_BATCH_BYTES - 1024
MAX_CALLS_PER_MESSAGE = 64
MAX_MESSAGES_PER_ITEM = 4096


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
            size = os.path.getsize(path)
        except OSError:
            size = 0
        sessions.append({"session_id": session_id, "path": path, "turns": turns,
                         "first_at": first, "last_at": last, "bytes": size})
    sessions.sort(key=lambda s: (s["first_at"] or "", s["session_id"]))
    return sessions


def _cmd_list(host: str, exclude: set) -> int:
    for entry in _catalogue(host, exclude):
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


def _cmd_preview(host: str, data_dir: str, explicit: List[str]) -> int:
    excluded = _exclusions(data_dir, explicit, host)
    sessions = _catalogue(host, excluded)
    turns = sum(len(s["turns"]) for s in sessions)
    firsts = [s["first_at"] for s in sessions if s["first_at"]]
    lasts = [s["last_at"] for s in sessions if s["last_at"]]
    summary = {
        "sessions": len(sessions),
        "turns": turns,
        "first_at": min(firsts) if firsts else None,
        "last_at": max(lasts) if lasts else None,
        "bytes_estimate": sum(s["bytes"] for s in sessions),
        "excluded": sorted(excluded),
        "decision": _decision(data_dir),
    }
    print(json.dumps(summary, ensure_ascii=False))
    if sessions:
        sys.stderr.write("Found %d past conversation%s (%d turns) from %s to %s.\n" % (
            len(sessions), "" if len(sessions) == 1 else "s", turns,
            _human_date(summary["first_at"]), _human_date(summary["last_at"])))
    else:
        sys.stderr.write("No past conversations found on this computer.\n")
    return 0


# ---------------------------------------------------------------------------
# Offer check (UserPromptSubmit command hook). Silent once decided. Never
# fails visibly: any problem means "say nothing" and exit 0.
# ---------------------------------------------------------------------------

def _quote(value: str) -> str:
    if os.name == "nt":
        value = value.replace("\\", "/")
    return '"%s"' % value.replace('"', '\\"')


def _has_other_transcripts(host: str, current: str) -> bool:
    root = _base_dir(host)
    for dirpath, dirnames, filenames in os.walk(root):
        if host == "claude":
            dirnames[:] = [d for d in dirnames if d != "subagents"]
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            if host == "claude" and name.startswith("agent-"):
                continue
            stem = name[:-6]
            if current and (stem == current or stem.endswith(current)):
                continue
            try:
                if os.path.getsize(os.path.join(dirpath, name)) > 0:
                    return True
            except OSError:
                continue
    return False


def _offer_text(host: str, data_dir: str, session_id: str) -> str:
    script = os.path.abspath(__file__)
    base = "%s %s --host %s --data-dir %s" % (
        _quote(sys.executable or "python3"), _quote(script), host, _quote(data_dir))
    if session_id:
        base += " --exclude-session %s" % _quote(session_id)
    return (
        "[substrate] Import offer pending (asked once per computer). Ignore this note until "
        "the Substrate memory tools work in this session. Once memory_search has succeeded "
        "and you have said \"Connected to Substrate.\", in that same reply and without "
        "waiting for the user: run `SYNC --preview` and offer, in plain words with its "
        "conversation count and date range: Import all / Let me pick / Not now. "
        "SYNC means: `%(base)s`.\n"
        "Import all: call memory_import_ticket {\"platform\": \"%(host)s\"}, run "
        "`SYNC --upload --all --background` with the environment variables "
        "SUBSTRATE_IMPORT_TICKET and SUBSTRATE_MCP_URL set from its result (never show the "
        "ticket in your reply), then keep running `SYNC --status --wait 60` and report each "
        "message until it is done; end with its summary line. Do not stop and wait for the "
        "user while it runs.\n"
        "Let me pick: `SYNC --list`, show titles and dates, then upload the same way with "
        "`--session <id>` per chosen conversation instead of `--all`.\n"
        "Not now: `SYNC --record-decision no`; say they can ask \"import my past "
        "conversations\" any time."
    ) % {"base": base, "host": host}


def _setup_text(host: str, data_dir: str, session_id: str, offer: bool) -> str:
    script = os.path.abspath(__file__)
    base = "%s %s --host %s --data-dir %s" % (
        _quote(sys.executable or "python3"), _quote(script), host, _quote(data_dir))
    if session_id:
        base += " --exclude-session %s" % _quote(session_id)
    text = (
        "[substrate] Substrate setup is not finished on this computer. Finish it now, in this "
        "reply, before anything else, in a few short lines. A bare \"continue\", \"done\", "
        "\"ok\" or \"approved\" from the user just means: carry on with Substrate setup. Do not "
        "comment on it or ask what to work on. SYNC means: `%(base)s`.\n"
        "1. Call memory_search with a short, non-secret query. If it works (even empty): say "
        "\"Connected to Substrate.\" and run `SYNC --record-connected`.\n"
        "2. If it needs sign-in (only an `authenticate` tool for substrate-memory exists, or "
        "memory_search is missing or not connected): call that authenticate tool and tell the "
        "user exactly: \"Open this link, sign in and choose Approve connection. I'll continue "
        "automatically.\" plus the link. Do not repeat the tool's notes about localhost "
        "connection errors or pasting URLs; mention pasting the address-bar URL only if the "
        "user says the page showed an error (remote or SSH machines). Then do NOT end your "
        "turn: run `SYNC --pause 15`, then call memory_search again (load it with ToolSearch if "
        "it is deferred); repeat up to 20 times (about 5 minutes). As soon as it works, do "
        "step 1 and continue. Only if it never works, ask them to say \"done\" once they have "
        "approved. Never ask for a token or key.\n"
        "3. If no Substrate memory tools exist at all, tell the user to type /reload-plugins "
        "and then say \"continue\"."
    ) % {"base": base}
    if offer:
        text += ("\n4. Right after \"Connected to Substrate.\", in the same reply: run "
                 "`SYNC --preview` and, if it finds conversations, offer Import all / Let me "
                 "pick / Not now with its count and date range (/substrate-claude:substrate-import "
                 "or $substrate-import has the steps).")
    return text


def _cmd_offer_check(host: str, data_dir: str) -> int:
    try:
        session_id = ""
        payload = {}  # type: Dict[str, Any]
        if not sys.stdin.isatty():
            raw = sys.stdin.read(1 << 20)
            try:
                parsed = json.loads(raw) if raw.strip() else {}
                payload = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                payload = {}
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
        if connected and decided:
            return 0
        if not decided and not _has_other_transcripts(host, session_id):
            try:
                _record_decision(data_dir, "none")
            except OSError:
                pass
            decided = True
        if connected and decided:
            return 0
        if connected:
            context = _offer_text(host, data_dir, session_id)
        else:
            context = _setup_text(host, data_dir, session_id, offer=not decided)
        output = {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }}
        sys.stdout.write(json.dumps(output, ensure_ascii=False))
        sys.stdout.flush()
    except BaseException:  # noqa: BLE001 - a hook never fails the prompt
        return 0
    return 0


# ---------------------------------------------------------------------------
# Direct upload over MCP Streamable HTTP with an import ticket.
# ---------------------------------------------------------------------------

USER_AGENT = "substrate-sync/0.9.0"
MAX_RETRIES = 8
REQUEST_TIMEOUT = 120.0


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
            with self._opener.open(request, timeout=REQUEST_TIMEOUT) as response:
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
            if code in (408, 425, 429) or 500 <= code <= 599:
                raise Retryable("http_%d" % code, _retry_after(error.headers)) from None
            if 300 <= code < 400:
                raise Fatal("redirect_refused") from None
            raise Fatal("http_%d" % code) from None
        except (urllib.error.URLError, OSError, socket.timeout) as error:
            raise Retryable("network: %s" % type(error).__name__) from None

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


def _with_retries(action, on_wait=None):  # noqa: ANN001, ANN202
    attempt = 0
    while True:
        try:
            return action()
        except Retryable as error:
            attempt += 1
            if attempt > MAX_RETRIES:
                raise Fatal("server_unavailable (%s)" % error.reason) from None
            wait = _backoff(attempt, error.retry_after)
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
            on_wait,
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


def _ticket_env() -> Tuple[str, str]:
    ticket = os.environ.get("SUBSTRATE_IMPORT_TICKET", "").strip()
    url = os.environ.get("SUBSTRATE_MCP_URL", "").strip()
    return ticket, url


def _spawn_background(argv: List[str], data_dir: str) -> int:
    os.makedirs(data_dir, exist_ok=True)
    command = [sys.executable, os.path.abspath(__file__)] + [a for a in argv if a != "--background"]
    StatusWriter(data_dir, {"state": "starting", "pid": None, "started_at": _utc_now()}).update()
    log = open(os.path.join(data_dir, LOG_FILE), "ab")
    kwargs = {}  # type: Dict[str, Any]
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 cwd=data_dir, env=dict(os.environ), close_fds=True, **kwargs)
    finally:
        log.close()
    status_path = os.path.join(data_dir, STATUS_FILE)
    print(json.dumps({"started": True, "pid": child.pid, "status_file": status_path,
                      "message": "Import started in the background."}))
    return 0


def _cmd_upload(host: str, data_dir: str, args: argparse.Namespace, argv: List[str]) -> int:
    ticket, url = _ticket_env()
    if not ticket or not url:
        print(json.dumps({"error": "missing_ticket", "message":
                          "Set SUBSTRATE_IMPORT_TICKET and SUBSTRATE_MCP_URL from "
                          "memory_import_ticket before uploading."}))
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
        return _spawn_background(argv + ["--_child"], data_dir)
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
    progress = _read_json(progress_path)
    sent_digests = progress.get("sessions") if isinstance(progress.get("sessions"), dict) else {}
    tally = Tally()
    turns_total = 0

    def on_wait(reason: str, seconds: float) -> None:
        status.update(note="Server busy (%s); retrying in %ds." % (reason, int(seconds)))

    def finish(state: str, message: str, code: int) -> int:
        summary = "Imported %d session%s (%d turns): %d stored, %d duplicate, %d rejected." % (
            status.state.get("sessions_done", 0),
            "" if status.state.get("sessions_done", 0) == 1 else "s",
            turns_total, tally.stored, tally.duplicate, tally.rejected)
        status.update(state=state, message=message, summary=summary, stored=tally.stored,
                      duplicate=tally.duplicate, rejected=tally.rejected,
                      sessions_sealed=tally.sealed, seal_failed=tally.seal_failed, note="")
        print(summary if state == "done" else message)
        return code

    client = McpClient(url, ticket)
    try:
        _with_retries(client.initialize, on_wait)
        for entry in sessions:
            items = []  # type: List[Dict[str, Any]]
            for item in _build_envelopes(entry["session_id"], entry["turns"],
                                         "history_replay", 0):
                items.extend(_fit_item(item))
            if items:
                # Last, after every turn: seal the session so it is extracted.
                items.append(_seal_item(entry["session_id"], items, host))
            digest = hashlib.sha256(_canonical_bytes(items)).hexdigest()
            turns_total += len(entry["turns"])
            status.update(uploading_session=entry["session_id"], turns=turns_total)
            if not args.force and sent_digests.get(entry["session_id"]) == digest:
                tally.duplicate += sum(1 for i in items if i["kind"] == "capture_turn")
                tally.sealed += sum(1 for i in items if i["kind"] == "capture_session")
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
            try:
                _write_json(progress_path, {"sessions": sent_digests})
            except OSError:
                pass
            status.update(sessions_done=status.state["sessions_done"] + 1)
    except AuthStop:
        return finish("stopped", "The import ticket expired or was revoked after %d of %d "
                      "conversations. Ask me to continue the import; conversations already "
                      "imported are skipped." % (status.state["sessions_done"], len(sessions)), 3)
    except Fatal as error:
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
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--wait", type=float, default=0.0)
    parser.add_argument("--offer-check", action="store_true")
    parser.add_argument("--record-decision", choices=DECISIONS)
    parser.add_argument("--record-connected", action="store_true")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--pause", type=float, default=0.0)
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    if "--offer-check" in argv:
        try:
            args, _unknown = parser.parse_known_args(argv)
            return _cmd_offer_check(args.host, _data_dir(args.host, args.data_dir))
        except BaseException:  # noqa: BLE001 - a hook never fails the prompt
            return 0
    args = parser.parse_args(argv)
    data_dir = _data_dir(args.host, args.data_dir)
    if args.pause:
        # Lets the agent wait for a browser sign-in without ending its turn.
        _sleep(max(0.0, min(args.pause, 60.0)))
        print(json.dumps({"paused": max(0.0, min(args.pause, 60.0))}))
        return 0
    if args.record_connected:
        _record_connected(data_dir)
        print(json.dumps({"connected": True}))
        return 0
    if args.record_decision:
        _record_decision(data_dir, args.record_decision)
        print(json.dumps({"decision": args.record_decision}))
        return 0
    if args.status:
        return _cmd_status(data_dir, args.wait)
    if args.preview:
        return _cmd_preview(args.host, data_dir, args.exclude_session)
    if args.upload:
        return _cmd_upload(args.host, data_dir, args, argv)
    if args.list:
        return _cmd_list(args.host, _exclusions(data_dir, args.exclude_session))
    if args.session:
        return _cmd_session(args.host, args.session[0], args.after_index, args.origin)
    parser.error("need --list, --session <id>, --preview, --upload, --status, "
                 "--offer-check or --record-decision")
    return 2


if __name__ == "__main__":
    sys.exit(main())
