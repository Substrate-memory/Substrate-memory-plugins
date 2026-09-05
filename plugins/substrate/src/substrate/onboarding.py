"""RFC 8628 device onboarding for the Substrate Hermes plugin.

When no usable credential exists the plugin starts a device-authorization
grant against the Substrate origin, surfaces the browser approval link to
the agent (never a key), polls for the issued tenant-scoped key, validates
it, and stores it privately in the active profile. No key is ever pasted
into chat and no dashboard visit is required.

Missing credentials instruct the agent to run the login CLI
(``onboard.py start --json``); nothing here creates an account by itself.
A grant stays ``pending`` until the user approves it in a browser, and the
device code (a bearer credential for the grant) lives only in an
owner-private file, never in state, logs, or status output.

Standard library only.
"""

from __future__ import annotations

import argparse
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
    from . import credentials
    from .client import ClientError, SubstrateClient
except ImportError:  # standalone script layout (src/ bootstrapped on sys.path)
    try:
        from substrate import contract  # type: ignore[no-redef]
        from substrate import credentials  # type: ignore[no-redef]
        from substrate.client import ClientError, SubstrateClient  # type: ignore[no-redef]
    except ImportError:  # flat script layout (public repo shape)
        import contract  # type: ignore[no-redef]
        import credentials  # type: ignore[no-redef]
        from client import ClientError, SubstrateClient  # type: ignore[no-redef]

CLIENT_ID = "substrate-hermes"
SCOPES = "capture retrieve"
SCOPES_SET = frozenset({"capture", "retrieve"})
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
MAX_AGENT_NAME_BYTES = 64
MAX_RESPONSE_BYTES = 65_536
STATE_VERSION = 1
RETRY_COOLDOWN_SECONDS = 60.0
BEGIN_TIMEOUT_SECONDS = 10.0
POLL_TIMEOUT_SECONDS = 10.0
DEVICE_PATH = "/oauth/device"
TOKEN_RE = re.compile(r"^sk_sub_[A-Za-z0-9_\-]{8,256}$")
USER_CODE_RE = re.compile(r"^[BCDFGHJKLMNPQRSTVWXZ23456789]{4}-[BCDFGHJKLMNPQRSTVWXZ23456789]{4}$")
DEVICE_CODE_RE = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_STATE_KEYS = ("access_token", "api_key", "token", "device_code")

TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


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
    return credentials.active_home()


def resolve_agent_name() -> str:
    candidate = (
        os.environ.get("SUBSTRATE_AGENT_NAME")
        or os.environ.get("HERMES_PROFILE")
        or ""
    )
    return _clip(" ".join(str(candidate).split()), MAX_AGENT_NAME_BYTES)


def resolve_origin() -> str:
    return credentials.api_origin()


def check_origin(origin: str) -> str:
    """Validate an origin with the client's URL rules (https, or loopback)."""
    try:
        SubstrateClient((origin or "").rstrip("/") or credentials.DEFAULT_ORIGIN,
                        "origin-validation")
    except ClientError as exc:
        raise OnboardingError("invalid_config") from exc
    return ((origin or "").rstrip("/") or credentials.DEFAULT_ORIGIN).rstrip("/")


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect())


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
    headers = {
        "Accept": "application/json",
        "User-Agent": "substrate-hermes-plugin/0.4.0",
    }
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
        try:
            response.close()
        except Exception:
            pass
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
    try:
        parsed = urllib.parse.urlsplit(value)
        expected = urllib.parse.urlsplit(origin)
    except ValueError as exc:
        raise OnboardingError("invalid_response") from exc
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or parsed.path != DEVICE_PATH
    ):
        raise OnboardingError("invalid_response")
    try:
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    except ValueError as exc:
        raise OnboardingError("invalid_response") from exc
    if query.get("user_code") != user_code:
        raise OnboardingError("invalid_response")
    return urllib.parse.urlunsplit((
        expected.scheme,
        expected.netloc,
        DEVICE_PATH,
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


def _state_path(home: Path) -> Path:
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


def _atomic_private_write(path: Path, text: str, mode: int = 0o600) -> None:
    if path.exists() and path.is_symlink():
        raise OnboardingError("refusing_symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
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


def _save_state(home: Path, state: dict[str, Any]) -> None:
    clean = {k: v for k, v in state.items() if k not in FORBIDDEN_STATE_KEYS}
    clean.update(state_version=STATE_VERSION, updated_at=time.time())
    _atomic_private_write(_state_path(home), json.dumps(clean, sort_keys=True) + "\n")


def _device_credential_path(home: Path) -> Path:
    return home / "substrate" / "credentials" / "onboarding-device"


def _read_private_device_code(home: Path) -> str:
    path = _device_credential_path(home)
    try:
        info = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            return ""
        value = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return ""
    return value if 0 < len(value) <= 4096 else ""


def _clear_device_credential(home: Path) -> None:
    try:
        path = _device_credential_path(home)
        if path.is_symlink():
            return
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


class OnboardingManager:
    """One profile's resumable device-authorization state machine."""

    def __init__(self, home: Path, origin: str) -> None:
        self.home = home
        self.origin = origin
        self.lock = threading.RLock()
        self._thread: threading.Thread | None = None

    def describe(self, state: dict[str, Any]) -> dict[str, Any]:
        """Content-free summary: links and codes only, never secrets."""
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
                "agent_name": _clip(
                    str(state.get("agent_name", "")), MAX_AGENT_NAME_BYTES
                ),
            }
        if phase in {"failed", "declined", "invalid"}:
            return {
                "status": str(phase),
                "error_class": _clip(str(state.get("error_class", "")), 48),
            }
        return {"status": _clip(str(phase), 24)}

    def _fail(self, category: str) -> dict[str, Any]:
        fresh = {"phase": "failed", "error_class": category, "attempted_at": time.time()}
        try:
            _save_state(self.home, fresh)
        except OnboardingError:
            pass
        return self.describe(fresh)

    def ensure(self, *, force: bool = False) -> dict[str, Any] | None:
        """Start or resume onboarding. Returns None once a key is available."""
        with self.lock:
            state = _load_state(self.home)
            now = time.time()
            if (
                state.get("phase") == "pending"
                and float(state.get("expires_at", 0)) > now
            ):
                self._start_thread()
                return self.describe(state)
            if not force and (
                os.environ.get(credentials.ENV_KEY) or credentials.stored_api_key(self.home)
            ):
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
        if status == 401 or status == 403:
            # Fail safely: no grant, no state change beyond the error class.
            return self._fail("authorization_failed")
        if status != 200:
            return self._fail("rate_limited" if status == 429 else "authorization_failed")
        device_code = value.get("device_code")
        user_code = value.get("user_code")
        if (
            not isinstance(device_code, str)
            or DEVICE_CODE_RE.fullmatch(device_code) is None
        ):
            return self._fail("invalid_response")
        if not isinstance(user_code, str) or USER_CODE_RE.fullmatch(user_code) is None:
            return self._fail("invalid_response")
        try:
            complete = safe_verification_url(
                self.origin, value.get("verification_uri_complete"), user_code
            )
        except OnboardingError as exc:
            return self._fail(exc.category)
        try:
            interval = max(1, min(int(value.get("interval", 5)), 60))
        except (TypeError, ValueError):
            return self._fail("invalid_response")
        try:
            lifetime = max(1, min(int(value.get("expires_in", 900)), 3600))
        except (TypeError, ValueError):
            return self._fail("invalid_response")
        now = time.time()
        state = {
            "phase": "pending",
            "user_code": user_code,
            "agent_name": _clip(
                str(value.get("agent_name") or resolve_agent_name()),
                MAX_AGENT_NAME_BYTES,
            ),
            "verification_uri_complete": complete,
            "interval": interval,
            "expires_at": now + lifetime,
            "attempted_at": now,
        }
        # The device code is a bearer credential for the approval grant: it
        # lives only in an owner-private file, never in the descriptive state.
        try:
            _atomic_private_write(_device_credential_path(self.home), device_code)
            _save_state(self.home, state)
        except OnboardingError as exc:
            return self._fail(exc.category)
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
            try:
                if time.time() >= float(state.get("expires_at", 0)):
                    raise ValueError
            except (TypeError, ValueError):
                try:
                    _save_state(
                        self.home,
                        {**state, "phase": "failed",
                         "error_class": "authorization_expired"},
                    )
                except OnboardingError:
                    pass
                return
            try:
                interval = max(1, min(int(state.get("interval", 5)), 60))
            except (TypeError, ValueError):
                interval = 5
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
            try:
                _save_state(
                    self.home,
                    {**state, "phase": "failed",
                     "error_class": "missing_device_credential"},
                )
            except OnboardingError:
                pass
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
                "transport_error",
                "response_too_large",
                "invalid_content_type",
            }:
                return True
            try:
                _save_state(
                    self.home, {**state, "phase": "failed", "error_class": exc.category}
                )
            except OnboardingError:
                pass
            return False
        if status in TRANSIENT_HTTP_STATUSES:
            return True
        if status == 200:
            return self._complete(value)
        error = value.get("error")
        if error == "authorization_pending":
            return True
        if error == "slow_down":
            try:
                state["interval"] = min(60, int(state.get("interval", 5)) + 5)
                _save_state(self.home, state)
            except OnboardingError:
                pass
            return True
        if error == "access_denied":
            _clear_device_credential(self.home)
            try:
                _save_state(
                    self.home,
                    {**state, "phase": "declined", "error_class": "access_denied"},
                )
            except OnboardingError:
                pass
            return False
        if error in {"expired_token", "invalid_grant"}:
            _clear_device_credential(self.home)
            try:
                _save_state(
                    self.home,
                    {**state, "phase": "failed",
                     "error_class": "authorization_expired"},
                )
            except OnboardingError:
                pass
            return False
        try:
            _save_state(
                self.home,
                {**state, "phase": "failed", "error_class": "authorization_failed"},
            )
        except OnboardingError:
            pass
        return False

    def _complete(self, value: dict[str, Any]) -> bool:
        state = _load_state(self.home)
        if state.get("phase") != "pending":
            return False
        token = value.get("access_token")
        scope = value.get("scope", "")
        if (
            not isinstance(token, str)
            or TOKEN_RE.fullmatch(token) is None
            or str(value.get("token_type", "")).casefold() != "bearer"
            or (
                {part for part in str(scope).split() if part} != SCOPES_SET
            )
        ):
            try:
                _save_state(
                    self.home,
                    {**state, "phase": "failed", "error_class": "invalid_response"},
                )
            except OnboardingError:
                pass
            return False
        if not token_is_valid(self.origin, token):
            _clear_device_credential(self.home)
            try:
                _save_state(
                    self.home,
                    {**state, "phase": "failed",
                     "error_class": "authenticated_health_check_failed"},
                )
            except OnboardingError:
                pass
            return False
        try:
            credentials.store_token(self.home, self.origin, token)
        except credentials.CredentialError as exc:
            try:
                _save_state(
                    self.home, {**state, "phase": "failed", "error_class": exc.category}
                )
            except OnboardingError:
                pass
            return False
        os.environ[credentials.ENV_KEY] = token
        _clear_device_credential(self.home)
        try:
            _save_state(
                self.home, {**state, "phase": "connected", "connected_at": time.time()}
            )
        except OnboardingError:
            pass
        return False


_manager_lock = threading.Lock()
_manager: "OnboardingManager | None" = None


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


def note_auth_failure(rejected: str = "") -> None:
    """Reconnect handling for a 401/403: heal the in-process credential view.

    ``rejected`` is the exact token the failed request presented ("" when
    unknown). When it matches the host environment value, that value is
    stale (e.g. the profile ``.env`` loaded at host startup, since replaced
    by a login in another process): reload the login-written key in-process
    when one exists, else stop presenting the known-rejected credential.
    Only the process environment is touched — never credential files, never
    unrelated variables, and nothing secret is printed. Otherwise the
    previous behavior holds: an explicit environment key belonging to an
    advanced setup is left alone, and a rejected stored credential is
    deleted. Spooled events stay spooled throughout.
    """
    try:
        home = active_home()
    except (OSError, ValueError):
        return
    env_token = os.environ.get(credentials.ENV_KEY, "").strip()
    if env_token:
        if rejected and rejected == env_token:
            try:
                stored = credentials.stored_api_key(home)
            except (OSError, ValueError):
                stored = ""
            try:
                if stored and stored != rejected:
                    os.environ[credentials.ENV_KEY] = stored
                    return
                os.environ.pop(credentials.ENV_KEY, None)
            except Exception:
                pass
        else:
            # An explicit environment key belongs to an advanced setup;
            # never delete what this plugin did not store.
            return
    try:
        credentials.clear_stored_token(home)
    except (OSError, ValueError):
        return
    try:
        state = _load_state(home)
        if state.get("phase") != "pending":
            _save_state(
                home,
                {"phase": "failed", "error_class": "reconnect_required",
                 "attempted_at": 0.0},
            )
    except OnboardingError:
        pass


# ---------------------------------------------------------------------------
# Login CLI: ``onboard.py start|status|poll [--json]``
# ---------------------------------------------------------------------------


def _print_payload(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True), flush=True)
        return
    status = payload.get("status")
    if status == "authorization_pending":
        print(
            "Substrate memory needs one-time browser approval.\n"
            f"Link: {payload.get('verification_uri_complete')}\n"
            f"One-time code: {payload.get('user_code')} "
            f"(valid for {payload.get('expires_in')} seconds).\n"
            "Open the link, sign in by email, and approve the connection. "
            "Then re-run with `status` (or wait with `poll`). "
            "Never paste an API key into chat.",
            flush=True,
        )
    elif status == "ready":
        print(
            f"Substrate memory is connected ({payload.get('credential', 'ok')}).",
            flush=True,
        )
    else:
        print(
            f"Substrate onboarding status: {status}"
            + (f" ({payload.get('error_class')})" if payload.get("error_class") else "")
            + ".",
            flush=True,
        )


def _resolve_cli_home(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return active_home()


def _cli_status(home: Path, as_json: bool) -> int:
    state = _load_state(home)
    manager = OnboardingManager(home, resolve_origin())
    described = manager.describe(state)
    if described.get("status") == "connected":
        existing = os.environ.get(credentials.ENV_KEY) or credentials.stored_api_key(home)
        if existing:
            _print_payload({"status": "ready", "credential": "stored"}, as_json)
            return 0
        _print_payload({"status": "failed", "error_class": "missing_credential"}, as_json)
        return 1
    _print_payload(described, as_json)
    return 0 if described.get("status") == "authorization_pending" else 1


def _cli_start(home: Path, origin: str, as_json: bool) -> int:
    existing = os.environ.get(credentials.ENV_KEY) or credentials.stored_api_key(home)
    if existing and token_is_valid(origin, existing):
        try:
            credentials.save_origin(home, origin)
        except credentials.CredentialError:
            pass
        _print_payload({"status": "ready", "credential": "existing"}, as_json)
        return 0
    manager = OnboardingManager(home, origin)
    status = manager.ensure(force=True)
    if status is None:
        _print_payload({"status": "ready", "credential": "existing"}, as_json)
        return 0
    _print_payload(status, as_json)
    return 0 if status.get("status") == "authorization_pending" else 1


def _cli_poll(home: Path, origin: str, timeout: float, as_json: bool) -> int:
    manager = OnboardingManager(home, origin)
    status = manager.ensure()
    if status is None:
        _print_payload({"status": "ready", "credential": "existing"}, as_json)
        return 0
    if status.get("status") != "authorization_pending":
        _print_payload(status, as_json)
        return 1
    if not as_json:
        _print_payload(status, False)
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        time.sleep(1.0)
        state = _load_state(home)
        phase = state.get("phase")
        if phase == "connected":
            _print_payload(
                {"status": "ready", "credential": "device_authorization"}, as_json
            )
            return 0
        if phase in {"failed", "declined", "invalid"}:
            _print_payload(manager.describe(state), as_json)
            return 1
    # Bounded wait over: re-emit the pending descriptor (link included) as the
    # single JSON document so the agent can show it and re-poll.
    _print_payload(manager.describe(_load_state(home)), as_json)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Connect this Hermes profile to Substrate (device login)."
    )
    parser.add_argument("--hermes-home", default=None,
                        help="profile home (default: HERMES_HOME or ~/.hermes)")
    parser.add_argument("--origin", default=None,
                        help="Substrate origin (default: stored or "
                             "https://app.trysubstrate.co)")
    parser.add_argument("--name", default=None,
                        help="display name shown for this agent during approval")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="emit content-free JSON (no secrets)")
    parser.add_argument("--headless", action="store_true",
                        help="no browser is opened; print the approval link")
    sub = parser.add_subparsers(dest="command")
    start = sub.add_parser("start", help="begin device authorization and show the link")
    status = sub.add_parser("status", help="show content-free onboarding status")
    poll = sub.add_parser("poll", help="wait for browser approval (bounded)")
    poll.add_argument("--timeout", type=float, default=900.0,
                      help="maximum seconds to wait (default 900)")
    for command_parser in (start, status, poll):
        command_parser.add_argument("--json", dest="as_json", action="store_true",
                                    help="emit content-free JSON (no secrets)")
        command_parser.add_argument("--headless", action="store_true",
                                    help="no browser is opened; print the approval link")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.name:
        os.environ["SUBSTRATE_AGENT_NAME"] = str(args.name)[:MAX_AGENT_NAME_BYTES]
    try:
        home = _resolve_cli_home(args.hermes_home)
        origin = check_origin(args.origin or resolve_origin())
    except (OnboardingError, OSError, ValueError):
        _print_payload({"status": "failed", "error_class": "invalid_config"},
                       bool(args.as_json))
        return 1
    command = args.command or "status"
    try:
        if command == "start":
            return _cli_start(home, origin, bool(args.as_json))
        if command == "poll":
            return _cli_poll(home, origin, float(args.timeout), bool(args.as_json))
        return _cli_status(home, bool(args.as_json))
    except (OnboardingError, credentials.CredentialError):
        _print_payload({"status": "failed", "error_class": "internal_error"},
                       bool(args.as_json))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
