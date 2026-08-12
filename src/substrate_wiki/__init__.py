"""Substrate Wiki external memory provider for Hermes Agent v0.20.0."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import queue
import random
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

try:
    from agent.memory_provider import MemoryProvider
except ModuleNotFoundError as exc:  # Allows contract tests without Hermes installed.
    if exc.name not in {"agent", "agent.memory_provider"}:
        raise

    class MemoryProvider:  # type: ignore[no-redef]
        pass


from .client import SubstrateAPIError, SubstrateClient
from .events import MAX_CAPTURE_BYTES, CaptureEventBuilder
from .redaction import configured_secret_values, redact
from .spool import DurableSpool, secure_atomic_json_write

__all__ = ["SubstrateWikiProvider", "register"]

_PROVIDER_ID = "substrate_wiki"
_MAX_TOOL_RESULT_BYTES = 64 * 1024
_MAX_CAPTURE_BYTES = MAX_CAPTURE_BYTES
_MAX_SESSIONS = 32
_MAX_MESSAGE_HASHES = 512
_MAX_PREFETCH_BYTES = 16 * 1024
_MAX_PREFETCH_ENTRIES = 128
_MIN_SPOOL_BYTES = 16 * 1024
_RETRY_BASE_SECONDS = 1.0
_AUTH_RETRY_BASE_SECONDS = 30.0
_MAX_RETRY_SECONDS = 300.0
_MAX_RETRY_EXPONENT = 8
_RETRY_JITTER_FRACTION = 0.2
_ENTITY_TYPES = frozenset(
    {
        "person",
        "agent",
        "organization",
        "project",
        "product",
        "place",
        "event",
        "other",
        "system",
        "service",
        "automation",
    }
)
_ENTITY_FILENAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,79})--[0-9a-f]{8}\.md")
_SCOPE_KWARGS = {
    "agent_identity": "agent_identity",
    "agent_workspace": "agent_workspace",
    "user_id": "user_id",
    "platform": "platform",
    "agent_id": "agent_id",
    "agent_name": "agent_name",
    "workspace": "workspace",
    "profile": "profile",
    "user": "user",
    "chat_type": "chat_type",
}
_CONTEXT_SCOPE_FIELDS = (
    "agent_id",
    "agent_name",
    "workspace",
    "profile",
    "user",
    "platform",
    "chat_type",
)
_NON_PRIMARY_ROLES = {"cron", "subagent", "child", "secondary", "worker", "flush", "gateway"}

# Hermes v0.20.0 consumes direct function schemas, not OpenAI's {type,function} envelope.
_TOOL_SCHEMAS = [
    {
        "name": "wiki_search",
        "description": "Search maintained Substrate wiki pages and return matching cited material.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms.", "maxLength": 4096},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 8},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wiki_read",
        "description": "Read one maintained wiki page by its repository-relative path or legacy slug.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Wiki page path or slug, for example notes/hermes.md or notes/hermes.",
                    "maxLength": 4096,
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wiki_query",
        "description": "Ask a question over the wiki and receive a synthesized, cited answer.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "maxLength": 16384},
                "save_as_synthesis": {"type": "boolean", "default": False},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wiki_ingest",
        "description": "Submit text or a public URL for asynchronous ingestion into the wiki.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Text or public URL to ingest.",
                    "maxLength": 262144,
                },
                "title": {"type": "string", "maxLength": 512},
                "source_type": {"type": "string", "enum": ["text", "url"], "default": "text"},
            },
            "required": ["content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wiki_job_status",
        "description": "Check the current state of a Substrate asynchronous job.",
        "parameters": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "maxLength": 512}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
]


def _bounded_json(value: Any, *, limit: int = _MAX_TOOL_RESULT_BYTES) -> str:
    """Serialize to a valid bounded JSON string without leaking arbitrary reprs."""
    try:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        rendered = json.dumps({"error": "invalid_response"}, separators=(",", ":"))
    encoded = rendered.encode("utf-8")
    if len(encoded) <= limit:
        return rendered
    digest = hashlib.sha256(encoded).hexdigest()
    return json.dumps(
        {"error": "response_too_large", "sha256": digest, "original_bytes": len(encoded)},
        separators=(",", ":"),
        sort_keys=True,
    )


def _context_is_primary(context: Any, *, default: bool = True) -> bool:
    """Interpret Hermes string, mapping, and object runtime contexts consistently."""
    if context is None:
        return default
    if isinstance(context, str):
        return context.strip().lower() not in _NON_PRIMARY_ROLES
    if _context_value(context, "is_primary", True) is False:
        return False
    if _context_value(context, "primary", True) is False:
        return False
    role = (
        _context_value(context, "role")
        or _context_value(context, "runtime_role")
        or _context_value(context, "source")
    )
    return not (isinstance(role, str) and role.lower() in _NON_PRIMARY_ROLES)


def _is_primary_runtime(kwargs: dict[str, Any]) -> bool:
    """Fail closed only when Hermes explicitly marks this as a non-primary runtime."""
    context = kwargs.get("runtime_context") or kwargs.get("context")
    return _context_is_primary(context, default=kwargs.get("is_primary", True) is not False)


def _context_value(context: Any, key: str, default: Any = None) -> Any:
    if isinstance(context, dict):
        return context.get(key, default)
    return getattr(context, key, default)


def _bounded_scope_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)[:512]
    return ""


class SubstrateWikiProvider(MemoryProvider):
    """Hermes provider backed by the authenticated Substrate HTTP API."""

    @property
    def name(self) -> str:
        return _PROVIDER_ID

    def __init__(self) -> None:
        self._session_id = ""
        self._home: Path | None = None
        self._client: SubstrateClient | None = None
        self._event_builder: CaptureEventBuilder | None = None
        self._spool: DurableSpool | None = None
        self._events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
        self._prefetch_jobs: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=32)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._prefetch_worker: threading.Thread | None = None
        self._onboarding_worker: threading.Thread | None = None
        self._prefetch_cache: dict[str, tuple[float, str]] = {}
        self._latest_prefetch_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._capture_lock = threading.RLock()
        self._delivery_lock = threading.Lock()
        self._current_event: dict[str, Any] | None = None
        self._current_persisted: Path | None = None
        self._delivery_failure_streak = 0
        self._retry_random = random.Random()
        self._capture_state: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._secrets: tuple[str, ...] = ()
        self._scope: dict[str, str] = {"provider_id": _PROVIDER_ID}
        self._initialized_primary = True
        self._counters = {
            "captured": 0,
            "suppressed": 0,
            "delivered": 0,
            "delivery_failed": 0,
            "spooled": 0,
            "spool_evicted": 0,
            "quarantined": 0,
            "permanent_dropped": 0,
            "dropped": 0,
            "prefetch_queued": 0,
            "prefetch_cached": 0,
            "prefetch_failed": 0,
        }
        self._last_delivery_category = "none"
        self._last_prefetch_category = "none"
        self._settings: dict[str, Any] = {
            "api_url": "https://app.trysubstrate.co",
            "spool_max_items": 1000,
            "spool_max_bytes": 10 * 1024 * 1024,
            "prefetch_ttl_seconds": 60,
        }

    def is_available(self) -> bool:
        """The hosted provider activates before sign-in; still reject unsafe overrides."""
        override = os.environ.get("HERMES_API_URL", "")
        return not override or override.rstrip("/") == "https://app.trysubstrate.co"

    def post_setup(self, hermes_home: str, config: dict[str, Any]) -> None:
        """Hermes v0.20 setup hook: activate, sign in, then ask history consent."""
        from .onboarding import main as onboarding_main

        memory = config.setdefault("memory", {})
        if not isinstance(memory, dict):
            memory = {}
            config["memory"] = memory
        memory["provider"] = _PROVIDER_ID
        try:
            from hermes_cli.config import save_config

            save_config(config)
        except ImportError:
            pass
        mode = "auto" if sys.stdin.isatty() else "device"
        onboarding_main([
            "--hermes-home", hermes_home, "--mode", mode, "--wait", "--history", "ask"
        ])

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        hermes_home = kwargs.get("hermes_home")
        if not hermes_home:
            raise ValueError("hermes_home is required")
        self._session_id = str(session_id or "")
        self._home = Path(hermes_home) / _PROVIDER_ID
        if self._home.exists() and self._home.is_symlink():
            raise ValueError("plugin state directory must not be a symlink")
        self._home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(self._home, 0o700)
        agent_context = kwargs.get("agent_context")
        self._initialized_primary = _context_is_primary(agent_context)
        self._scope = {"provider_id": _PROVIDER_ID}
        for source, target in _SCOPE_KWARGS.items():
            value = _bounded_scope_value(kwargs.get(source))
            if value:
                self._scope[target] = value
        for field in _CONTEXT_SCOPE_FIELDS:
            if field in self._scope:
                continue
            value = _bounded_scope_value(_context_value(agent_context, field))
            if value:
                self._scope[field] = value
        self._load_settings()
        # Hosted onboarding is pinned; no local discovery/start or arbitrary origin.
        self._settings["api_url"] = "https://app.trysubstrate.co"
        self._client = SubstrateClient.from_env(
            fallback_url="https://app.trysubstrate.co",
            hermes_home=Path(hermes_home),
            hosted_default=True,
        )
        self._settings["spool_max_bytes"] = max(
            _MIN_SPOOL_BYTES, int(self._settings["spool_max_bytes"])
        )
        self._spool = DurableSpool(
            self._home / "spool",
            max_items=int(self._settings["spool_max_items"]),
            max_bytes=int(self._settings["spool_max_bytes"]),
        )
        client_key = str(getattr(self._client, "api_key", "") or "")
        self._secrets = (
            tuple(dict.fromkeys((*configured_secret_values(), client_key)))
            if client_key
            else configured_secret_values()
        )
        self._event_builder = CaptureEventBuilder(self._scope, secrets=self._secrets)
        self._stop.clear()
        self._wake.clear()
        self._worker = threading.Thread(
            target=self._sender_loop, name="substrate_wiki_sender", daemon=True
        )
        self._prefetch_worker = threading.Thread(
            target=self._prefetch_loop, name="substrate_wiki_prefetch", daemon=True
        )
        self._worker.start()
        self._prefetch_worker.start()
        if self._initialized_primary:
            self._onboarding_worker = threading.Thread(
                target=(self._repair_onboarding if client_key else self._begin_onboarding),
                name=(
                    "substrate_wiki_onboarding_repair"
                    if client_key
                    else "substrate_wiki_onboarding"
                ),
                daemon=True,
            )
            self._onboarding_worker.start()

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return list(_TOOL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if not isinstance(args, dict):
            return _bounded_json({"error": "invalid_arguments"})
        client = self._require_client()
        try:
            if tool_name == "wiki_search":
                result = client.search(
                    self._input(args, "query", 4096),
                    limit=max(1, min(int(args.get("limit", 8)), 25)),
                )
            elif tool_name == "wiki_read":
                result = client.read_page(self._input(args, "path", 4096))
            elif tool_name == "wiki_query":
                result = client.query_wiki(
                    self._input(args, "question", 16384),
                    save_as_synthesis=self._boolean(args, "save_as_synthesis", False),
                )
            elif tool_name == "wiki_ingest":
                result = client.ingest(
                    self._input(args, "content", 262144),
                    title=self._optional_input(args, "title", 512),
                    source_type=self._source_type(args.get("source_type", "text")),
                )
            elif tool_name == "wiki_job_status":
                result = client.job_status(self._input(args, "job_id", 512))
            else:
                result = {"error": "unknown_tool"}
        except (KeyError, TypeError, ValueError):
            result = {"error": "invalid_arguments"}
        except SubstrateAPIError as exc:
            result = {"error": exc.category}
        return _bounded_json(result)

    def get_config_schema(self) -> list[dict[str, Any]]:
        """Hosted sign-in owns credentials; Hermes must not prompt for API keys."""
        return []

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        """Persist only the immutable hosted origin and non-secret tuning."""
        root = Path(hermes_home) / _PROVIDER_ID
        if root.exists() and root.is_symlink():
            raise ValueError("plugin state directory must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        secure_atomic_json_write(root / "config.json", {
            "api_url": "https://app.trysubstrate.co",
            "hosted": True,
            "spool_max_items": max(1, int(values.get("spool_max_items", self._settings["spool_max_items"]))),
            "spool_max_bytes": max(_MIN_SPOOL_BYTES, int(values.get("spool_max_bytes", self._settings["spool_max_bytes"]))),
            "prefetch_ttl_seconds": max(1, int(values.get("prefetch_ttl_seconds", self._settings["prefetch_ttl_seconds"]))),
        })
        self._settings["api_url"] = "https://app.trysubstrate.co"

    def _repair_onboarding(self) -> None:
        try:
            from .onboarding import OnboardingManager

            assert self._home is not None
            OnboardingManager(self._home.parent).repair(wait=False)
        except Exception:  # noqa: BLE001 - status remains content-free and resumable
            return

    def _request_reconnect(self) -> None:
        if self._home is None or not self._initialized_primary:
            return
        worker = self._onboarding_worker
        if worker is not None and worker.is_alive():
            return
        try:
            from .credentials import credential_store

            credential_store(self._home.parent).delete()
        except (OSError, ValueError):
            return
        self._onboarding_worker = threading.Thread(
            target=self._begin_onboarding,
            name="substrate_wiki_reconnect",
            daemon=True,
        )
        self._onboarding_worker.start()

    def _begin_onboarding(self) -> None:
        """Complete hosted sign-in in the background, then wake durable delivery."""
        try:
            from .onboarding import OnboardingManager

            assert self._home is not None
            result = OnboardingManager(self._home.parent).run(mode="auto", wait=True)
            if not result.get("authenticated"):
                return
            client = SubstrateClient.from_env(
                fallback_url="https://app.trysubstrate.co",
                hermes_home=self._home.parent,
                hosted_default=True,
            )
            if not client.api_key:
                return
            with self._capture_lock:
                self._client = client
                self._secrets = tuple(
                    dict.fromkeys((*configured_secret_values(), client.api_key))
                )
                self._event_builder = CaptureEventBuilder(self._scope, secrets=self._secrets)
            self._wake.set()
        except Exception:  # noqa: BLE001 - surfaced by content-free onboarding status
            return

    def system_prompt_block(self) -> str:
        return (
            "Substrate Wiki is the single published memory. Automatic recall contains only canonical published entity "
            "wiki pages, never raw conversation transcripts, private claims, or extraction state. Treat temporal "
            "qualifiers and citations as evidence metadata, surface contradictions, and use wiki_ingest only when the "
            "user asks to add source material."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return already-cached cited context without performing network I/O."""
        if not isinstance(query, str) or not query.strip():
            return ""
        sid = session_id or self._session_id
        cache_key = self._cache_key(query[:4096], sid)
        session_key = self._session_cache_key(sid)
        with self._cache_lock:
            self._evict_prefetch_locked()
            cached = self._prefetch_cache.get(cache_key)
            if cached:
                return cached[1]
            latest = self._latest_prefetch_cache.get(session_key)
            return latest[1] if latest else ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue cache warming on one bounded worker; never create per-turn threads."""
        if not isinstance(query, str) or not query.strip() or self._stop.is_set():
            return
        job = (query[:4096], session_id or self._session_id)
        try:
            self._prefetch_jobs.put_nowait(job)
            self._counters["prefetch_queued"] += 1
        except queue.Full:
            return

    def sync_turn(
        self,
        user: Any,
        assistant: Any,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        if not self._capture_allowed(kwargs):
            return
        sid = str(session_id or self._session_id)
        payload = {"user": user, "assistant": assistant}
        self._capture("turn", sid, payload, messages=messages)

    def on_pre_compress(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        if self._capture_allowed(kwargs):
            self._capture("pre_compress", self._session_id, {}, messages=messages)
        return ""

    def on_session_end(self, messages: list[dict[str, Any]], **kwargs: Any) -> None:
        if self._capture_allowed(kwargs):
            self._capture_snapshot("session_end", self._session_id, messages)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: Any,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if not self._capture_allowed(kwargs):
            return
        safe_metadata = metadata if isinstance(metadata, dict) else {}
        metadata_session = safe_metadata.get("session_id")
        sid = (
            str(metadata_session)[:512]
            if isinstance(metadata_session, (str, int))
            else self._session_id
        )
        selected_metadata: dict[str, Any] = {}
        for key in ("source", "provenance"):
            value = safe_metadata.get(key)
            if isinstance(value, (str, int, float, bool)):
                selected_metadata[key] = value
        self._capture(
            "memory_write",
            sid,
            {"action": action, "target": target, "content": content, "metadata": selected_metadata},
        )

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        """Emit the old session boundary before rebinding to prevent attribution races."""
        if not self._capture_allowed(kwargs):
            return
        new_session = str(new_session_id or "")
        with self._capture_lock:
            old_session = self._session_id
            if old_session and (old_session != new_session or reset or rewound):
                self._capture(
                    "session_boundary",
                    old_session,
                    {
                        "reason": "session_switch",
                        "next_session_id": new_session,
                        "parent_session_id": str(parent_session_id or "")[:512],
                        "reset": bool(reset),
                        "rewound": bool(rewound),
                    },
                )
            self._session_id = new_session

    def shutdown(self) -> None:
        """Stop workers within five seconds; each sender event is write-ahead spooled."""
        deadline = time.monotonic() + 5.0
        self._stop.set()
        self._wake.set()
        if self._prefetch_worker and self._prefetch_worker.is_alive():
            self._prefetch_worker.join(timeout=max(0.0, deadline - time.monotonic()))
        while True:
            try:
                self._prefetch_jobs.get_nowait()
            except queue.Empty:
                break
            else:
                self._prefetch_jobs.task_done()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=max(0.0, deadline - time.monotonic()))

    def _load_settings(self) -> None:
        assert self._home is not None
        path = self._home / "config.json"
        if path.is_symlink():
            return
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            return
        if not isinstance(values, dict):
            return
        api_url = values.get("api_url")
        if isinstance(api_url, str) and (
            not api_url or SubstrateClient.is_allowed_base_url(api_url)
        ):
            self._settings["api_url"] = api_url.rstrip("/")
        for key in ("spool_max_items", "spool_max_bytes", "prefetch_ttl_seconds"):
            try:
                if key in values:
                    minimum = _MIN_SPOOL_BYTES if key == "spool_max_bytes" else 1
                    self._settings[key] = max(minimum, int(values[key]))
            except (TypeError, ValueError):
                continue

    def _capture(
        self,
        kind: str,
        session_id: str,
        payload: dict[str, Any],
        *,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        with self._capture_lock:
            safe_payload = redact(payload, self._secrets)
            hashes, boundary, delta, proposed_state = self._incremental_boundary(
                session_id, messages
            )
            if messages is not None:
                roles = {
                    item.get("role")
                    for item in delta
                    if isinstance(item, dict) and isinstance(item.get("role"), str)
                }
                if "user" in roles:
                    safe_payload.pop("user", None)
                if "assistant" in roles:
                    safe_payload.pop("assistant", None)
            builder = self._require_event_builder()
            if messages is None:
                events = iter(
                    (
                        builder.payload_event(
                            kind,
                            session_id,
                            safe_payload,
                            boundary=boundary,
                        ),
                    )
                )
            else:
                events = builder.iter_message_events(
                    kind,
                    session_id,
                    delta,
                    start_index=boundary["start"],
                    payload=safe_payload,
                )
            captured = 0
            delivered_to_spool = True
            for event in events:
                if hashes:
                    event.setdefault("source_message_hashes", hashes)
                if not self._enqueue(event):
                    delivered_to_spool = False
                    break
                captured += 1
            if captured and delivered_to_spool:
                self._counters["captured"] += captured
                if proposed_state is not None:
                    self._commit_capture_state(session_id, proposed_state)

    def _capture_snapshot(self, kind: str, session_id: str, messages: list[dict[str, Any]]) -> None:
        with self._capture_lock:
            event = self._require_event_builder().payload_event(
                kind,
                session_id,
                {
                    "session_complete": True,
                    "total_message_boundary": {"start": 0, "end": len(messages)},
                    "protocol": "stream-v2",
                },
                boundary={"start": 0, "end": len(messages)},
            )
            if self._enqueue(event):
                self._counters["captured"] += 1

    def _incremental_boundary(
        self, session_id: str, messages: list[dict[str, Any]] | None
    ) -> tuple[list[str], dict[str, int], list[Any], dict[str, Any] | None]:
        state = self._capture_state.get(session_id, {"base": 0, "hashes": []})
        previous_base = int(state["base"])
        previous_hashes = list(state["hashes"])
        previous_end = previous_base + len(previous_hashes)
        if not messages:
            return [], {"start": previous_end, "end": previous_end}, [], None
        total = len(messages)
        current_base = max(0, total - _MAX_MESSAGE_HASHES)
        safe_messages = redact(messages[current_base:], self._secrets)
        current_hashes = [
            hashlib.sha256(
                json.dumps(
                    message, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            for message in safe_messages
        ]
        overlap_start = max(previous_base, current_base)
        overlap_end = min(previous_end, total)
        unchanged = overlap_end >= overlap_start
        if unchanged:
            for absolute in range(overlap_start, overlap_end):
                if (
                    previous_hashes[absolute - previous_base]
                    != current_hashes[absolute - current_base]
                ):
                    unchanged = False
                    break
        delta_start = (
            max(previous_end, current_base) if unchanged and total >= previous_end else current_base
        )
        delta_offset = max(0, delta_start - current_base)
        return (
            current_hashes[delta_offset:],
            {"start": delta_start, "end": total},
            safe_messages[delta_offset:],
            {"base": current_base, "hashes": current_hashes},
        )

    def _commit_capture_state(self, session_id: str, state: dict[str, Any]) -> None:
        self._capture_state[session_id] = state
        self._capture_state.move_to_end(session_id)
        while len(self._capture_state) > _MAX_SESSIONS:
            self._capture_state.popitem(last=False)

    def _bounded_capture(self, value: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        spool_limit = int(self._settings.get("spool_max_bytes", _MAX_CAPTURE_BYTES))
        limit = min(_MAX_CAPTURE_BYTES, max(1024, spool_limit // 2))
        if len(encoded) <= limit:
            return value
        return {
            "capture_truncated": True,
            "original_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }

    def _require_event_builder(self) -> CaptureEventBuilder:
        if self._event_builder is None:
            raise RuntimeError("event builder is not initialized")
        return self._event_builder

    def _enqueue(self, event: dict[str, Any]) -> bool:
        path = self._persist(event)
        if path is None:
            return False
        self._wake.set()
        return True

    def _sender_loop(self) -> None:
        while True:
            event, spool_path = self._next_event()
            if event is None:
                if self._stop.is_set():
                    return
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            with self._delivery_lock:
                self._current_event = event
                self._current_persisted = spool_path
            try:
                self._deliver(event)
            except SubstrateAPIError as exc:
                self._counters["delivery_failed"] += 1
                self._last_delivery_category = exc.category
                if exc.category in {"http_401", "http_403"}:
                    self._request_reconnect()
                if not self._is_transient_error(exc.category):
                    self._reset_delivery_backoff()
                    self._discard_spooled(spool_path, quarantine=True)
                    self._counters["permanent_dropped"] += 1
                else:
                    self._delivery_failure_streak += 1
                    delay = self._retry_delay(
                        exc.category,
                        self._delivery_failure_streak,
                        retry_after=exc.retry_after,
                    )
                    if spool_path is not None and self._spool is not None:
                        self._spool.release(spool_path)
                    if self._wait_for_retry(delay):
                        return
            except (KeyError, TypeError, ValueError):
                self._counters["delivery_failed"] += 1
                self._last_delivery_category = "invalid_spooled_event"
                self._reset_delivery_backoff()
                self._discard_spooled(spool_path, quarantine=True)
                self._counters["permanent_dropped"] += 1
            except Exception:  # A malformed custom client must not kill the sender.
                self._counters["delivery_failed"] += 1
                self._last_delivery_category = "delivery_exception"
                self._reset_delivery_backoff()
                self._discard_spooled(spool_path, quarantine=True)
                self._counters["permanent_dropped"] += 1
            else:
                self._reset_delivery_backoff()
                self._counters["delivered"] += 1
                self._last_delivery_category = "ok"
                if spool_path is not None and self._spool is not None:
                    self._spool.remove(spool_path)
            finally:
                with self._delivery_lock:
                    self._current_event = None
                    self._current_persisted = None
            if self._stop.is_set():
                return

    def _prefetch_loop(self) -> None:
        while True:
            try:
                query, session_id = self._prefetch_jobs.get(timeout=0.25)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                if self._stop.is_set():
                    continue
                client = self._require_client()
                memory_search = getattr(client, "memory_search", None)
                if not callable(memory_search):
                    raise SubstrateAPIError("server_upgrade_required")
                result = memory_search(query, limit=5, scope=self._scope)
                cited = self._cited_prefetch(result)
                if cited:
                    key = self._cache_key(query, session_id)
                    session_key = self._session_cache_key(session_id)
                    with self._cache_lock:
                        if session_id == self._session_id and not self._stop.is_set():
                            self._evict_prefetch_locked()
                            while len(self._prefetch_cache) >= _MAX_PREFETCH_ENTRIES:
                                self._prefetch_cache.pop(next(iter(self._prefetch_cache)))
                            expires = time.monotonic() + int(self._settings["prefetch_ttl_seconds"])
                            self._prefetch_cache[key] = (expires, cited)
                            self._latest_prefetch_cache[session_key] = (expires, cited)
                            self._latest_prefetch_cache.move_to_end(session_key)
                            while len(self._latest_prefetch_cache) > _MAX_SESSIONS:
                                self._latest_prefetch_cache.popitem(last=False)
                            self._counters["prefetch_cached"] += 1
                            self._last_prefetch_category = "ok"
            except SubstrateAPIError as exc:
                self._counters["prefetch_failed"] += 1
                self._last_prefetch_category = exc.category
            finally:
                self._prefetch_jobs.task_done()

    @staticmethod
    def _cited_prefetch(result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        raw_results = result.get("results")
        if not isinstance(raw_results, list):
            return ""
        blocks: list[str] = []
        seen_entities: set[str] = set()
        used = 0
        for item in raw_results[:5]:
            if not isinstance(item, dict):
                continue
            citation = SubstrateWikiProvider._canonical_entity_path(item)
            text = item.get("memory_card")
            if not citation or not text:
                continue
            entity_id = str(item["entity_id"]).strip()
            if entity_id in seen_entities:
                continue
            block = f"{text.strip()[:4096]}\nSource: {citation.strip()[:1024]}"
            encoded = block.encode("utf-8")
            if used + len(encoded) > _MAX_PREFETCH_BYTES:
                break
            blocks.append(block)
            seen_entities.add(entity_id)
            used += len(encoded) + 2
        return "\n\n".join(blocks)

    @staticmethod
    def _canonical_entity_path(item: dict[str, Any]) -> str | None:
        """Accept only immutable canonical entity wiki paths for automatic recall."""
        path = item.get("canonical_path")
        entity_id = item.get("entity_id")
        entity_type = item.get("entity_type")
        if (
            item.get("page_type") != "entity"
            or item.get("quality_version") != 2
            or not isinstance(item.get("memory_card"), str)
            or not isinstance(path, str)
            or not isinstance(entity_id, str)
            or not entity_id.strip()
            or len(entity_id) > 256
            or not isinstance(entity_type, str)
            or entity_type not in _ENTITY_TYPES
            or "\\" in path
        ):
            return None
        parts = path.split("/")
        if (
            len(parts) != 3
            or parts[0] != "entities"
            or parts[1] not in _ENTITY_TYPES
            or _ENTITY_FILENAME.fullmatch(parts[2]) is None
        ):
            return None
        return path

    def _next_event(self) -> tuple[dict[str, Any] | None, Path | None]:
        if self._spool is None:
            return None, None
        path = self._spool.claim_oldest()
        if path is None:
            return None, None
        try:
            return self._spool.load(path), path
        except (OSError, UnicodeError, ValueError, TypeError):
            self._spool.quarantine(path)
            self._counters["quarantined"] += 1
            return None, None

    def _deliver(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            raise ValueError("invalid event")
        kind = event.get("kind")
        event_id = event.get("event_id")
        if not isinstance(kind, str) or not isinstance(event_id, str) or not event_id:
            raise ValueError("invalid event")
        path = {
            "turn": "/api/v1/hermes/turns",
            "pre_compress": "/api/v1/hermes/turns",
            "session_boundary": "/api/v1/hermes/turns",
            "session_end": "/api/v1/hermes/completed-sessions",
            "memory_write": "/api/v1/hermes/memory-write-events",
        }.get(kind)
        if path is None:
            raise ValueError("unknown event kind")
        self._require_client().request("POST", path, body=event, idempotency_key=event_id)

    def _persist(self, event: dict[str, Any]) -> Path | None:
        if self._spool is None:
            self._counters["dropped"] += 1
            return None
        before = self._spool.evicted_count
        try:
            path = self._spool.append(event)
            self._counters["spooled"] += 1
            self._counters["spool_evicted"] += self._spool.evicted_count - before
            return path
        except (OSError, TypeError, ValueError):
            self._counters["dropped"] += 1
            return None

    def _discard_spooled(self, path: Path | None, *, quarantine: bool) -> None:
        if path is None or self._spool is None:
            return
        if quarantine:
            self._spool.quarantine(path)
            self._counters["quarantined"] += 1
        else:
            self._spool.remove(path)

    def _retry_delay(
        self,
        category: str,
        failure_streak: int,
        *,
        retry_after: float | None = None,
    ) -> float:
        base = (
            _AUTH_RETRY_BASE_SECONDS
            if category in {"http_401", "http_403", "not_configured", "invalid_api_url"}
            else _RETRY_BASE_SECONDS
        )
        exponent = min(max(0, failure_streak - 1), _MAX_RETRY_EXPONENT)
        delay = min(_MAX_RETRY_SECONDS, base * (2**exponent))
        jittered = delay * self._retry_random.uniform(
            1.0 - _RETRY_JITTER_FRACTION,
            1.0 + _RETRY_JITTER_FRACTION,
        )
        if (
            isinstance(retry_after, (int, float))
            and not isinstance(retry_after, bool)
            and math.isfinite(retry_after)
        ):
            retry_floor = max(0.0, min(float(retry_after), _MAX_RETRY_SECONDS))
            jittered = max(jittered, retry_floor)
        return max(0.0, min(jittered, _MAX_RETRY_SECONDS))

    def _reset_delivery_backoff(self) -> None:
        self._delivery_failure_streak = 0

    def _wait_for_retry(self, delay: float) -> bool:
        return self._stop.wait(delay)

    @staticmethod
    def _is_transient_error(category: str) -> bool:
        if category in {"timeout", "transport_error", "not_configured", "invalid_api_url"}:
            return True
        if category in {"http_401", "http_403", "http_429"}:
            return True
        if category.startswith("http_"):
            try:
                return int(category[5:]) >= 500
            except ValueError:
                return False
        return False

    def status_snapshot(self) -> dict[str, Any]:
        """Return bounded operational status without identifiers, content, URLs, or secrets."""
        return {
            "provider_id": _PROVIDER_ID,
            "initialized": self._home is not None,
            "primary_runtime": self._initialized_primary,
            "workers": {
                "sender": bool(self._worker and self._worker.is_alive()),
                "prefetch": bool(self._prefetch_worker and self._prefetch_worker.is_alive()),
            },
            "queues": {
                "events": min(self._events.qsize(), self._events.maxsize),
                "events_capacity": self._events.maxsize,
                "prefetch": min(self._prefetch_jobs.qsize(), self._prefetch_jobs.maxsize),
                "prefetch_capacity": self._prefetch_jobs.maxsize,
                "spool": min(len(self._spool), int(self._settings["spool_max_items"]))
                if self._spool
                else 0,
            },
            "counters": {key: int(value) for key, value in self._counters.items()},
            "last_delivery_category": self._last_delivery_category,
            "last_prefetch_category": self._last_prefetch_category,
        }

    def _capture_allowed(self, kwargs: dict[str, Any]) -> bool:
        allowed = self._initialized_primary and _is_primary_runtime(kwargs)
        if not allowed:
            self._counters["suppressed"] += 1
        return allowed

    @staticmethod
    def _read_persisted_api_url(root: Path) -> str:
        if root.is_symlink():
            return ""
        path = root / "config.json"
        if path.is_symlink():
            return ""
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            return ""
        value = values.get("api_url") if isinstance(values, dict) else None
        return (
            value.rstrip("/")
            if isinstance(value, str) and SubstrateClient.is_allowed_base_url(value)
            else ""
        )

    def _require_client(self) -> SubstrateClient:
        if self._client is None:
            raise SubstrateAPIError("not_initialized")
        return self._client

    def _evict_prefetch_locked(self) -> None:
        now = time.monotonic()
        expired = [key for key, (expires, _) in self._prefetch_cache.items() if expires <= now]
        for key in expired:
            self._prefetch_cache.pop(key, None)
        expired_sessions = [
            key for key, (expires, _) in self._latest_prefetch_cache.items() if expires <= now
        ]
        for key in expired_sessions:
            self._latest_prefetch_cache.pop(key, None)

    @staticmethod
    def _cache_key(query: str, session_id: str) -> str:
        return hashlib.sha256(f"{session_id}\0{query}".encode()).hexdigest()

    @staticmethod
    def _session_cache_key(session_id: str) -> str:
        return hashlib.sha256(session_id.encode()).hexdigest()

    @staticmethod
    def _boolean(args: dict[str, Any], key: str, default: bool) -> bool:
        value = args.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(key)
        return value

    @staticmethod
    def _input(args: dict[str, Any], key: str, maximum: int) -> str:
        value = args[key]
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(key)
        return value

    @classmethod
    def _optional_input(cls, args: dict[str, Any], key: str, maximum: int) -> str | None:
        if args.get(key) is None:
            return None
        return cls._input(args, key, maximum)

    @staticmethod
    def _source_type(value: Any) -> str:
        if value not in {"text", "url"}:
            raise ValueError("source_type")
        return str(value)


def register(ctx: Any) -> None:
    ctx.register_memory_provider(SubstrateWikiProvider())
