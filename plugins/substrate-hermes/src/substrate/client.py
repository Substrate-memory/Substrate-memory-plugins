"""Small stdlib HTTP client for the Substrate API."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

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
        # Unknown-handle forget (server 2acadcd: 404 {error:not_found}) is
        # permanent: kept as transport_error for the tool path (which folds
        # unknown categories), with status + transient=False so the spool
        # sender quarantines instead of retrying forever.
        return "transport_error", False
    if status == 409:
        return "conflict", False
    if status == 413:
        return "payload_too_large", False
    if 500 <= status <= 599:
        return "server_error", True
    return "transport_error", status == 408


class SubstrateClient:
    """JSON-over-HTTP client with no work performed at construction time."""

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

    def post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        timeout: float,
        idempotency_key: str | None = None,
        max_response_bytes: int = 1_048_576,
    ) -> Any:
        if not self.api_key:
            raise ClientError("invalid_config", transient=False)
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ClientError("invalid_request", transient=False)
        data = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if len(data) > _MAX_REQUEST_BYTES:
            raise ClientError("invalid_request", transient=False)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "substrate-hermes-plugin/0.4.0",
        }
        if idempotency_key:
            if not isinstance(idempotency_key, str) or len(idempotency_key.encode("utf-8")) > 256:
                raise ClientError("invalid_request", transient=False)
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            f"{self.api_url}{path}", data=data, headers=headers, method="POST"
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
            # A 503 (or any 5xx) may carry Retry-After just like a 429.
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
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ClientError("invalid_response", transient=False) from exc
