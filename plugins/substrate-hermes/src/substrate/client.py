"""Stdlib MCP Streamable-HTTP client for the Substrate memory server.

Every server call goes to ``POST {origin}/mcp`` as MCP JSON-RPC: one
``initialize`` per process (verifying ``serverInfo.name ==
"substrate-memory"`` and the ``substrate-mcp-contract/2`` instructions
prefix), then ``tools/call`` (and ``tools/list`` for the capabilities
check). Authentication is the device-grant key
``Authorization: Bearer sk_sub_...`` with
``Accept: application/json, text/event-stream``. Both plain-JSON and SSE
(``data:`` line) response bodies are accepted. Standard library only.
"""

from __future__ import annotations

import ipaddress
import itertools
import json
import math
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

PLUGIN_VERSION = "0.7.0"
_MCP_PATH = "/mcp"
_MAX_REQUEST_BYTES = 512 * 1024


class ClientError(RuntimeError):
    """A bounded local error. Backend response text is never retained."""

    def __init__(
        self,
        category: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        transient: bool = True,
    ) -> None:
        self.category = category
        self.status = status
        if (
            isinstance(retry_after, bool)
            or not isinstance(retry_after, (int, float))
            or not math.isfinite(retry_after)
            or retry_after < 0
        ):
            retry_after = None
        self.retry_after = retry_after
        self.transient = transient
        super().__init__(category)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so bearer credentials never leave the origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN202
        return None


def _ensure_no_redirect_opener() -> None:
    """Install a process-wide opener that refuses to follow redirects."""
    global _NO_REDIRECT_INSTALLED
    if _NO_REDIRECT_INSTALLED:
        return
    try:
        current = urllib.request._opener
    except AttributeError:
        current = None
    if current is not None and any(
        isinstance(handler, _NoRedirectHandler) for handler in current.handlers
    ):
        _NO_REDIRECT_INSTALLED = True
        return
    try:
        urllib.request.install_opener(urllib.request.build_opener(_NoRedirectHandler()))
        _NO_REDIRECT_INSTALLED = True
    except Exception:  # noqa: BLE001 - redirect enforcement falls back to geturl check
        pass


_NO_REDIRECT_INSTALLED = False


def _retry_after_seconds(headers: Any) -> float | None:
    try:
        value = headers.get("Retry-After", "") if headers is not None else ""
    except Exception:  # noqa: BLE001 - a bad header map means no hint
        return None
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
            target = target.replace(tzinfo=timezone.utc)
        seconds = (target - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _is_loopback_host(host: str) -> bool:
    normalized = (host or "").rstrip(".").lower()
    if normalized in {"localhost"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _check_api_url(api_url: str) -> str:
    parsed = urllib.parse.urlsplit(api_url)
    if not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ClientError("invalid_config", transient=False)
    if parsed.scheme == "https":
        return api_url
    if parsed.scheme == "http" and _is_loopback_host(parsed.hostname or ""):
        return api_url
    raise ClientError("invalid_config", transient=False)


def _http_error_category(status: int) -> tuple[str, bool]:
    if status == 401:
        return "unauthorized", True
    if status == 403:
        return "forbidden", True
    if status == 429:
        return "rate_limited", True
    if status == 400:
        return "invalid_request", False
    if status == 404:
        return "not_found", False
    if status == 409:
        return "conflict", False
    if status == 413:
        return "payload_too_large", False
    if 500 <= status <= 599:
        return "server_error", True
    return "transport_error", status == 408


# JSON-RPC error codes that map to permanent (non-retryable) failures.
_PERMANENT_JSONRPC_CODES = frozenset({-32602})


def _tool_error_category(structured: Any) -> str:
    """Extract the contract error category from a failed tool result."""
    if isinstance(structured, dict):
        error = structured.get("error")
        if isinstance(error, str) and error:
            return error
    return "transport_error"


_PERMANENT_TOOL_ERRORS = frozenset({"invalid_request", "not_found", "forbidden"})


def parse_mcp_body(raw: bytes) -> Any:
    """Parse an MCP Streamable-HTTP body: plain JSON or SSE ``data:`` lines.

    Plain JSON is tried first. Otherwise every ``data:`` line is collected
    and the last one that decodes as JSON wins (``[DONE]`` is skipped).
    Anything else raises ``ClientError("invalid_response")``.
    """
    if not isinstance(raw, bytes) or not raw:
        raise ClientError("invalid_response", transient=False)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        pass
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ClientError("invalid_response", transient=False) from exc
    payloads: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            continue
        if stripped.startswith("data:"):
            datum = stripped[5:].strip()
            if datum and datum != "[DONE]":
                payloads.append(datum)
    for datum in reversed(payloads):
        try:
            return json.loads(datum)
        except json.JSONDecodeError:
            continue
    raise ClientError("invalid_response", transient=False)


# One verified initialize result per origin per process.
_init_lock = threading.Lock()
_init_cache: dict[str, dict[str, Any]] = {}
_id_counter = itertools.count(1)


class SubstrateClient:
    """MCP JSON-RPC client with no work performed at construction time."""

    def __init__(self, api_url: str, api_key: str) -> None:
        api_url = (api_url or "").rstrip("/")
        self.api_url = _check_api_url(api_url)
        self.api_key = api_key or ""

    @classmethod
    def from_env(cls) -> "SubstrateClient":
        try:
            from . import credentials as _credentials
        except ImportError:  # standalone script layout
            import credentials as _credentials  # type: ignore[no-redef]
        return cls(
            os.environ.get(_credentials.ENV_URL, "").strip()
            or _credentials.stored_origin()
            or os.environ.get(_credentials.LEGACY_ENV_URL, "").strip()
            or _credentials.DEFAULT_ORIGIN,
            os.environ.get(_credentials.ENV_KEY, "").strip()
            or _credentials.stored_api_key(),
        )

    # -- low-level JSON-RPC -------------------------------------------

    def _rpc(
        self, method: str, params: dict[str, Any], *, timeout: float,
        max_response_bytes: int = 1_048_576,
    ) -> Any:
        """POST one JSON-RPC message to /mcp and return its ``result``."""
        if not self.api_key:
            raise ClientError("invalid_config", transient=False)
        body = {
            "jsonrpc": "2.0",
            "id": next(_id_counter),
            "method": method,
            "params": params,
        }
        data = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if len(data) > _MAX_REQUEST_BYTES:
            raise ClientError("invalid_request", transient=False)
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"substrate-hermes-plugin/{PLUGIN_VERSION}",
        }
        request = urllib.request.Request(
            f"{self.api_url}{_MCP_PATH}", data=data, headers=headers, method="POST"
        )
        _ensure_no_redirect_opener()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if isinstance(status, int) and not 200 <= status < 300:
                    raise ClientError("transport_error")
                geturl = getattr(response, "geturl", None)
                if callable(geturl):
                    try:
                        if geturl() != request.full_url:
                            raise ClientError("transport_error")
                    except ClientError:
                        raise
                    except Exception:  # noqa: BLE001 - ignore a broken geturl
                        pass
                raw = response.read(max_response_bytes + 1)
        except ClientError:
            raise
        except urllib.error.HTTPError as exc:
            category, transient = _http_error_category(exc.code)
            retry_after = (
                _retry_after_seconds(exc.headers)
                if exc.code == 429 or 500 <= exc.code <= 599
                else None
            )
            raise ClientError(
                category, status=exc.code, retry_after=retry_after, transient=transient
            ) from None
        except (TimeoutError, socket.timeout) as exc:
            raise ClientError("timeout") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ClientError("transport_error") from exc
        except Exception as exc:
            raise ClientError("transport_error") from exc
        if len(raw) > max_response_bytes:
            raise ClientError("invalid_response", transient=False)
        envelope = parse_mcp_body(raw)
        if not isinstance(envelope, dict):
            raise ClientError("invalid_response", transient=False)
        if "error" in envelope and envelope["error"] is not None:
            detail = envelope["error"]
            code = detail.get("code") if isinstance(detail, dict) else None
            message = detail.get("message") if isinstance(detail, dict) else ""
            if code == -32000 or (isinstance(message, str) and "unauthorized" in message.lower()):
                raise ClientError("unauthorized")
            if code == -32003 or (isinstance(message, str) and "forbidden" in message.lower()):
                raise ClientError("forbidden")
            transient = code not in _PERMANENT_JSONRPC_CODES
            raise ClientError("transport_error", transient=transient)
        if "result" not in envelope:
            raise ClientError("invalid_response", transient=False)
        return envelope["result"]

    # -- initialize / tools -------------------------------------------

    def ensure_initialized(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Run MCP ``initialize`` once per process; verify the server identity.

        Refuses to run against any server whose ``serverInfo.name`` is not
        ``"substrate-memory"`` or whose ``instructions`` do not start with
        ``"substrate-mcp-contract/2"``.
        """
        try:
            from . import contract as _contract
        except ImportError:  # standalone script layout
            import contract as _contract  # type: ignore[no-redef]
        with _init_lock:
            cached = _init_cache.get(self.api_url)
            if cached is not None:
                return cached
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "substrate-hermes-plugin", "version": PLUGIN_VERSION},
            },
            timeout=timeout,
        )
        _contract.validate_mcp_initialize(result)
        with _init_lock:
            _init_cache[self.api_url] = result
        return result

    def tools_list(self, *, timeout: float = 5.0) -> list[str]:
        """Return the server's tool names; the capabilities check validates them."""
        try:
            from . import contract as _contract
        except ImportError:  # standalone script layout
            import contract as _contract  # type: ignore[no-redef]
        self.ensure_initialized(timeout=timeout)
        result = self._rpc("tools/list", {}, timeout=timeout)
        return _contract.validate_mcp_tools(result)

    def check_capabilities(self, *, timeout: float = 5.0) -> list[str]:
        """``initialize`` + ``tools/list`` containing all 12 contract tools."""
        return self.tools_list(timeout=timeout)

    def call_tool(
        self, name: str, arguments: dict[str, Any], *, timeout: float,
        max_response_bytes: int = 1_048_576,
    ) -> tuple[dict[str, Any], str]:
        """Call one MCP tool; return ``(structuredContent, text)``.

        A failed tool call (``isError: true``) raises ``ClientError`` whose
        category is the contract ``error`` value (``invalid_request``,
        ``not_found`` and ``forbidden`` are permanent; the rest retryable).
        """
        if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 128:
            raise ClientError("invalid_request", transient=False)
        if not isinstance(arguments, dict):
            raise ClientError("invalid_request", transient=False)
        self.ensure_initialized(timeout=min(timeout, 5.0))
        result = self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )
        if not isinstance(result, dict):
            raise ClientError("invalid_response", transient=False)
        if result.get("isError") is True:
            structured = result.get("structuredContent")
            if not isinstance(structured, dict):
                # Fall back to the text payload when the server sends only text.
                text = ""
                try:
                    content = result.get("content")
                    if isinstance(content, list) and content and isinstance(content[0], dict):
                        text = str(content[0].get("text", ""))
                        maybe = json.loads(text)
                        if isinstance(maybe, dict):
                            structured = maybe
                except (ValueError, TypeError):
                    structured = None
                if not isinstance(structured, dict):
                    raise ClientError("transport_error")
            category = _tool_error_category(structured)
            transient = category not in _PERMANENT_TOOL_ERRORS
            if category in ("unauthorized", "forbidden"):
                transient = True
            raise ClientError(category, transient=transient)
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            raise ClientError("invalid_response", transient=False)
        text = ""
        try:
            content = result.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                candidate = content[0].get("text", "")
                if isinstance(candidate, str):
                    text = candidate
        except (TypeError, AttributeError):
            text = ""
        return structured, text

    # -- legacy seam (tests only) --------------------------------------

    def post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        timeout: float,
        idempotency_key: str | None = None,
        max_response_bytes: int = 1_048_576,
    ) -> Any:
        """Translate a legacy REST-style call onto its MCP tool equivalent.

        Kept only so older test doubles keep working; new code must use
        :meth:`call_tool` directly.
        """
        _LEGACY_ROUTES = {
            "/api/v1/memory/turn-context": "memory_turn_context",
            "/api/v1/memory/search": "memory_search",
            "/api/v1/memory/expand": "memory_expand",
            "/api/v1/memory/evidence": "memory_evidence",
            "/api/v1/ledger/events": None,
        }
        if path not in _LEGACY_ROUTES or _LEGACY_ROUTES[path] is None:
            raise ClientError("invalid_request", transient=False)
        structured, _text = self.call_tool(
            _LEGACY_ROUTES[path], body, timeout=timeout,
            max_response_bytes=max_response_bytes,
        )
        return structured

    def reset_init_cache(self) -> None:
        """Drop the cached ``initialize`` result (tests only)."""
        with _init_lock:
            _init_cache.pop(self.api_url, None)


def reset_init_cache() -> None:
    """Drop all cached ``initialize`` results (tests only)."""
    with _init_lock:
        _init_cache.clear()
