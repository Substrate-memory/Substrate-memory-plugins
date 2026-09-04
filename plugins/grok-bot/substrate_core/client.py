"""Small stdlib HTTP client for the Substrate API."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_MAX_TOKEN_BYTES = 16_384


class ClientError(RuntimeError):
    """A bounded local error. Backend response text is never retained."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _profile_homes() -> list[Path]:
    """Return likely active Grok Bot homes without inspecting other profiles."""
    # GROK-BOT HOST-HOME PATCH (only intentional difference from
    # plugins/substrate/client.py): resolve via hosthome.grok_home()
    # (SUBSTRATE_HOME > GROK_HOME > ~/.grok). No HERMES_HOME, no
    # <home>/plugins/substrate installed-layout walk, no ~/.hermes fallback,
    # no ~/.config/grok second-profile scan (audit MINOR-3 — a Grok session
    # must never read another profile's credential). A repo checkout path
    # must never resolve as a home. All server-compatible behavior (CLIENT
    # allowlist, limits, validation) is unchanged.
    try:
        from .hosthome import grok_home
    except ImportError:  # standalone script layout
        from hosthome import grok_home  # type: ignore[no-redef]
    values: list[Path] = [grok_home()]
    # Never inspect a fallback profile when an active/configured profile is known.
    selected = values[:1]
    result: list[Path] = []
    for value in selected:
        try:
            resolved = value.resolve()
        except OSError:
            continue
        if resolved not in result:
            result.append(resolved)
    return result


def _read_private_token(path: Path) -> str:
    """Read only an owner-private regular token file; otherwise return empty."""
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > _MAX_TOKEN_BYTES:
            return ""
        getuid = getattr(os, "getuid", None)
        if callable(getuid) and (info.st_uid != getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            return ""
        value = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return ""
    return value.strip() if 0 < len(value.strip()) <= _MAX_TOKEN_BYTES else ""


def _secret_service_token(home: Path) -> str:
    """Read the legacy token from Secret Service without placing it in arguments or logs."""
    executable = shutil.which("secret-tool")
    if not executable:
        return ""
    account = hashlib.sha256(os.fsencode(str(home.resolve()))).hexdigest()[:24] + ":profile"
    try:
        result = subprocess.run(
            (
                executable,
                "lookup",
                "service",
                "co.trysubstrate.hermes",
                "account",
                account,
                "slot",
                "access-token",
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0 or len(result.stdout) > _MAX_TOKEN_BYTES:
        return ""
    try:
        value = result.stdout.decode("utf-8").strip()
    except UnicodeError:
        return ""
    return value if 0 < len(value) <= _MAX_TOKEN_BYTES else ""


def _env_file_token(home: Path) -> str:
    # GROK-BOT HOST-HOME PATCH: <home>/.env fallback for SUBSTRATE_API_KEY. Hosts here do not
    # load the profile .env into the plugin environment, while onboarding
    # persists the key there; without this the plugin re-onboards forever.
    # Same strict checks as _read_private_token: regular file, not a symlink,
    # owned by the current uid, no group/other permission bits, bounded size;
    # only the SUBSTRATE_API_KEY line is parsed and the value is never logged.
    path = home / ".env"
    try:
        info = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or path.is_symlink()
            or info.st_size > _MAX_TOKEN_BYTES
        ):
            return ""
        getuid = getattr(os, "getuid", None)
        if callable(getuid) and (
            info.st_uid != getuid() or stat.S_IMODE(info.st_mode) & 0o077
        ):
            return ""
        text = path.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, OSError, UnicodeError):
        return ""
    value = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("export"):
            rest = stripped[len("export"):]
            if rest[:1] in (" ", "\t"):
                stripped = rest.strip()
        name, sep, raw = stripped.partition("=")
        if not sep or name.strip() != "SUBSTRATE_API_KEY":
            continue
        candidate = raw.strip()
        if (
            len(candidate) >= 2
            and candidate[0] == candidate[-1]
            and candidate[0] in ("'", '"')
        ):
            candidate = candidate[1:-1].strip()
        value = candidate
    value = value.strip()
    return value if 0 < len(value) <= _MAX_TOKEN_BYTES else ""


def _stored_api_key() -> str:
    """Resolve this plugin's token, then a secure legacy-plugin token for migration."""
    homes = _profile_homes()
    for home in homes:
        for relative in (
            ("substrate", "credentials", "access-token"),
            ("substrate_wiki", "credentials", "access-token"),
        ):
            value = _read_private_token(home.joinpath(*relative))
            if value:
                return value
    # GROK-BOT HOST-HOME PATCH: fall back to SUBSTRATE_API_KEY in the host profile <home>/.env
    # written by onboarding. Same strict file checks as _read_private_token.
    for home in homes:
        value = _env_file_token(home)
        if value:
            return value
    for home in homes:
        value = _secret_service_token(home)
        if value:
            return value
    return ""


def _stored_origin() -> str:
    for home in _profile_homes():
        path = home / "substrate" / "config.json"
        try:
            info = path.stat(follow_symlinks=False)
            getuid = getattr(os, "getuid", None)
            if (
                path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or info.st_size > 16_384
                or (callable(getuid) and (info.st_uid != getuid() or stat.S_IMODE(info.st_mode) & 0o077))
            ):
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            continue
        origin = value.get("api_url") if isinstance(value, dict) else None
        if isinstance(origin, str) and origin:
            return origin
    return ""


_ROOT_FINGERPRINTS = {
    "isrg-root-x1.pem": "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6",
    "isrg-root-x2.pem": "69729b8e15a86efc177a57afb7171dfc64add28c2fca8cf1507e34453ccb1470",
}


def tls_context() -> ssl.SSLContext:
    """Use system trust plus verified public ISRG roots for Let's Encrypt chains."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    root_dir = Path(__file__).resolve().parent / "ca"
    for name, expected in _ROOT_FINGERPRINTS.items():
        try:
            pem = (root_dir / name).read_text(encoding="ascii")
            der = ssl.PEM_cert_to_DER_cert(pem)
            if hashlib.sha256(der).hexdigest() != expected:
                raise ClientError("invalid_config")
            context.load_verify_locations(cadata=pem)
        except (FileNotFoundError, OSError, UnicodeError, ValueError, ssl.SSLError) as exc:
            raise ClientError("invalid_config") from exc
    return context


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _open_request(request: urllib.request.Request, *, timeout: float) -> Any:
    parsed = urllib.parse.urlsplit(request.full_url)
    handlers: list[Any] = [_NoRedirect()]
    if parsed.scheme == "https":
        handlers.append(urllib.request.HTTPSHandler(context=tls_context()))
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout)


class SubstrateClient:
    """JSON-over-HTTP client with no work performed at construction time."""

    def __init__(self, api_url: str, api_key: str) -> None:
        api_url = (api_url or "").rstrip("/")
        parsed = urllib.parse.urlsplit(api_url)
        development = os.environ.get("SUBSTRATE_DEVELOPMENT_MODE") == "1"
        allowed_hosts = {"app.trysubstrate.co", "vm-substrate-ar-01.taile961d2.ts.net"}
        try:
            valid_production = (
                parsed.scheme == "https"
                and parsed.hostname in allowed_hosts
                and parsed.port in {None, 443, 8443, 10000}
            )
        except ValueError as exc:
            raise ClientError("invalid_config") from exc
        if (
            not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or (not valid_production and not development)
        ):
            raise ClientError("invalid_config")
        self.api_url = api_url
        self.api_key = api_key or ""

    @classmethod
    def from_env(cls) -> "SubstrateClient":
        return cls(
            os.environ.get("SUBSTRATE_API_URL")
            or _stored_origin()
            or os.environ.get("SUBSTRATE_WIKI_ORIGIN")
            or "https://vm-substrate-ar-01.taile961d2.ts.net:10000",
            os.environ.get("SUBSTRATE_API_KEY") or _stored_api_key(),
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
            with _open_request(request, timeout=timeout) as response:
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
