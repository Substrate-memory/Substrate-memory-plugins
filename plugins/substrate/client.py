"""Small stdlib HTTP client for the Substrate API."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ClientError(RuntimeError):
    """A bounded local error. Backend response text is never retained."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


class SubstrateClient:
    """JSON-over-HTTP client with no work performed at construction time."""

    def __init__(self, api_url: str, api_key: str) -> None:
        api_url = (api_url or "").rstrip("/")
        parsed = urllib.parse.urlsplit(api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ClientError("invalid_config")
        self.api_url = api_url
        self.api_key = api_key or ""

    @classmethod
    def from_env(cls) -> "SubstrateClient":
        return cls(
            os.environ.get("SUBSTRATE_API_URL", "https://app.trysubstrate.co"),
            os.environ.get("SUBSTRATE_API_KEY", ""),
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
            raise ClientError("invalid_config")
        data = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            f"{self.api_url}{path}", data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if isinstance(status, int) and not 200 <= status < 300:
                    raise ClientError("transport_error")
                raw = response.read(max_response_bytes + 1)
        except ClientError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise ClientError("timeout") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ClientError("transport_error") from exc
        except Exception as exc:
            raise ClientError("transport_error") from exc
        if len(raw) > max_response_bytes:
            raise ClientError("invalid_response")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ClientError("invalid_response") from exc
