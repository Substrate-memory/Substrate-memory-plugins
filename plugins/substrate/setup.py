#!/usr/bin/env python3
"""One-prompt setup and credential migration for the Substrate Hermes plugin."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from . import contract
    from . import onboarding
    from .client import _profile_homes, _stored_origin, tls_context
except ImportError:
    import contract
    import onboarding
    from client import _profile_homes, _stored_origin, tls_context

CLIENT_ID = "substrate-hermes"
SCOPES = "capture retrieve"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
MAX_RESPONSE_BYTES = 65_536


class SetupError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _origin(value: str) -> str:
    result = value.rstrip("/")
    parsed = urllib.parse.urlsplit(result)
    development = os.environ.get("SUBSTRATE_DEVELOPMENT_MODE") == "1"
    allowed_hosts = {"app.trysubstrate.co", "vm-substrate-ar-01.taile961d2.ts.net"}
    try:
        valid_production = (
            parsed.scheme == "https"
            and parsed.hostname in allowed_hosts
            and parsed.port in {None, 443, 8443, 10000}
        )
    except ValueError as exc:
        raise SetupError("Substrate origin is invalid") from exc
    if (
        not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (not valid_production and not development)
    ):
        raise SetupError("Substrate origin is not an allowed HTTPS origin")
    return result


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=tls_context())
    )


def _read_json(response: Any) -> tuple[int, dict[str, Any]]:
    try:
        status = int(response.status)
        content_type = response.headers.get_content_type()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        response.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SetupError("Substrate response was too large")
    if content_type != "application/json":
        raise SetupError("Substrate returned an invalid content type")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError("Substrate returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise SetupError("Substrate returned an invalid response")
    return status, value


def _request(origin: str, path: str, *, form: dict[str, str] | None = None,
             token: str = "", timeout: float = 60.0) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json", "User-Agent": "substrate-hermes-plugin/0.2.0"}
    data = None
    method = "GET"
    if form is not None:
        data = urllib.parse.urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        method = "POST"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(origin + path, data=data, headers=headers, method=method)
    try:
        response = _opener().open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except (urllib.error.URLError, OSError, TimeoutError, socket.timeout) as exc:
        raise SetupError("Could not establish a verified TLS connection to Substrate") from exc
    return _read_json(response)


def _validate_token(origin: str, token: str) -> bool:
    try:
        status, value = _request(origin, "/api/v1/capabilities", token=token)
        if status != 200:
            return False
        contract.validate_capabilities(contract.shape_response("capabilities", value))
        status, value = _request(
            origin,
            "/api/v1/memory/search",
            method="POST",
            token=token,
            json_body={"query": "Substrate installation health check", "limit": 1},
        )
        shaped = contract.shape_response("search", value)
        return (
            status == 200
            and shaped.get("contract_version") == contract.CONTRACT_VERSION
            and isinstance(shaped.get("results"), list)
        )
    except (SetupError, contract.ContractError):
        return False


def _secure_write(path: Path, value: str) -> None:
    if path.exists() and path.is_symlink():
        raise SetupError(f"Refusing symbolic link: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _state_root(home: Path) -> Path:
    root = home / "substrate"
    if root.exists() and root.is_symlink():
        raise SetupError("Refusing symbolic-link Substrate state directory")
    return root


def _save_origin(home: Path, origin: str) -> None:
    root = _state_root(home)
    _secure_write(root / "config.json", json.dumps({"api_url": origin}, sort_keys=True) + "\n")


def _save(home: Path, origin: str, token: str) -> None:
    root = _state_root(home)
    _secure_write(root / "credentials" / "access-token", token)
    _save_origin(home, origin)


def _safe_verification_url(origin: str, value: Any, user_code: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise SetupError("Substrate returned an invalid verification URL")
    parsed = urllib.parse.urlsplit(value)
    expected = urllib.parse.urlsplit(origin)
    allowed_origins = {
        (expected.scheme, expected.netloc),
        ("https", "app.trysubstrate.co"),
    }
    if (parsed.scheme, parsed.netloc) not in allowed_origins or parsed.path != "/oauth/device":
        raise SetupError("Substrate returned an invalid verification URL")
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    if query.get("user_code") != user_code:
        raise SetupError("Substrate returned a mismatched verification URL")
    # Use the authenticated API origin's equivalent public route. This avoids a stale
    # presentation hostname without trusting a new authority.
    return urllib.parse.urlunsplit((
        expected.scheme,
        expected.netloc,
        "/oauth/device",
        urllib.parse.urlencode({"user_code": user_code}),
        "",
    ))


def connect(home: Path, origin: str) -> dict[str, Any]:
    """Connect the active profile using the plugin's device onboarding."""
    try:
        result = onboarding.run_cli(home, origin)
    except onboarding.OnboardingError as exc:
        raise SetupError(exc.category) from exc
    if result.get("status") not in {"ready"}:
        raise SetupError(str(result.get("error_class", "authorization_failed")))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect the active Hermes profile to Substrate")
    parser.add_argument("--hermes-home", type=Path)
    parser.add_argument("--origin")
    args = parser.parse_args(argv)
    homes = _profile_homes()
    active_home = (homes[0] if homes else Path.home() / ".hermes").resolve()
    home = (args.hermes_home or active_home).resolve()
    if home != active_home:
        print(json.dumps({"status": "failed", "error": "active_profile_mismatch"}), flush=True)
        return 1
    origin = _origin(
        args.origin
        or os.environ.get("SUBSTRATE_API_URL")
        or _stored_origin()
        or os.environ.get("SUBSTRATE_WIKI_ORIGIN")
        or "https://vm-substrate-ar-01.taile961d2.ts.net:10000"
    )
    try:
        result = connect(home, origin)
    except (SetupError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
