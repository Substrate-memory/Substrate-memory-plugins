"""Automatic RFC 8628 device onboarding for the Substrate Hermes plugin.

The plugin connects itself: when no usable tenant-scoped key exists it starts
a device-authorization grant against the Substrate origin, surfaces the
browser approval link to the agent, polls for the issued key, validates it,
and stores it privately in the active profile's ``.env`` as
``SUBSTRATE_API_KEY``. No key is ever pasted into chat and no external setup
command is required.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from . import contract
    from .client import (
        ClientError,
        SubstrateClient,
        _stored_api_key,
        _stored_origin,
        tls_context,
    )
except ImportError:  # standalone script layout
    import contract
    from client import (
        ClientError,
        SubstrateClient,
        _stored_api_key,
        _stored_origin,
        tls_context,
    )

CLIENT_ID = "substrate-hermes"
SCOPES = "capture retrieve"
MAX_AGENT_NAME_BYTES = 64
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
ENV_KEY = "SUBSTRATE_API_KEY"
DEFAULT_ORIGIN = "https://vm-substrate-ar-01.taile961d2.ts.net:10000"
MAX_RESPONSE_BYTES = 65_536
STATE_VERSION = 1
RETRY_COOLDOWN_SECONDS = 60.0
BEGIN_TIMEOUT_SECONDS = 10.0
POLL_TIMEOUT_SECONDS = 10.0
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
FORBIDDEN_STATE_KEYS = ("access_token", "api_key", "token")
_ENV_LINE_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?SUBSTRATE_API_KEY[ \t]*=.*$", re.MULTILINE
)


class OnboardingError(RuntimeError):
    """A bounded local error; backend response text is never retained."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _clip(value: str, maximum: int) -> str:
    value = value.encode("utf-8", "replace").decode("utf-8")
    return value.encode("utf-8")[:maximum].decode("utf-8", "ignore")


def active_home() -> Path:
    """The one active profile home; no other profile is ever inspected."""
    # CODEX HOST-HOME PATCH (only intentional difference from
    # plugins/substrate/onboarding.py): the active home resolves via
    # hosthome.codex_home() (SUBSTRATE_HOME > CODEX_HOME > ~/.codex). No
    # HERMES_HOME, no <home>/plugins/substrate installed-layout walk, no
    # ~/.hermes fallback. State, credential, env, origin, and CLI paths all
    # flow from this home. Device-grant, scopes, storage paths, and
    # validation are unchanged.
    try:
        from . import hosthome
    except ImportError:  # standalone script layout
        import hosthome
    return hosthome.codex_home().resolve()


def resolve_agent_name() -> str:
    """A human-readable name for this agent, shown in the approval page."""
    candidate = (
        os.environ.get("SUBSTRATE_AGENT_NAME")
        or os.environ.get("CODEX_PROFILE")
        or os.environ.get("HERMES_PROFILE")
        or ""
    )
    candidate = _clip(" ".join(str(candidate).split()), MAX_AGENT_NAME_BYTES)
    return candidate


def resolve_origin() -> str:
    # CODEX HOST-HOME PATCH: origin's stored fallback comes from
    # client._stored_origin(), which itself resolves via hosthome.codex_home().
    # No Hermes home is ever consulted here.
    return (
        os.environ.get("SUBSTRATE_API_URL")
        or _stored_origin()
        or os.environ.get("SUBSTRATE_WIKI_ORIGIN")
        or DEFAULT_ORIGIN
    ).rstrip("/")


def check_origin(origin: str) -> str:
    """Validate an origin with the client's production allowlist rules."""
    try:
        SubstrateClient((origin or DEFAULT_ORIGIN).rstrip("/"), "origin-validation")
    except ClientError as exc:
        raise OnboardingError("invalid_config") from exc
    return (origin or DEFAULT_ORIGIN).rstrip("/")


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=tls_context())
    )


def request_json(
    origin: str,
    path: str,
    *,
    form: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    token: str = "",
    method: str = "GET",
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json", "User-Agent": "substrate-hermes-plugin/0.4.0"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        method = "POST"
    elif json_body is not None:
        data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        origin + path, data=data, headers=headers, method=method
    )
    try:
        response = _opener().open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except (urllib.error.URLError, OSError, TimeoutError, socket.timeout) as exc:
        raise OnboardingError("transport_error") from exc
    try:
        status = int(response.status)
        content_type = response.headers.get_content_type()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        response.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise OnboardingError("response_too_large")
    if content_type != "application/json":
        raise OnboardingError("invalid_content_type")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OnboardingError("invalid_response") from exc
    if not isinstance(value, dict):
        raise OnboardingError("invalid_response")
    return status, value


def safe_verification_url(origin: str, value: Any, user_code: str) -> str:
    """Accept only the server's device URL and rebuild it on the API origin."""
    if not isinstance(value, str) or len(value) > 4096:
        raise OnboardingError("invalid_response")
    parsed = urllib.parse.urlsplit(value)
    expected = urllib.parse.urlsplit(origin)
    allowed = {(expected.scheme, expected.netloc), ("https", "app.trysubstrate.co")}
    if (parsed.scheme, parsed.netloc) not in allowed or parsed.path != "/oauth/device":
        raise OnboardingError("invalid_response")
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    if query.get("user_code") != user_code:
        raise OnboardingError("invalid_response")
    return urllib.parse.urlunsplit((
        expected.scheme,
        expected.netloc,
        "/oauth/device",
        urllib.parse.urlencode({"user_code": user_code}),
        "",
    ))


def token_is_valid(origin: str, token: str) -> bool:
    """Authenticated preflight: capabilities plus one bounded search call."""
    try:
        status, value = request_json(origin, "/api/v1/capabilities", token=token)
        if status != 200:
            return False
        contract.validate_capabilities(contract.shape_response("capabilities", value))
        status, value = request_json(
            origin,
            "/api/v1/memory/search",
            json_body={"query": "Substrate installation health check", "limit": 1},
            token=token,
        )
        shaped = contract.shape_response("search", value)
    except (OnboardingError, contract.ContractError):
        return False
    return (
        status == 200
        and shaped.get("contract_version") == contract.CONTRACT_VERSION
        and isinstance(shaped.get("results"), list)
    )


def _atomic_private_write(path: Path, text: str, mode: int = 0o600) -> None:
    if path.exists() and path.is_symlink():
        raise OnboardingError("refusing_symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            os.chmod(path, mode)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_env_key(home: Path, token: str) -> None:
    """Store the key in the active profile's .env using Hermes update semantics."""
    if not token or len(token) > 16_384:
        raise OnboardingError("invalid_response")
    env_path = home / ".env"
    if env_path.is_symlink():
        raise OnboardingError("refusing_symlink")
    if env_path.exists():
        raw = env_path.read_text(encoding="utf-8-sig", errors="replace")
        if _ENV_LINE_RE.search(raw):
            text = _ENV_LINE_RE.sub(lambda _match: f"{ENV_KEY}={token}", raw)
        else:
            text = raw if (raw == "" or raw.endswith("\n")) else raw + "\n"
            text += f"{ENV_KEY}={token}\n"
    else:
        text = f"{ENV_KEY}={token}\n"
    # The file now holds a bearer credential; always tighten to owner-only.
    _atomic_private_write(env_path, text, mode=0o600)

# CODEX HOST-HOME PATCH: helper implementing the multi-host spec
# credential-location rule for this host. Same owner-only atomic write
# semantics as the token-file save in the Hermes setup flow.
def _store_token_file(home: Path, token: str) -> None:
    if not token or len(token) > 16_384:
        raise OnboardingError("invalid_response")
    _atomic_private_write(home / "substrate" / "credentials" / "access-token", token)



def _state_path(home: Path) -> Path:
    # CODEX HOST-HOME PATCH: *home* always comes from active_home()
    # (hosthome.codex_home()); this helper never resolves a home itself.
    return home / "substrate" / "onboarding.json"


def _load_state(home: Path) -> dict[str, Any]:
    path = _state_path(home)
    try:
        info = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > 16_384:
            return {"phase": "invalid"}
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"phase": "new"}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"phase": "invalid"}
    if not isinstance(value, dict) or value.get("state_version") != STATE_VERSION:
        return {"phase": "invalid"}
    for forbidden in FORBIDDEN_STATE_KEYS:
        value.pop(forbidden, None)
    return value


def _save_state(home: Path, state: dict[str, Any]) -> None:
    clean = {key: value for key, value in state.items() if key not in FORBIDDEN_STATE_KEYS}
    clean.update(state_version=STATE_VERSION, updated_at=time.time())
    _atomic_private_write(_state_path(home), json.dumps(clean, sort_keys=True) + "\n")


def _read_private_device_code(home: Path) -> str:
    path = home / "substrate" / "credentials" / "onboarding-device"
    try:
        info = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            return ""
        value = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return ""
    return value if 0 < len(value) <= 4096 else ""


def _clear_device_credential(home: Path) -> None:
    path = home / "substrate" / "credentials" / "onboarding-device"
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _save_origin(home: Path, origin: str) -> None:
    root = home / "substrate"
    if root.exists() and root.is_symlink():
        raise OnboardingError("refusing_symlink")
    _atomic_private_write(
        root / "config.json", json.dumps({"api_url": origin}, sort_keys=True) + "\n"
    )


class OnboardingManager:
    """One profile's resumable device-authorization state machine."""

    def __init__(self, home: Path, origin: str) -> None:
        self.home = home
        self.origin = origin
        self.lock = threading.RLock()
        self._thread: threading.Thread | None = None

    def describe(self, state: dict[str, Any]) -> dict[str, Any]:
        phase = state.get("phase")
        now = time.time()
        if phase == "pending":
            return {
                "status": "authorization_pending",
                "verification_uri_complete": _clip(
                    str(state.get("verification_uri_complete", "")), 512
                ),
                "user_code": _clip(str(state.get("user_code", "")), 16),
                "expires_in": max(0, int(float(state.get("expires_at", now)) - now)),
                "agent_name": _clip(str(state.get("agent_name", "")), MAX_AGENT_NAME_BYTES),
            }
        if phase in {"failed", "declined", "invalid"}:
            return {
                "status": str(phase),
                "error_class": _clip(str(state.get("error_class", "")), 48),
            }
        return {"status": _clip(str(phase), 24)}

    def _fail(self, category: str) -> dict[str, Any]:
        fresh = {"phase": "failed", "error_class": category, "attempted_at": time.time()}
        _save_state(self.home, fresh)
        return self.describe(fresh)

    def ensure(self, *, force: bool = False) -> dict[str, Any] | None:
        with self.lock:
            state = _load_state(self.home)
            now = time.time()
            if state.get("phase") == "pending" and float(state.get("expires_at", 0)) > now:
                self._start_thread()
                return self.describe(state)
            if not force and (os.environ.get(ENV_KEY) or _stored_api_key()):
                return None
            if (
                not force
                and state.get("phase") in {"failed", "declined", "invalid"}
                and float(state.get("attempted_at", 0)) + RETRY_COOLDOWN_SECONDS > now
            ):
                return self.describe(state)
            return self._begin()

    def _begin(self) -> dict[str, Any]:
        try:
            status, value = request_json(
                self.origin,
                "/oauth/device_authorization",
                form={
                    "client_id": CLIENT_ID,
                    "scope": SCOPES,
                    "agent_name": resolve_agent_name(),
                },
                timeout=BEGIN_TIMEOUT_SECONDS,
            )
        except OnboardingError as exc:
            return self._fail(exc.category)
        if status != 200:
            return self._fail("rate_limited" if status == 429 else "authorization_failed")
        device_code = value.get("device_code")
        user_code = value.get("user_code")
        if not isinstance(device_code, str) or not device_code or len(device_code) > 4096:
            return self._fail("invalid_response")
        if not isinstance(user_code, str) or not user_code or len(user_code) > 64:
            return self._fail("invalid_response")
        try:
            complete = safe_verification_url(
                self.origin, value.get("verification_uri_complete"), user_code
            )
        except OnboardingError as exc:
            return self._fail(exc.category)
        now = time.time()
        state = {
            "phase": "pending",
            "user_code": user_code,
            "agent_name": _clip(str(value.get("agent_name") or resolve_agent_name()), MAX_AGENT_NAME_BYTES),
            "verification_uri_complete": complete,
            "interval": max(1, min(int(value.get("interval", 5)), 60)),
            "expires_at": now + max(1, min(int(value.get("expires_in", 900)), 3600)),
            "attempted_at": now,
        }
        # The device code is a bearer credential for the approval grant: it lives
        # only in an owner-private file, never in the descriptive state.
        _atomic_private_write(
            self.home / "substrate" / "credentials" / "onboarding-device",
            device_code,
        )
        _save_state(self.home, state)
        self._start_thread()
        return self.describe(state)

    def _start_thread(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="substrate-onboarding", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            state = _load_state(self.home)
            if state.get("phase") != "pending":
                return
            if time.time() >= float(state.get("expires_at", 0)):
                _save_state(
                    self.home,
                    {**state, "phase": "failed", "error_class": "authorization_expired"},
                )
                return
            interval = max(1, min(int(state.get("interval", 5)), 60))
            time.sleep(interval)
            if not self._poll_once():
                return

    def _poll_once(self) -> bool:
        """One poll step. Returns True when polling should continue."""
        state = _load_state(self.home)
        if state.get("phase") != "pending":
            return False
        device_code = _read_private_device_code(self.home)
        if not device_code:
            _save_state(
                self.home,
                {**state, "phase": "failed", "error_class": "missing_device_credential"},
            )
            return False
        try:
            status, value = request_json(
                self.origin,
                "/oauth/token",
                form={
                    "grant_type": DEVICE_GRANT,
                    "device_code": device_code,
                    "client_id": CLIENT_ID,
                },
                timeout=POLL_TIMEOUT_SECONDS,
            )
        except OnboardingError as exc:
            if exc.category in {
                "transport_error", "response_too_large", "invalid_content_type",
            }:
                return True
            _save_state(
                self.home,
                {**state, "phase": "failed", "error_class": exc.category},
            )
            return False
        if status in TRANSIENT_HTTP_STATUSES:
            return True
        if status == 200:
            return self._complete(value)
        error = value.get("error")
        if error == "authorization_pending":
            return True
        if error == "slow_down":
            state["interval"] = min(60, int(state.get("interval", 5)) + 5)
            _save_state(self.home, state)
            return True
        if error == "access_denied":
            _clear_device_credential(self.home)
            _save_state(
                self.home, {**state, "phase": "declined", "error_class": "access_denied"}
            )
            return False
        if error in {"expired_token", "invalid_grant"}:
            _clear_device_credential(self.home)
            _save_state(
                self.home,
                {**state, "phase": "failed", "error_class": "authorization_expired"},
            )
            return False
        _save_state(
            self.home, {**state, "phase": "failed", "error_class": "authorization_failed"}
        )
        return False

    def _complete(self, value: dict[str, Any]) -> bool:
        state = _load_state(self.home)
        if state.get("phase") != "pending":
            return False
        token = value.get("access_token")
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 16_384
            or str(value.get("token_type", "")).casefold() != "bearer"
        ):
            _save_state(
                self.home, {**state, "phase": "failed", "error_class": "invalid_response"}
            )
            return False
        if not token_is_valid(self.origin, token):
            _clear_device_credential(self.home)
            _save_state(
                self.home,
                {**state, "phase": "failed", "error_class": "authenticated_health_check_failed"},
            )
            return False
        try:
            write_env_key(self.home, token)
            # CODEX HOST-HOME PATCH: mirror the key into the 0600
            # token file the vendored client reads first, per the multi-host
            # spec credential-location rule. The .env write above is unchanged.
            _store_token_file(self.home, token)
            _save_origin(self.home, self.origin)
        except OnboardingError as exc:
            _save_state(self.home, {**state, "phase": "failed", "error_class": exc.category})
            return False
        os.environ[ENV_KEY] = token
        _clear_device_credential(self.home)
        _save_state(
            self.home,
            {**state, "phase": "connected", "connected_at": time.time()},
        )
        return False


_manager_lock = threading.Lock()
_manager: OnboardingManager | None = None


def ensure_started(*, force: bool = False) -> dict[str, Any] | None:
    """Start or resume onboarding. Returns None once a key is available."""
    global _manager
    try:
        home = active_home()
        origin = check_origin(resolve_origin())
    except (OnboardingError, OSError, ValueError):
        return {"status": "failed", "error_class": "invalid_config"}
    with _manager_lock:
        if _manager is None or _manager.home != home or _manager.origin != origin:
            _manager = OnboardingManager(home, origin)
    try:
        return _manager.ensure(force=force)
    except Exception:
        return {"status": "failed", "error_class": "internal_error"}


def run_cli(home: Path, origin: str, *, wait: bool = True, poll_timeout: float = 900.0) -> dict[str, Any]:
    """Explicit connect used by ``setup.py``: print the link, then wait."""
    existing = os.environ.get(ENV_KEY) or _stored_api_key()
    if existing and token_is_valid(origin, existing):
        _save_origin(home, origin)
        return {"status": "ready", "credential": "existing", "origin": origin}
    manager = OnboardingManager(home, origin)
    status = manager.ensure(force=True)
    if status.get("status") != "authorization_pending" or not wait:
        return status
    print(json.dumps(status, sort_keys=True), flush=True)
    deadline = time.monotonic() + max(0.0, poll_timeout)
    while time.monotonic() < deadline:
        time.sleep(1.0)
        state = _load_state(home)
        phase = state.get("phase")
        if phase == "connected":
            return {
                "status": "ready",
                "credential": "device_authorization",
                "origin": origin,
            }
        if phase in {"failed", "declined"}:
            return manager.describe(state)
    return {"status": "failed", "error_class": "authorization_expired"}
