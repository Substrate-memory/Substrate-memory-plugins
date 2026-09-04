#!/usr/bin/env python3
"""Codex hook entry point for Substrate memory (standard library only).

Modes (first argv element)::

    pre      UserPromptSubmit: print bounded ``<memory-context>`` (or the
             one-time onboarding link) as ``hookSpecificOutput`` JSON.
    post     PostToolUse / turn end: spool a best-effort completed-turn
             capture and return immediately.
    session  SessionStart: queue a ``capture_session`` marker.

Wire contract (verified empirically against ``codex-cli 0.144.5``; see
``README.md`` "Hook contract: verified vs best-effort" for the exact
method and the parts that remain best-effort):

- Hook stdin is JSON with snake_case fields. ``hook_event_name`` is a
  PascalCase const (``"UserPromptSubmit"``, ``"PostToolUse"``,
  ``"SessionStart"``, ...). ``session_id`` is always present;
  ``UserPromptSubmit`` additionally carries ``prompt`` and ``turn_id``;
  ``PostToolUse`` carries ``tool_name``, ``tool_input``, ``tool_response``,
  ``tool_use_id`` and ``turn_id``; ``SessionStart`` carries ``source``
  (``startup|resume|clear|compact``) and no ``turn_id``. ``transcript_path``
  is a nullable path to the session rollout JSONL file.
- Hook stdout must be the enveloped form
  ``{"hookSpecificOutput": {"hookEventName": ..., "additionalContext": ...}}``.
  The bare ``{"additionalContext": ...}`` form has no slot in the CLI's
  output schema and may be dropped, so it is never emitted.
- ``hooks/hooks.json`` is discovered from the installed plugin directory.
  Its top level accepts only ``description``/``hooks`` (a ``$schema`` key
  breaks parsing); handler-level keys are tolerated.

This script always exits 0 and never prints secrets: only the
``hookSpecificOutput`` JSON (already redacted) goes to stdout, and only
hook/mode names and byte sizes go to stderr.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from substrate_core import runtime  # noqa: E402

# Idempotency namespace for stable per-turn / per-boundary event ids. When a
# hook payload carries both session and turn identity, the spooled envelope
# reuses one deterministic UUID (uuid5) so repeated fires of the same turn
# (Codex fires PostToolUse per tool call) share one Idempotency-Key and the
# server cannot record duplicate captures.
_IDEMPOTENCY_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

# Best-effort SessionStart.source -> capture_session boundary mapping. Codex
# reports how the session started; the server records how the previous
# context ended. "switch" (this session became active) is the default; it is
# also used when the source is absent or unknown. Documented as best-effort
# in README.md.
_SOURCE_BOUNDARIES = {
    "startup": "switch",
    "resume": "switch",
    "clear": "reset",
    "compact": "compress",
}

# Output events whose schema carries hookSpecificOutput.additionalContext.
_CONTEXT_EVENTS = frozenset({"UserPromptSubmit", "PreToolUse", "PostToolUse", "SessionStart"})

# Spool bounds (Hermes parity: the in-process worker holds 64 envelopes and
# drops new work when full; the hook process is ephemeral so the queue lives
# on disk instead). Files older than _MAX_SPOOL_AGE_SECONDS are deleted
# unsent by the sweeper.
_MAX_SPOOL_FILES = 64


def _log(message: str) -> None:
    sys.stderr.write(f"[substrate-hook] {message}\n")
    sys.stderr.flush()


def _read_stdin_json() -> Any:
    try:
        raw = sys.stdin.read(1_048_576)
    except Exception:
        return None
    if not raw or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        return None


def _dig(payload: Any, *keys: str) -> Any:
    for key in keys:
        if isinstance(payload, dict) and payload.get(key) not in (None, ""):
            return payload[key]
    return ""


def _extract_text(payload: Any, *keys: str) -> str:
    value = _dig(payload, *keys)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [item for item in value if isinstance(item, str)]
        if parts:
            return "\n".join(parts)
    return ""


def _session_id(payload: Any) -> str:
    # "session_id" is the verified Codex field; the rest are legacy
    # best-effort fallbacks for older/alternate shapes.
    for key in ("session_id", "conversation_id", "thread_id"):
        value = _dig(payload, key)
        if isinstance(value, str) and value:
            return value
    return ""


def _turn_id(payload: Any) -> str:
    value = _dig(payload, "turn_id")
    return value if isinstance(value, str) else ""


def _hook_event_name(payload: Any) -> str:
    value = _dig(payload, "hook_event_name")
    return value if isinstance(value, str) else ""


def _history(payload: Any) -> list[Any]:
    for key in ("conversation_history", "history", "messages", "transcript"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, list):
            return value
    return []


def _transcript_path(payload: Any) -> str:
    # Verified nullable field pointing at the session rollout JSONL file.
    value = payload.get("transcript_path") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value else ""


def _read_transcript_messages(transcript_path: str) -> list[dict[str, str]]:
    """Best-effort tail read of the Codex rollout JSONL transcript.

    Returns ``[{"role": ..., "content": ...}]`` for recent user/assistant
    turns, newest last. The rollout schema is large and version-sensitive, so
    only ``response_item`` rows shaped like
    ``payload: {type: "message", role: "user"|"assistant",
    content: [{type: "input_text"|"output_text"|"text", text: ...}]}``
    are used; everything else is skipped. Any error (missing file, bad
    JSON, oversized file) yields ``[]`` and the caller falls back to the
    hook payload fields. Never raises, never logs file contents.
    """
    messages: list[dict[str, str]] = []
    try:
        path = Path(transcript_path)
        if not path.is_file():
            return []
        if path.stat().st_size > 32 * 1024 * 1024:
            return []
        with path.open("rb") as handle:
            try:
                handle.seek(-262_144, 2)
            except OSError:
                handle.seek(0)
            tail = handle.read(262_144 + 4096).decode("utf-8", "ignore")
        lines = tail.splitlines()[-400:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("type") != "response_item":
                continue
            item = row.get("payload")
            if not isinstance(item, dict):
                continue
            if item.get("type") != "message":
                continue
            role = item.get("role")
            if role not in ("user", "assistant"):
                continue
            parts: list[str] = []
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") not in ("input_text", "output_text", "text"):
                        continue
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text[:8192])
            if not parts:
                continue
            text = "\n".join(parts)
            # Skip injected environment context; it is host noise, not memory.
            if role == "user" and text.lstrip().startswith("<environment_context>"):
                continue
            messages.append({"role": role, "content": text})
        return messages[-20:]
    except Exception:
        return []


def _stable_event_id(*parts: str) -> str:
    return str(uuid.uuid5(_IDEMPOTENCY_NAMESPACE, "\x00".join(parts)))


def _emit_context(event_name: str, text: str) -> None:
    # Exact enveloped shape the CLI schema requires: hookSpecificOutput with
    # the per-event hookEventName const carrying additionalContext. No other
    # top-level keys (the output schema is closed).
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": text,
                }
            }
        )
        + "\n"
    )
    sys.stdout.flush()


def _context_event(payload: Any, default: str) -> str:
    name = _hook_event_name(payload)
    if name in _CONTEXT_EVENTS:
        return name
    return default


def run_pre(payload: Any) -> int:
    # "prompt" is the verified UserPromptSubmit field; the rest are legacy
    # best-effort fallbacks.
    prompt = _extract_text(payload, "prompt", "user_message", "user_prompt", "input", "text")
    history = _history(payload)
    if not history:
        history = _read_transcript_messages(_transcript_path(payload))
    try:
        text = runtime.codex_pre_turn_text(
            session_id=_session_id(payload),
            user_message=prompt,
            conversation_history=history,
            turn_id=_turn_id(payload),
        )
    except Exception:
        return 0
    if text:
        _emit_context(_context_event(payload, "UserPromptSubmit"), text)
        _log(f"pre: injected {len(text.encode('utf-8'))} bytes")
    return 0


def _spool_dir() -> Path | None:
    try:
        home = runtime.onboarding.active_home()
    except Exception:
        return None
    return home / "substrate" / "spool-codex"


def _spool_envelope(envelope: dict[str, Any]) -> None:
    spool = _spool_dir()
    if spool is None:
        return
    try:
        spool.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(spool, 0o700)
    except OSError:
        return
    event_id = envelope.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return
    try:
        data = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    path = spool / f"{event_id}.json"
    # Stable filename per event id: repeated PostToolUse fires for one turn
    # overwrite the same file (last-write-wins), so the on-disk queue holds
    # at most one envelope per turn and the sweeper can only deliver one
    # capture per Idempotency-Key.
    try:
        if len(list(spool.glob("*.json"))) >= _MAX_SPOOL_FILES and not path.exists():
            _log("spool full, dropping envelope")
            return
    except OSError:
        return
    tmp = spool / f".{event_id}.{os.getpid()}.tmp"
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            raw = data.encode("utf-8")
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(path))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return
    worker = PLUGIN_ROOT / "hooks" / "_send_spooled.py"
    try:
        # The hook itself never waits: delivery plus a bounded sweep of older
        # spool files happens in this detached child.
        subprocess.Popen(
            [sys.executable, str(worker), str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(PLUGIN_ROOT),
        )
    except Exception:
        pass


def _stabilize_turn_envelope(
    envelope: dict[str, Any] | None, session_id: str, turn_id: str
) -> dict[str, Any] | None:
    """Bind a stable Idempotency-Key per turn envelope.

    Returns the envelope with ``event_id`` replaced by a deterministic UUID
    derived from (session_id, turn_id) when both are present, re-validated
    against the vendored contract. Falls back to the original envelope when
    identity is missing or re-validation fails (fail closed: a valid
    at-most-once capture beats a dropped one).
    """
    if envelope is None or not session_id or not turn_id:
        return envelope
    assert isinstance(envelope, dict)
    original = envelope.get("event_id")
    stable = _stable_event_id("capture_turn", session_id, turn_id)
    envelope["event_id"] = stable
    try:
        runtime.contract.validate_envelope(envelope, idempotency_key=stable)
    except Exception:
        # Fail closed toward delivery: keep the valid random-id envelope
        # rather than dropping the capture.
        envelope["event_id"] = original
    return envelope


def _stabilize_session_envelope(
    envelope: dict[str, Any] | None, session_id: str, boundary: str
) -> dict[str, Any] | None:
    if envelope is None or not session_id:
        return envelope
    assert isinstance(envelope, dict)
    original = envelope.get("event_id")
    stable = _stable_event_id("capture_session", session_id, boundary)
    envelope["event_id"] = stable
    try:
        runtime.contract.validate_envelope(envelope, idempotency_key=stable)
    except Exception:
        envelope["event_id"] = original
    return envelope


def run_post(payload: Any) -> int:
    try:
        sid = _session_id(payload)
        turn = _turn_id(payload)
        messages = _history(payload)
        if not messages:
            messages = _read_transcript_messages(_transcript_path(payload))
        # "last_assistant_message" is the verified nullable PostToolUse
        # field; tool_input/tool_response describe one tool call, not the
        # turn, so they are not used as turn text.
        assistant = _extract_text(payload, "last_assistant_message")
        envelope = runtime._capture_envelope(
            "",
            assistant,
            session_id=sid,
            messages=messages,
            turn_id=turn,
        )
        envelope = _stabilize_turn_envelope(envelope, sid, turn)
    except Exception:
        return 0
    if envelope is not None:
        _spool_envelope(envelope)
        _log("post: capture spooled")
    return 0


def _source_boundary(payload: Any, argv_boundary: str) -> str:
    # Verified "source" wins; an explicit valid argv boundary is the legacy
    # fallback; otherwise the documented default "switch".
    source = _dig(payload, "source")
    if isinstance(source, str) and source in _SOURCE_BOUNDARIES:
        return _SOURCE_BOUNDARIES[source]
    if argv_boundary in runtime.contract.BOUNDARIES:
        return argv_boundary
    return "switch"


def run_session(payload: Any, boundary: str) -> int:
    try:
        sid = _session_id(payload)
        resolved = _source_boundary(payload, boundary)
        envelope = runtime._session_envelope(sid, resolved)
        envelope = _stabilize_session_envelope(envelope, sid, resolved)
    except Exception:
        return 0
    if envelope is not None:
        _spool_envelope(envelope)
        _log(f"session: {resolved} marker spooled")
    return 0


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "pre"
    boundary = argv[2] if len(argv) > 2 else "switch"
    payload = _read_stdin_json()
    start = time.monotonic()
    try:
        if mode == "post":
            return run_post(payload)
        if mode == "session":
            if boundary not in runtime.contract.BOUNDARIES:
                boundary = "switch"
            return run_session(payload, boundary)
        return run_pre(payload)
    finally:
        _log(f"mode={mode} elapsed_ms={int((time.monotonic() - start) * 1000)}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
