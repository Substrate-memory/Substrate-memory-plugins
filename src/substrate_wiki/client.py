"""Bounded standard-library HTTP client for the Substrate wiki API."""

from __future__ import annotations

import ipaddress
import json
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_REQUEST_BYTES = 512 * 1024
_MAX_QUERY_CHARS = 4096

# Endpoint-specific response shaping keeps a compromised or misbehaving origin from
# smuggling an arbitrary near-1MiB object through as a "search result" or "answer":
# every field is capped in count and length before it reaches the tool-call layer.
_MAX_RESULT_ITEMS = 25
_MAX_CITATION_ITEMS = 50
_MAX_SHORT_FIELD_CHARS = 2048
_MAX_TEXT_FIELD_CHARS = 65536
_MAX_MEMORY_CARD_CHARS = 8192
_USER_AGENT = "substrate_wiki-hermes-plugin/2.0.0"
_PLUGIN_VERSION = (2, 0, 0)


class SubstrateAPIError(RuntimeError):
    """A sanitized API failure with a stable category and optional retry hint."""

    def __init__(self, category: str, *, retry_after: float | None = None) -> None:
        self.category = category
        self.retry_after = (
            float(retry_after)
            if isinstance(retry_after, (int, float))
            and not isinstance(retry_after, bool)
            and math.isfinite(retry_after)
            and retry_after >= 0
            else None
        )
        super().__init__(category)


def _strict_semver(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if (
        len(parts) != 3
        or any(not part.isascii() or not part.isdigit() for part in parts)
        or any(len(part) > 1 and part.startswith("0") for part in parts)
    ):
        return None
    version = tuple(int(part) for part in parts)
    if any(part > 1_000_000 for part in version):
        return None
    return version  # type: ignore[return-value]


def validate_capabilities(
    capabilities: dict[str, Any], *, require_replay: bool = True, require_entity: bool = True
) -> None:
    """Validate the hosted connection without admitting partial/legacy contracts."""
    if capabilities.get("provider") != "substrate_wiki":
        raise SubstrateAPIError("server_upgrade_required")
    if require_replay:
        replay = capabilities.get("history_replay")
        minimum = _strict_semver(replay.get("min_plugin_version")) if isinstance(replay, dict) else None
        valid_replay = (
            isinstance(replay, dict)
            and 2 in capabilities.get("capture_schema_versions", [])
            and replay.get("protocol") == "stream-v2"
            and minimum is not None
            and _PLUGIN_VERSION >= minimum
            and replay.get("content_free_completion") is True
            and replay.get("incremental_windows") is True
            and int(replay.get("status_version", 0)) == 2
            and int(capabilities.get("max_event_bytes", 0)) == 262_144
        )
        if not valid_replay:
            raise SubstrateAPIError("server_upgrade_required")
    if require_entity:
        entity = capabilities.get("entity_memory")
        quality = capabilities.get("entity_quality")
        entity_min = _strict_semver(entity.get("min_plugin_version")) if isinstance(entity, dict) else None
        quality_min = _strict_semver(quality.get("min_plugin_version")) if isinstance(quality, dict) else None
        valid_entity = (
            isinstance(entity, dict) and isinstance(quality, dict)
            and entity.get("protocol") == "entity-wiki-v1"
            and entity_min is not None and _PLUGIN_VERSION >= entity_min
            and entity.get("search_endpoint") == "/api/v1/hermes/memory/search"
            and entity.get("canonical_wiki_pages") is True
            and entity.get("entity_page_type") == "entity"
            and quality.get("protocol") == "entity-quality-v2"
            and quality_min is not None and _PLUGIN_VERSION >= quality_min
            and quality.get("memory_card") is True
            and quality.get("quality_version") == 2
            and quality.get("canonical_redirects") is True
        )
        if not valid_entity:
            raise SubstrateAPIError("server_upgrade_required")


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects so bearer credentials never leave the configured origin."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _content_type(headers: Any) -> str:
    value = headers.get("Content-Type", "") if headers is not None else ""
    return value.split(";", 1)[0].strip().lower()


def _retry_after_seconds(headers: Any, *, now: datetime | None = None) -> float | None:
    value = headers.get("Retry-After", "") if headers is not None else ""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            target = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        moment = now or datetime.now(UTC)
        seconds = (target - moment).total_seconds()
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


@dataclass(slots=True)
class SubstrateClient:
    base_url: str
    api_key: str
    timeout: float = 10.0
    max_response_bytes: int = _MAX_RESPONSE_BYTES
    _entity_wiki_capable: bool | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.max_response_bytes = max(1024, min(int(self.max_response_bytes), 8 * 1024 * 1024))
        if self.base_url and not self.is_allowed_base_url(self.base_url):
            raise SubstrateAPIError("invalid_api_url")

    @staticmethod
    def is_allowed_base_url(base_url: str) -> bool:
        """Return whether a base URL is safe for bearer-authenticated requests."""
        try:
            parsed = urlsplit(base_url)
            host = parsed.hostname
            _port = parsed.port
        except (TypeError, ValueError):
            return False
        raw_path = parsed.path or ""
        decoded_path = unquote(raw_path)
        path_parts = decoded_path.split("/")
        if (
            not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (
                raw_path not in {"", "/"}
                and (not raw_path.startswith("/") or raw_path.endswith("/"))
            )
            or "//" in raw_path
            or any(part in {".", ".."} for part in path_parts)
            or "\\" in decoded_path
        ):
            return False
        if parsed.scheme == "https":
            return True
        if parsed.scheme != "http":
            return False
        normalized_host = host.rstrip(".").lower()
        if normalized_host == "localhost":
            return True
        try:
            return ipaddress.ip_address(normalized_host).is_loopback
        except ValueError:
            return False

    @classmethod
    def from_env(
        cls,
        *,
        timeout: float = 10.0,
        fallback_url: str = "",
        hermes_home: Any = None,
        hosted_default: bool = False,
    ) -> SubstrateClient:
        """Resolve explicit legacy env configuration, then hosted onboarding custody."""
        hosted_origin = "https://app.trysubstrate.co"
        base_url = os.environ.get("HERMES_API_URL", "") or fallback_url
        api_key = os.environ.get("HERMES_API_KEY", "")
        if hosted_default:
            if base_url and base_url.rstrip("/") != hosted_origin:
                raise SubstrateAPIError("unsafe_hosted_origin_override")
            base_url = hosted_origin
        if not api_key and hermes_home is not None and base_url.rstrip("/") == hosted_origin:
            from pathlib import Path

            from .credentials import credential_store

            api_key = credential_store(Path(hermes_home)).get()
        return cls(base_url, api_key, timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        if not self.base_url or not self.api_key:
            raise SubstrateAPIError("not_configured")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise SubstrateAPIError("invalid_request")
        url = f"{self.base_url}{path}"
        if query:
            clean_query: dict[str, Any] = {}
            for key, value in query.items():
                if value is None:
                    continue
                text = str(value)
                if len(text) > _MAX_QUERY_CHARS:
                    raise SubstrateAPIError("invalid_request")
                clean_query[str(key)] = value
            url += "?" + urlencode(clean_query)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": _USER_AGENT,
        }
        data = None
        if body is not None:
            try:
                data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError):
                raise SubstrateAPIError("invalid_request") from None
            if len(data) > _MAX_REQUEST_BYTES:
                raise SubstrateAPIError("request_too_large")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            if len(idempotency_key) > 256:
                raise SubstrateAPIError("invalid_request")
            headers["Idempotency-Key"] = idempotency_key
        request = Request(url, data=data, headers=headers, method=method.upper())
        try:
            opener = build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=self.timeout) as response:  # noqa: S310
                if _content_type(getattr(response, "headers", None)) != "application/json":
                    raise SubstrateAPIError("invalid_content_type")
                declared = response.headers.get("Content-Length")
                if declared:
                    try:
                        if int(declared) > self.max_response_bytes:
                            raise SubstrateAPIError("response_too_large")
                    except ValueError:
                        raise SubstrateAPIError("invalid_response") from None
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise SubstrateAPIError("response_too_large")
                if not raw:
                    return {}
                try:
                    decoded = raw.decode("utf-8", errors="strict")
                    value = json.loads(decoded)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise SubstrateAPIError("invalid_response") from None
                return self._shape_response(path, value)
        except SubstrateAPIError:
            raise
        except HTTPError as exc:
            retry_after = _retry_after_seconds(exc.headers) if exc.code == 429 else None
            raise SubstrateAPIError(f"http_{exc.code}", retry_after=retry_after) from None
        except TimeoutError:
            raise SubstrateAPIError("timeout") from None
        except (URLError, OSError):
            raise SubstrateAPIError("transport_error") from None

    @classmethod
    def _shape_response(cls, path: str, value: Any) -> dict[str, Any] | list[Any]:
        if path.endswith("/capabilities"):
            if not isinstance(value, dict):
                raise SubstrateAPIError("invalid_response")
            replay = value.get("history_replay")
            versions = value.get("capture_schema_versions")
            if not isinstance(replay, dict) or not isinstance(versions, list):
                raise SubstrateAPIError("invalid_response")
            shaped: dict[str, Any] = {
                "provider": cls._cap_scalar(value.get("provider"), _MAX_SHORT_FIELD_CHARS),
                "server_commit": cls._cap_scalar(
                    value.get("server_commit"), _MAX_SHORT_FIELD_CHARS
                ),
                "capture_schema_versions": [
                    item
                    for item in versions[:8]
                    if isinstance(item, int) and not isinstance(item, bool)
                ],
                "max_event_bytes": value.get("max_event_bytes"),
                "history_replay": cls._select_fields(
                    replay,
                    (
                        "protocol",
                        "min_plugin_version",
                        "content_free_completion",
                        "incremental_windows",
                        "status_version",
                    ),
                ),
            }
            entity_memory = value.get("entity_memory")
            if isinstance(entity_memory, dict):
                shaped["entity_memory"] = cls._select_fields(
                    entity_memory,
                    (
                        "protocol",
                        "min_plugin_version",
                        "search_endpoint",
                        "canonical_wiki_pages",
                        "entity_page_type",
                    ),
                )
            entity_quality = value.get("entity_quality")
            if isinstance(entity_quality, dict):
                shaped["entity_quality"] = cls._select_fields(
                    entity_quality,
                    (
                        "protocol",
                        "min_plugin_version",
                        "memory_card",
                        "quality_version",
                        "canonical_redirects",
                    ),
                )
            return shaped
        if path.endswith("/search") or path.endswith("/representation-context"):
            items = value.get("results", []) if isinstance(value, dict) else value
            if not isinstance(items, list):
                raise SubstrateAPIError("invalid_response")
            memory_only = path.endswith("/memory/search") or path.endswith(
                "/representation-context"
            )
            shaped_results = [
                cls._shape_memory_item(item) if memory_only else cls._shape_item(item)
                for item in items[:_MAX_RESULT_ITEMS]
                if isinstance(item, dict)
            ]
            return {"results": shaped_results}
        if path.endswith("/read"):
            if not isinstance(value, dict):
                raise SubstrateAPIError("invalid_response")
            return cls._select_fields(
                value, ("path", "slug", "title", "summary", "body", "content", "updated_at")
            )
        if path.endswith("/query"):
            if not isinstance(value, dict):
                raise SubstrateAPIError("invalid_response")
            shaped = cls._select_fields(
                value, ("answer", "insufficient_context", "saved", "synthesis_path")
            )
            error = value.get("error")
            if isinstance(error, dict):
                shaped_error = cls._select_fields(error, ("code", "message", "retryable"))
                if shaped_error:
                    shaped["error"] = shaped_error
            citations = value.get("citations")
            if isinstance(citations, list):
                shaped["citations"] = [
                    cls._cap_scalar(item, _MAX_SHORT_FIELD_CHARS)
                    for item in citations[:_MAX_CITATION_ITEMS]
                    if isinstance(item, (str, int, float, bool))
                ]
            return shaped
        if path.endswith("/ingest"):
            if not isinstance(value, dict):
                raise SubstrateAPIError("invalid_response")
            return cls._select_fields(value, ("job_id", "status", "duplicate"))
        if path.endswith("/job-status"):
            if not isinstance(value, dict):
                raise SubstrateAPIError("invalid_response")
            shaped = cls._select_fields(
                value,
                (
                    "id",
                    "job_id",
                    "status",
                    "kind",
                    "attempts",
                    "max_attempts",
                    "created_at",
                    "updated_at",
                    "error",
                ),
            )
            error_detail = value.get("error_detail")
            if isinstance(error_detail, dict):
                shaped_detail = cls._select_fields(error_detail, ("code", "retryable"))
                if shaped_detail:
                    shaped["error_detail"] = shaped_detail
            return shaped
        if path.startswith("/api/v1/hermes/") and isinstance(value, dict):
            shaped = cls._select_fields(
                value,
                (
                    "event_id",
                    "accepted",
                    "stored",
                    "duplicate",
                    "status",
                    "action",
                    "batch_id",
                    "job_id",
                    "state",
                    "eligible",
                    "discovered",
                    "skipped",
                    "delivered",
                    "deduplicated",
                    "processed",
                    "processed_windows",
                    "checkpointed",
                    "peak_rss_bytes",
                    "last_progress_at",
                    "error_class",
                    "quarantined",
                    "pending_review",
                    "failed",
                    "failed_windows",
                    "projected_entities",
                    "published_claims",
                    "stub_count",
                    "pending_resolution",
                    "projection_pending",
                    "complete",
                ),
            )
            results = value.get("results")
            if isinstance(results, list):
                shaped["results"] = [
                    cls._shape_item(item)
                    for item in results[:_MAX_RESULT_ITEMS]
                    if isinstance(item, dict)
                ]
            legacy = value.get("legacy_batch_ids")
            if isinstance(legacy, list):
                shaped["legacy_batch_ids"] = [
                    str(item)[:128] for item in legacy[:64] if isinstance(item, (str, int))
                ]
            return shaped
        raise SubstrateAPIError("invalid_response")

    @classmethod
    def _shape_memory_item(cls, item: dict[str, Any]) -> dict[str, Any]:
        """Admit only the canonical v2 memory-card contract for automatic recall."""
        shaped = cls._select_fields(
            item,
            (
                "path",
                "title",
                "score",
                "page_type",
                "entity_id",
                "entity_type",
                "canonical_path",
                "memory_card",
                "quality_version",
            ),
        )
        roles = item.get("roles")
        if isinstance(roles, list):
            shaped["roles"] = [role[:64] for role in roles[:16] if isinstance(role, str)]
        return shaped

    @classmethod
    def _shape_item(cls, item: dict[str, Any]) -> dict[str, Any]:
        shaped = cls._select_fields(
            item,
            (
                "path",
                "slug",
                "title",
                "summary",
                "text",
                "snippet",
                "content",
                "citation",
                "source",
                "url",
                "score",
                "page_type",
                "entity_id",
                "entity_type",
                "canonical_path",
                "memory_card",
                "quality_version",
            ),
        )
        roles = item.get("roles")
        if isinstance(roles, list):
            shaped["roles"] = [role[:64] for role in roles[:16] if isinstance(role, str)]
        return shaped

    @classmethod
    def _select_fields(cls, value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        shaped: dict[str, Any] = {}
        for key in fields:
            child = value.get(key)
            if isinstance(child, (str, int, float, bool)) or child is None:
                limit = (
                    _MAX_MEMORY_CARD_CHARS
                    if key == "memory_card"
                    else (
                        _MAX_TEXT_FIELD_CHARS
                        if key in {"body", "content", "answer", "text", "snippet"}
                        else _MAX_SHORT_FIELD_CHARS
                    )
                )
                shaped[key] = cls._cap_scalar(child, limit)
        return shaped

    @staticmethod
    def _cap_scalar(value: Any, maximum: int) -> Any:
        return value[:maximum] if isinstance(value, str) else value

    def search(self, query: str, *, limit: int = 8) -> Any:
        if not query or len(query) > _MAX_QUERY_CHARS:
            raise SubstrateAPIError("invalid_request")
        return self.request(
            "POST",
            "/api/v1/hermes/wiki/search",
            body={"q": query, "limit": limit},
        )

    def representation_context(
        self,
        query: str,
        *,
        limit: int = 8,
        scope: dict[str, Any] | None = None,
    ) -> Any:
        """Compatibility method; v1.4 uses bounded canonical memory cards."""
        return self.memory_search(query, limit=limit, scope=scope)

    def memory_search(
        self,
        query: str,
        *,
        limit: int = 8,
        scope: dict[str, Any] | None = None,
    ) -> Any:
        if not query or len(query) > _MAX_QUERY_CHARS:
            raise SubstrateAPIError("invalid_request")
        self.require_entity_wiki_capability()
        selected = scope or {}
        payload = {
            "q": query,
            "limit": limit,
            "platform": selected.get("platform") or "cli",
        }
        for target, *candidates in (
            ("user_id", "user_id", "user"),
            ("agent_id", "agent_id", "profile"),
            ("agent_identity", "agent_identity"),
            ("chat_type", "chat_type"),
        ):
            value = next((selected.get(key) for key in candidates if selected.get(key)), None)
            if value is not None:
                payload[target] = value
        return self.request(
            "POST",
            "/api/v1/hermes/memory/search",
            body=payload,
        )

    def require_entity_wiki_capability(self) -> None:
        """Fail closed unless automatic recall resolves canonical entity pages."""
        if self._entity_wiki_capable is True:
            return
        capabilities = self.capabilities()
        entity = capabilities.get("entity_memory")
        quality = capabilities.get("entity_quality")
        minimum = (
            _strict_semver(entity.get("min_plugin_version")) if isinstance(entity, dict) else None
        )
        quality_minimum = (
            _strict_semver(quality.get("min_plugin_version")) if isinstance(quality, dict) else None
        )
        valid = (
            isinstance(entity, dict)
            and isinstance(quality, dict)
            and capabilities.get("provider") == "substrate_wiki"
            and entity.get("protocol") == "entity-wiki-v1"
            and minimum is not None
            and _PLUGIN_VERSION >= minimum
            and entity.get("search_endpoint") == "/api/v1/hermes/memory/search"
            and entity.get("canonical_wiki_pages") is True
            and entity.get("entity_page_type") == "entity"
            and quality.get("protocol") == "entity-quality-v2"
            and quality_minimum is not None
            and _PLUGIN_VERSION >= quality_minimum
            and quality.get("memory_card") is True
            and quality.get("quality_version") == 2
            and quality.get("canonical_redirects") is True
        )
        if not valid:
            raise SubstrateAPIError("server_upgrade_required")
        self._entity_wiki_capable = True

    def import_status(self, batch_id: str) -> Any:
        if not batch_id or len(batch_id) > 128:
            raise SubstrateAPIError("invalid_request")
        return self.request(
            "GET",
            "/api/v1/hermes/import-status",
            query={"batch_id": batch_id},
        )

    def capabilities(self) -> dict[str, Any]:
        value = self.request("GET", "/api/v1/hermes/capabilities")
        if not isinstance(value, dict):
            raise SubstrateAPIError("invalid_response")
        return value

    def read_page(self, path: str) -> Any:
        if not path or len(path) > _MAX_QUERY_CHARS:
            raise SubstrateAPIError("invalid_request")
        return self.request("POST", "/api/v1/hermes/wiki/read", body={"path": path})

    def query_wiki(self, question: str, *, save_as_synthesis: bool = False) -> Any:
        return self.request(
            "POST",
            "/api/v1/hermes/wiki/query",
            body={"question": question, "save_as_synthesis": save_as_synthesis},
        )

    def ingest(self, content: str, *, title: str | None = None, source_type: str = "text") -> Any:
        return self.request(
            "POST",
            "/api/v1/hermes/wiki/ingest",
            body={"content": content, "title": title, "source_type": source_type},
        )

    def job_status(self, job_id: str) -> Any:
        return self.request("GET", "/api/v1/hermes/wiki/job-status", query={"job_id": job_id})
