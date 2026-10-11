"""Import past Hermes conversations from the active profile's ``state.db``.

After the first connection the plugin offers, once, to add the profile's
earlier conversations to Substrate memory. This module owns that:

- reading ``<profile>/state.db`` read-only, in short keyset-paged queries
  (never one long transaction, never a whole session or message in memory:
  message text is cut in SQL before Python sees it), across Hermes schema
  versions by column detection;
- choosing what to import: every human conversation except the current
  live one(s), automated runs (cron, batch, subagent, webhook, ...),
  group chats, hidden and empty sessions;
- building ``capture_turn`` / ``capture_session`` envelopes with
  ``capture_origin: history_replay`` and deterministic event ids, so a
  rerun or a resume yields ``duplicate`` instead of a second copy, redacted
  by the same helpers as live capture;
- delivering them through the profile spool at replay priority (live turns
  always go first) with backpressure, a checkpoint after every session and
  every few turns, and an exclusive lock so one import runs at a time;
- the offer decision in ``<profile>/substrate/import-offer.json`` and the
  progress in ``<profile>/substrate/import.json``.

Standard library only.
"""

from __future__ import annotations

import contextlib
import hashlib
import heapq
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import contract

STATE_DB_NAME = "state.db"
OFFER_FILE = "import-offer.json"
JOB_FILE = "import.json"
LOCK_FILE = "import.lock"
DECISIONS = frozenset({"yes", "no", "picked", "none"})

# Sessions Hermes starts on its own, not with the user. Legacy
# substrate_wiki excluded the same family (cron, batch, flush, test,
# webhook, worker); current Hermes adds subagent, kanban and webhook kinds.
EXCLUDED_SOURCES = frozenset({
    "batch", "cron", "flush", "test", "webhook", "msgraph_webhook", "worker",
    "subagent", "delegate", "kanban", "tool",
})
GROUP_CHAT_TYPES = frozenset({"channel", "group", "groupchat", "room", "supergroup", "forum"})
# An open session with a message this recent is the live one: the plugin
# captures it turn by turn (each live turn carries the whole conversation).
ACTIVE_WINDOW_SECONDS = 30 * 60
SESSION_PAGE = 200
MESSAGE_PAGE = 500
# Message text is cut in SQL; the envelope keeps at most 32 KiB per message.
SQL_TEXT_CHARS = 65_536
# Below the 256 KiB event cap with room for the memory_import request.
MAX_IMPORT_EVENT_BYTES = 200 * 1024
# Replay items allowed in the spool at once: live turns keep their room.
WINDOW_ITEMS = 64
WINDOW_BYTES = 2 * 1024 * 1024
CHECKPOINT_EVERY = 32
# Stop waiting (resumable) when Substrate accepts nothing for this long, e.g.
# offline or a revoked key; the next message or start continues the import.
STALL_SECONDS = 600.0
MAX_PICKED = 500
SPEAKER = {"id": "user", "role": "owner", "display": ""}


class HistoryError(RuntimeError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _iso(epoch: Any) -> str:
    try:
        value = float(epoch)
        moment = datetime.fromtimestamp(value, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _day(epoch: Any) -> str:
    """Plain date for people: "March 3, 2026"."""
    try:
        moment = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return f"{moment.strftime('%B')} {moment.day}, {moment.year}"


def _now_iso() -> str:
    return _iso(time.time())


# Fallback event time when a row has none: fixed, so a resent event is
# byte-identical (the server answers a different body under the same event id
# with a conflict, not a duplicate).
_NO_TIME = "1970-01-01T00:00:00.000000Z"


def profile_batch_id(home: Path) -> str:
    """One stable batch id per profile, for the same reason: every rerun and
    resume sends identical bodies, so Substrate answers ``duplicate``."""
    try:
        name = str(Path(home).resolve())
    except OSError:
        name = str(home)
    return hashlib.sha256(f"substrate-hermes-history\0{name}".encode()).hexdigest()[:16]


def substrate_dir(home: Path) -> Path:
    return Path(home) / "substrate"


def _read_json(path: Path, maximum: int = 262_144) -> dict[str, Any] | None:
    try:
        info = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink():
        raise HistoryError("refusing_symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


# ---------------------------------------------------------------------------
# Offer decision
# ---------------------------------------------------------------------------


def read_decision(home: Path) -> str:
    """The recorded answer to the import offer, or "" when not asked yet."""
    value = _read_json(substrate_dir(home) / OFFER_FILE, 4096) or {}
    decision = value.get("decision")
    return decision if isinstance(decision, str) and decision in DECISIONS else ""


def record_decision(home: Path, decision: str) -> None:
    if decision not in DECISIONS:
        raise ValueError(decision)
    _write_json(substrate_dir(home) / OFFER_FILE,
                {"version": 1, "decision": decision, "decided_at": _now_iso()})


# ---------------------------------------------------------------------------
# state.db reader (read-only, schema-tolerant, bounded)
# ---------------------------------------------------------------------------


class StateDB:
    """Read one Hermes ``state.db`` through short read-only queries.

    Works from the oldest released layout (schema 4: no ``active``,
    ``chat_type`` or ``hidden`` columns) to the current one (schema 26-30:
    soft-archived rewinds, compaction, hidden scaffolding rows) by checking
    which columns exist instead of trusting a version number.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._columns: dict[str, set[str]] = {}

    def exists(self) -> bool:
        try:
            return self.path.is_file() and not self.path.is_symlink()
        except OSError:
            return False

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if not self.exists():
            raise HistoryError("no_history")
        try:
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0
            )
        except sqlite3.Error as exc:
            raise HistoryError("unreadable") from exc
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            yield connection
        except sqlite3.Error as exc:
            raise HistoryError("unreadable") from exc
        finally:
            connection.close()

    def columns(self, table: str) -> set[str]:
        if table not in self._columns:
            with self.connect() as connection:
                self._columns[table] = {
                    str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
                }
        return self._columns[table]

    def schema_version(self) -> int:
        try:
            with self.connect() as connection:
                row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        except (HistoryError, sqlite3.Error):
            return 0
        try:
            return int(row[0]) if row else 0
        except (TypeError, ValueError):
            return 0

    def check(self) -> None:
        sessions, messages = self.columns("sessions"), self.columns("messages")
        if not {"id", "source"} <= sessions or not {"session_id", "role", "content"} <= messages:
            raise HistoryError("unsupported_schema")

    def message_filter(self, alias: str = "m") -> str:
        """The conversation the user saw: no rewound or superseded rows, no
        compaction summaries, no hidden scaffolding; compacted originals stay."""
        columns = self.columns("messages")
        parts = [f"{alias}.role IN ('user','assistant','tool')"]
        if "active" in columns:
            parts.append(
                f"({alias}.active = 1 OR {alias}.compacted = 1)" if "compacted" in columns
                else f"{alias}.active = 1"
            )
        if "_compressed_summary" in columns:
            parts.append(f"COALESCE({alias}._compressed_summary, 0) = 0")
        if "display_kind" in columns:
            parts.append(f"COALESCE({alias}.display_kind, '') <> 'hidden'")
        return " AND ".join(parts)

    def iter_sessions(self, after: tuple[float, str] | None = None) -> Iterator[dict[str, Any]]:
        """Sessions oldest first with message counts, in short keyset pages."""
        self.check()
        columns = self.columns("sessions")
        has_time = "timestamp" in self.columns("messages")
        wanted = [c for c in ("chat_type", "ended_at", "hidden", "title") if c in columns]
        start = "s.started_at" if "started_at" in columns else "0"
        last = "MAX(m.timestamp)" if has_time else "NULL"
        first = "MIN(m.timestamp)" if has_time else "NULL"
        select = ", ".join(
            ["s.id AS id", "s.source AS source", f"{start} AS started_at"]
            + [f"s.{c} AS {c}" for c in wanted]
        )
        where = self.message_filter()
        cursor = after
        while True:
            condition, params = "", []
            if cursor is not None:
                condition = f"WHERE ({start} > ? OR ({start} = ? AND s.id > ?))"
                params = [cursor[0], cursor[0], cursor[1]]
            sql = (
                f"SELECT {select}, "
                f"(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id AND {where}) AS messages, "
                f"(SELECT {first} FROM messages m WHERE m.session_id = s.id AND {where}) AS first_at, "
                f"(SELECT {last} FROM messages m WHERE m.session_id = s.id AND {where}) AS last_at "
                f"FROM sessions s {condition} ORDER BY {start}, s.id LIMIT {SESSION_PAGE}"
            )
            with self.connect() as connection:
                rows = [dict(row) for row in connection.execute(sql, params)]
            for row in rows:
                row["id"] = str(row.get("id") or "")
                try:
                    row["started_at"] = float(row.get("started_at") or 0)
                except (TypeError, ValueError):
                    row["started_at"] = 0.0
                yield row
            if len(rows) < SESSION_PAGE:
                return
            cursor = (rows[-1]["started_at"], rows[-1]["id"])

    def iter_messages(self, session_id: str) -> Iterator[dict[str, Any]]:
        """One session's conversation in order, text cut to SQL_TEXT_CHARS."""
        columns = self.columns("messages")
        optional = [c for c in ("tool_call_id", "tool_name", "timestamp") if c in columns]
        select = [
            "m.id AS _row", "m.role AS role",
            f"CASE WHEN typeof(m.content) = 'text' THEN substr(m.content, 1, {SQL_TEXT_CHARS}) "
            "WHEN m.content IS NULL THEN '' ELSE '[binary content omitted]' END AS content",
            f"length(m.content) > {SQL_TEXT_CHARS} AS _cut",
        ]
        if "tool_calls" in columns:
            select.append(
                f"CASE WHEN length(m.tool_calls) <= {SQL_TEXT_CHARS} THEN m.tool_calls END AS tool_calls"
            )
        select += [f"m.{c} AS {c}" for c in optional]
        where = self.message_filter()
        last = -1
        while True:
            sql = (
                f"SELECT {', '.join(select)} FROM messages m "
                f"WHERE m.session_id = ? AND m.id > ? AND {where} ORDER BY m.id LIMIT {MESSAGE_PAGE}"
            )
            with self.connect() as connection:
                rows = [dict(row) for row in connection.execute(sql, (session_id, last))]
            yield from rows
            if len(rows) < MESSAGE_PAGE:
                return
            last = int(rows[-1]["_row"])


def state_db_path(home: Path) -> Path:
    return Path(home) / STATE_DB_NAME


# ---------------------------------------------------------------------------
# What to import
# ---------------------------------------------------------------------------


def classify(row: dict[str, Any], *, exclude: set[str], now: float) -> str:
    """"" when the session is eligible, else the reason it is left out."""
    sid = row.get("id") or ""
    if not sid or len(sid.encode("utf-8")) > contract.MAX_SESSION_ID_BYTES:
        return "invalid"
    if sid in exclude:
        return "current"
    source = str(row.get("source") or "").strip().casefold()
    if source in EXCLUDED_SOURCES:
        return "automated"
    if str(row.get("chat_type") or "").strip().casefold() in GROUP_CHAT_TYPES:
        return "group"
    if row.get("hidden"):
        return "hidden"
    if not int(row.get("messages") or 0):
        return "empty"
    try:
        last = float(row.get("last_at") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if row.get("ended_at") is None and last >= now - ACTIVE_WINDOW_SECONDS:
        return "current"
    return ""


def _wanted(row: dict[str, Any], *, exclude: set[str], now: float,
            picked: set[str] | None) -> str:
    """Like classify, but a session the user picked by id is always taken
    unless it is the live one."""
    reason = classify(row, exclude=exclude, now=now)
    if picked is None:
        return reason
    if row.get("id") not in picked:
        return "not_picked"
    return reason if reason in {"current", "invalid", "empty"} else ""


def preview(
    home: Path, *, exclude: set[str] | None = None, limit: int = 0,
    picked: set[str] | None = None, now: float | None = None,
) -> dict[str, Any]:
    """Counts and date range of what "Import all" would add.

    ``recent`` lists up to ``limit`` eligible sessions, newest first, for
    "Let me pick". Raises :class:`HistoryError` when the database exists
    but cannot be read; a missing database previews as empty.
    """
    exclude = set(exclude or ())
    now = time.time() if now is None else now
    result: dict[str, Any] = {
        "sessions": 0, "messages": 0, "first_at": "", "last_at": "",
        "excluded": {"current": 0, "automated": 0, "group": 0, "hidden": 0},
        "recent": [],
    }
    db = StateDB(state_db_path(home))
    if not db.exists():
        return result
    first = last = None
    heap: list[tuple[float, str, dict[str, Any]]] = []
    for row in db.iter_sessions():
        reason = _wanted(row, exclude=exclude, now=now, picked=picked)
        if reason:
            if reason in result["excluded"]:
                result["excluded"][reason] += 1
            continue
        result["sessions"] += 1
        result["messages"] += int(row.get("messages") or 0)
        start = row.get("first_at") or row.get("started_at")
        end = row.get("last_at") or row.get("started_at")
        if start is not None and (first is None or float(start) < first):
            first = float(start)
        if end is not None and (last is None or float(end) > last):
            last = float(end)
        if limit > 0:
            entry = {
                "session_id": row["id"],
                "platform": str(row.get("source") or ""),
                "title": str(row.get("title") or "")[:120],
                "started": _day(row.get("started_at")),
                "messages": int(row.get("messages") or 0),
            }
            item = (float(end or 0), row["id"], entry)
            if len(heap) < limit:
                heapq.heappush(heap, item)
            else:
                heapq.heappushpop(heap, item)
    result["first_at"] = _iso(first) if first is not None else ""
    result["last_at"] = _iso(last) if last is not None else ""
    result["first_day"] = _day(first) if first is not None else ""
    result["last_day"] = _day(last) if last is not None else ""
    result["recent"] = [entry for _, _, entry in sorted(heap, reverse=True)]
    return result


def offer_question(view: dict[str, Any]) -> str:
    """The question the agent asks, in the user's terms."""
    return (
        f"I can also add your {describe_preview(view)} to your Substrate memory, so I "
        "remember them too. Import all, let me pick, or not now?"
    )


def describe_preview(view: dict[str, Any]) -> str:
    """One plain sentence: "42 earlier conversations (1,234 messages) from ... to ..."."""
    sessions = int(view.get("sessions") or 0)
    noun = "conversation" if sessions == 1 else "conversations"
    text = f"{sessions:,} earlier Hermes {noun} ({int(view.get('messages') or 0):,} messages"
    first, last = view.get("first_day") or "", view.get("last_day") or ""
    if first and last and first != last:
        text += f", {first} to {last}"
    elif first:
        text += f", {first}"
    return text + ")"


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------


def _message_item(row: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"role": row.get("role"), "content": row.get("content") or ""}
    for key in ("tool_call_id", "tool_name"):
        if row.get(key):
            item[key] = row[key]
    timestamp = _iso(row.get("timestamp")) if row.get("timestamp") is not None else ""
    if timestamp:
        item["timestamp"] = timestamp
    raw_calls = row.get("tool_calls")
    if isinstance(raw_calls, str) and raw_calls.strip():
        try:
            calls = json.loads(raw_calls)
        except ValueError:
            calls = None
        if isinstance(calls, list):
            item["tool_calls"] = calls
    return item


def _validated(envelope: dict[str, Any]) -> dict[str, Any]:
    contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
    return envelope


def _turn_envelope(session_id: str, messages: list[dict[str, Any]],
                   batch_id: str) -> dict[str, Any]:
    offset = {"start": messages[0]["index"], "end": messages[-1]["index"] + 1}
    payload = {"turn_id": f"replay-{offset['start']}-{offset['end']}", "messages": messages}
    return {
        "schema_version": contract.SCHEMA_VERSION,
        "contract_version": contract.CONTRACT_VERSION,
        "event_id": contract.deterministic_event_id("capture_turn", session_id, offset, payload),
        "kind": "capture_turn",
        "session_id": session_id,
        "offset": offset,
        "capture_origin": "history_replay",
        "batch_id": batch_id,
        "speaker": dict(SPEAKER),
        "created_at": messages[0].get("timestamp") or _NO_TIME,
        "payload": payload,
    }


def _turn_envelopes(session_id: str, messages: list[dict[str, Any]],
                    batch_id: str) -> list[dict[str, Any]]:
    """One envelope per turn; an oversized turn splits into consecutive parts."""
    whole = _turn_envelope(session_id, messages, batch_id)
    if len(messages) <= 4096 and len(contract.canonical_bytes(whole)) <= MAX_IMPORT_EVENT_BYTES:
        return [_validated(whole)]
    parts: list[dict[str, Any]] = []
    chunk: list[dict[str, Any]] = []
    for message in messages:
        candidate = [*chunk, message]
        if chunk and (
            len(candidate) > 4096
            or len(contract.canonical_bytes(_turn_envelope(session_id, candidate, batch_id)))
            > MAX_IMPORT_EVENT_BYTES
        ):
            parts.append(_validated(_turn_envelope(session_id, chunk, batch_id)))
            chunk = [message]
        else:
            chunk = candidate
    if chunk:
        parts.append(_validated(_turn_envelope(session_id, chunk, batch_id)))
    return parts


def session_end_envelope(row: dict[str, Any], high_water: int, batch_id: str) -> dict[str, Any]:
    sid = row["id"]
    payload = {
        "boundary": "end",
        "session_complete": True,
        "message_high_water": high_water,
        "platform": str(row.get("source") or "hermes")[: contract.MAX_PLATFORM_BYTES] or "hermes",
        "chat_type": str(row.get("chat_type") or "direct")[: contract.MAX_CHAT_TYPE_BYTES] or "direct",
    }
    offset = {"start": high_water, "end": high_water}
    return _validated({
        "schema_version": contract.SCHEMA_VERSION,
        "contract_version": contract.CONTRACT_VERSION,
        "event_id": contract.deterministic_event_id("capture_session", sid, offset, payload),
        "kind": "capture_session",
        "session_id": sid,
        "offset": offset,
        "capture_origin": "history_replay",
        "batch_id": batch_id,
        "speaker": dict(SPEAKER),
        "created_at": _iso(row.get("last_at")) or _iso(row.get("started_at")) or _NO_TIME,
        "payload": payload,
    })


def iter_session_envelopes(
    db: StateDB, row: dict[str, Any], *, batch_id: str, start_index: int = 0,
    server_high_water: int = 0, stats: dict[str, int] | None = None,
) -> Iterator[tuple[dict[str, Any], int]]:
    """Yield ``(envelope, resume_index)`` for one session's turns, then its end.

    A turn starts at each user message. Turns that end at or below
    ``server_high_water`` (already held by Substrate, e.g. captured live)
    or start before ``start_index`` (already queued before a restart) are
    skipped. ``resume_index`` is where a resumed run continues.
    """
    from .plugin import _capture_message  # same redaction and bounds as live capture

    sid = row["id"]
    turn: list[dict[str, Any]] = []
    index = 0

    def flush() -> Iterator[tuple[dict[str, Any], int]]:
        if not turn:
            return
        start, end = turn[0]["index"], turn[-1]["index"] + 1
        if start >= start_index and end <= server_high_water and stats is not None:
            stats["held"] = stats.get("held", 0) + 1
        if start >= start_index and end > server_high_water:
            for envelope in _turn_envelopes(sid, list(turn), batch_id):
                yield envelope, end

    for raw in db.iter_messages(sid):
        message = _capture_message(_message_item(raw), index)
        if message is not None and raw.get("_cut") and message["role"] == "tool":
            message["result_truncated"] = True
        index += 1
        if message is None:
            continue
        if message["role"] == "user" and turn:
            yield from flush()
            turn = []
        turn.append(message)
    yield from flush()
    if index:
        yield session_end_envelope(row, index, batch_id), index


# ---------------------------------------------------------------------------
# Import job: checkpointed, locked, spool-delivered
# ---------------------------------------------------------------------------


def _job_path(home: Path) -> Path:
    return substrate_dir(home) / JOB_FILE


def read_job(home: Path) -> dict[str, Any]:
    return _read_json(_job_path(home)) or {}


def _save_job(home: Path, job: dict[str, Any]) -> None:
    job["updated_at"] = _now_iso()
    _write_json(_job_path(home), job)


@contextlib.contextmanager
def job_lock(home: Path) -> Iterator[bool]:
    """Exclusive per-profile import lock (cross-process). Yields False if held."""
    try:
        import fcntl
    except ImportError:  # non-POSIX: best effort, single process
        yield True
        return
    path = substrate_dir(home) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def job_running(home: Path) -> bool:
    with job_lock(home) as free:
        return not free


def _replay_counters(spool: Any) -> dict[str, int]:
    from .spool import PRIORITY_REPLAY

    totals = {"delivered": 0, "duplicate": 0, "quarantined": 0, "evicted": 0}
    try:
        counters = spool.counters()
    except Exception:  # noqa: BLE001
        return totals
    for key, value in counters.items():
        try:
            kind, origin, priority, outcome = key.split("|")
        except ValueError:
            continue
        if kind != "capture_turn" or origin != "history_replay" or int(priority) != PRIORITY_REPLAY:
            continue
        if outcome in totals:
            totals[outcome] += int(value.get("item_count") or 0)
    return totals


def _server_high_water(client: Any, session_id: str) -> int:
    """Turns of this session Substrate already holds (0 when unknown)."""
    try:
        structured, _text = client.call_tool(
            "memory_import_status", {"session_id": session_id}, timeout=10.0
        )
        session = structured.get("session") if isinstance(structured, dict) else None
        value = session.get("message_high_water") if isinstance(session, dict) else 0
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
    except Exception:  # noqa: BLE001 - unknown means "send everything"; ids dedupe
        return 0


class ImportJob:
    """Import selected sessions into Substrate through the profile spool.

    ``run`` returns the final status. A crash leaves ``import.json`` in
    state ``running`` with a cursor; the next ``run`` (same selection)
    resumes there. Everything resent around the cursor has the same event
    ids, so the server answers ``duplicate``.
    """

    def __init__(
        self, home: Path, *, spool: Any, client: Any, exclude: set[str] | None = None,
        picked: list[str] | None = None, stop: threading.Event | None = None,
        poll_seconds: float = 0.25, drain_timeout: float | None = None,
        stall_seconds: float | None = None,
    ) -> None:
        self.home = Path(home)
        self.spool = spool
        self.client = client
        self.exclude = set(exclude or ())
        self.picked = list(dict.fromkeys(picked))[:MAX_PICKED] if picked else None
        self.stop = stop or threading.Event()
        self.poll_seconds = poll_seconds
        self.drain_timeout = drain_timeout
        self.stall_seconds = STALL_SECONDS if stall_seconds is None else stall_seconds
        self._last_pending: tuple[int, int] | None = None
        self._last_change = time.monotonic()
        self.stalled = False

    def _stalled(self, pending: tuple[int, int]) -> bool:
        """True (and stop) when the replay backlog has not moved for too long."""
        if pending != self._last_pending:
            self._last_pending, self._last_change = pending, time.monotonic()
            return False
        if time.monotonic() - self._last_change > self.stall_seconds:
            self.stalled = True
            self.stop.set()
            return True
        return False

    # -- state -------------------------------------------------------------

    def _fresh(self, view: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": 1, "state": "running", "batch_id": profile_batch_id(self.home),
            "picked": self.picked, "exclude": sorted(self.exclude)[:64],
            "cursor": None, "next_index": 0,
            "sessions_total": view["sessions"], "messages_total": view["messages"],
            "sessions_done": 0, "turns_queued": 0,
            "base": _replay_counters(self.spool),
            "started_at": _now_iso(), "announced": None,
            # Sessions that start later are live ones: never part of this import.
            "until": time.time(),
        }

    def _load_or_start(self) -> dict[str, Any]:
        job = read_job(self.home)
        if job.get("state") == "running" and job.get("picked") == self.picked and job.get("batch_id"):
            self.exclude |= set(job.get("exclude") or ())
            return job
        view = preview(self.home, exclude=self.exclude,
                       picked=set(self.picked) if self.picked is not None else None)
        return self._fresh(view)

    # -- delivery ------------------------------------------------------------

    def _wait_for_room(self, job: dict[str, Any]) -> None:
        from .spool import PRIORITY_REPLAY

        last_save = 0.0
        while not self.stop.is_set():
            items, size = self.spool.pending_at(PRIORITY_REPLAY)
            if items < WINDOW_ITEMS and size < WINDOW_BYTES:
                return
            if self._stalled((items, size)):
                return
            if time.monotonic() - last_save > 2.0:
                self._progress(job)
                last_save = time.monotonic()
            self.stop.wait(self.poll_seconds)

    def _progress(self, job: dict[str, Any]) -> None:
        now = _replay_counters(self.spool)
        base = job.get("base") or {}
        job["turns_sent"] = max(0, now["delivered"] + now["quarantined"]
                                - int(base.get("delivered", 0)) - int(base.get("quarantined", 0)))
        with contextlib.suppress(HistoryError, OSError):
            _save_job(self.home, job)

    def _drain(self, job: dict[str, Any]) -> bool:
        from .spool import PRIORITY_REPLAY

        deadline = None if self.drain_timeout is None else time.monotonic() + self.drain_timeout
        last_save = 0.0
        while not self.stop.is_set():
            pending = self.spool.pending_at(PRIORITY_REPLAY)
            if pending[0] == 0:
                return True
            if self._stalled(pending):
                return False
            if deadline is not None and time.monotonic() > deadline:
                return False
            if time.monotonic() - last_save > 2.0:
                self._progress(job)
                last_save = time.monotonic()
            self.stop.wait(self.poll_seconds)
        return False

    def run(self) -> dict[str, Any]:
        """Run (or resume) the import; returns the status after the lock is released."""
        with job_lock(self.home) as acquired:
            if acquired:
                self._run_locked()
        return status(self.home)

    def _run_locked(self) -> None:
        from .spool import PRIORITY_REPLAY

        job = self._load_or_start()
        _save_job(self.home, job)
        if self.client is not None and getattr(self.client, "api_key", ""):
            self.spool.start(self.client)
        db = StateDB(state_db_path(self.home))
        picked = set(self.picked) if self.picked is not None else None
        now = time.time()
        cursor = job.get("cursor")
        after = (float(cursor[0]), str(cursor[1])) if cursor else None
        since_checkpoint = 0
        try:
            sessions = db.iter_sessions(after) if db.exists() else iter(())
            for row in sessions:
                if self.stop.is_set():
                    break
                if row["started_at"] > float(job.get("until") or now):
                    break  # oldest first: everything from here on is newer
                if _wanted(row, exclude=self.exclude, now=now, picked=picked):
                    job["cursor"], job["next_index"] = [row["started_at"], row["id"]], 0
                    continue
                high_water = _server_high_water(self.client, row["id"]) if self.client else 0
                held = {"held": 0}
                for envelope, resume_index in iter_session_envelopes(
                    db, row, batch_id=job["batch_id"],
                    start_index=int(job.get("next_index") or 0),
                    server_high_water=high_water, stats=held,
                ):
                    self._wait_for_room(job)
                    if self.stop.is_set():
                        break
                    self.spool.enqueue(envelope, priority=PRIORITY_REPLAY,
                                       kind=envelope["kind"], capture_origin="history_replay")
                    if envelope["kind"] == "capture_turn":
                        job["turns_queued"] = int(job.get("turns_queued") or 0) + 1
                        job["next_index"] = resume_index
                    since_checkpoint += 1
                    if since_checkpoint >= CHECKPOINT_EVERY:
                        self._progress(job)
                        since_checkpoint = 0
                job["turns_held"] = int(job.get("turns_held") or 0) + held["held"]
                if self.stop.is_set():
                    break
                job["cursor"], job["next_index"] = [row["started_at"], row["id"]], 0
                job["sessions_done"] = int(job.get("sessions_done") or 0) + 1
                self._progress(job)
                since_checkpoint = 0
        except HistoryError as exc:
            job.update(state="failed", error=exc.category)
            _save_job(self.home, job)
            return
        if self.stop.is_set():
            self._progress(job)  # stays "running": the next start resumes here
            return
        if not self._drain(job):
            self._progress(job)
            return
        now_counts = _replay_counters(self.spool)
        base = job.get("base") or {}
        delta = {key: max(0, now_counts[key] - int(base.get(key, 0))) for key in now_counts}
        held_total = int(job.get("turns_held") or 0)  # already in Substrate: not resent
        job.update(
            state="done", finished_at=_now_iso(), announced=False,
            result={
                "sessions": int(job.get("sessions_done") or 0),
                "turns": delta["delivered"] + delta["quarantined"] + held_total,
                "stored": max(0, delta["delivered"] - delta["duplicate"]),
                "duplicate": delta["duplicate"] + held_total,
                "rejected": delta["quarantined"],
                "dropped": delta["evicted"],
            },
        )
        self._progress(job)
        return


def summary(result: dict[str, Any]) -> str:
    sessions = int(result.get("sessions") or 0)
    noun = "conversation" if sessions == 1 else "conversations"
    text = (f"Imported {sessions:,} past Hermes {noun} ({int(result.get('turns') or 0):,} turns): "
            f"{int(result.get('stored') or 0):,} new, "
            f"{int(result.get('duplicate') or 0):,} already in memory.")
    problems = int(result.get("rejected") or 0) + int(result.get("dropped") or 0)
    if problems:
        text += (f" {problems:,} turns could not be saved; ask me to import your past "
                 "conversations again to retry them.")
    return text


def status(home: Path) -> dict[str, Any]:
    """Content-free progress for the tool, the CLI and the next turn."""
    job = read_job(home)
    state = job.get("state") or "none"
    running = state == "running" and job_running(home)
    if state == "running" and not running:
        state = "interrupted"
    elif running:
        state = "running"
    out: dict[str, Any] = {
        "state": state,
        "decision": read_decision(home) or None,
        "sessions_total": int(job.get("sessions_total") or 0),
        "sessions_done": int(job.get("sessions_done") or 0),
        "turns_queued": int(job.get("turns_queued") or 0),
        "turns_sent": int(job.get("turns_sent") or 0),
    }
    if state == "done":
        out["result"] = job.get("result") or {}
        out["message"] = summary(out["result"])
    elif state == "running":
        out["message"] = (
            f"Importing past conversations: {out['sessions_done']:,} of "
            f"{out['sessions_total']:,} done, {out['turns_sent']:,} turns saved so far. "
            "It runs in the background; live messages are saved first."
        )
    elif state == "interrupted":
        out["message"] = (
            f"The import stopped after {out['sessions_done']:,} of {out['sessions_total']:,} "
            "conversations (Hermes restarted). It continues where it left off on the next "
            "message, or start it again."
        )
    elif state == "failed":
        out["error"] = str(job.get("error") or "")
        out["message"] = ("Could not read this profile's Hermes history (state.db). "
                          "Nothing was changed; try again later.")
    else:
        out["message"] = "No import has run for this profile yet."
    return out


def take_finished_announcement(home: Path) -> str:
    """The one-time "import finished" line for the next turn ("" otherwise)."""
    job = read_job(home)
    if job.get("state") != "done" or job.get("announced") is not False:
        return ""
    job["announced"] = True
    try:
        _save_job(home, job)
    except (HistoryError, OSError):
        return ""
    return summary(job.get("result") or {})


# ---------------------------------------------------------------------------
# Background runner (in-process, one per profile)
# ---------------------------------------------------------------------------

_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()


def start_background(
    home: Path, *, client: Any, exclude: set[str] | None = None,
    picked: list[str] | None = None, spool_factory: Callable[[], Any] | None = None,
) -> bool:
    """Run an import in a daemon thread. False when one is already running."""
    key = str(Path(home).resolve())
    with _threads_lock:
        thread = _threads.get(key)
        if (thread is not None and thread.is_alive()) or job_running(home):
            return False
        if spool_factory is None:
            from .spool import get_spool as spool_factory  # type: ignore[no-redef]

        def target() -> None:
            with contextlib.suppress(Exception):
                ImportJob(home, spool=spool_factory(), client=client, exclude=exclude,
                          picked=picked).run()

        thread = threading.Thread(target=target, name="substrate-history-import", daemon=True)
        _threads[key] = thread
        thread.start()
        return True


def resume_if_interrupted(home: Path, *, client: Any, exclude: set[str] | None = None) -> bool:
    """Continue an import that a restart cut off (only after a yes)."""
    job = read_job(home)
    if job.get("state") != "running" or read_decision(home) not in {"yes", "picked"}:
        return False
    if job_running(home):
        return False
    picked = job.get("picked") if isinstance(job.get("picked"), list) else None
    return start_background(home, client=client, exclude=exclude, picked=picked)


__all__ = [
    "ImportJob", "HistoryError", "StateDB", "classify", "describe_preview", "iter_session_envelopes",
    "job_running", "offer_question", "preview", "resume_if_interrupted", "read_decision", "read_job", "record_decision", "start_background",
    "state_db_path", "status", "summary", "take_finished_announcement",
]
