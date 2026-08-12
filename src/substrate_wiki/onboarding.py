"""Hosted-only, resumable Substrate onboarding for Hermes.

Authentication uses the RFC 8628 device grant exposed by the fixed Azure
origin. Operational state is content-free; device/access credentials live only
in profile-scoped credential custody.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .client import SubstrateAPIError, SubstrateClient, validate_capabilities
from .credentials import CredentialStore, credential_store
from .spool import secure_atomic_json_write

HOSTED_ORIGIN = "https://app.trysubstrate.co"
CLIENT_ID = "substrate-hermes"
SCOPES = "capture retrieve"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_STATE_VERSION = 1
_PLUGIN_VERSION = "2.0.0"
_MAX_RESPONSE = 64 * 1024
_TERMINAL = {"ready", "declined", "failed", "repair_required"}


class OnboardingError(RuntimeError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _hosted_url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise OnboardingError("invalid_response")
    parsed = urlsplit(value)
    if (
        f"{parsed.scheme}://{parsed.netloc}" != HOSTED_ORIGIN
        or parsed.username or parsed.password or parsed.fragment
    ):
        raise OnboardingError("invalid_response")
    return value


class HostedOAuthClient:
    """Minimal no-redirect RFC 8628 client pinned to the hosted origin."""

    def __init__(self, *, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def _post(self, path: str, values: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if path not in {"/oauth/device_authorization", "/oauth/token"}:
            raise OnboardingError("invalid_request")
        raw = urlencode(values).encode("ascii")
        request = Request(
            HOSTED_ORIGIN + path,
            data=raw,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": f"substrate_wiki-hermes-plugin/{_PLUGIN_VERSION}",
            },
        )
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            response = exc
        except (URLError, OSError, TimeoutError):
            raise OnboardingError("transport_error") from None
        try:
            status = int(response.status)
            content_type = response.headers.get_content_type()
            data = response.read(_MAX_RESPONSE + 1)
        finally:
            response.close()
        if len(data) > _MAX_RESPONSE:
            raise OnboardingError("response_too_large")
        if content_type != "application/json":
            raise OnboardingError("invalid_content_type")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise OnboardingError("invalid_response") from None
        if not isinstance(value, dict):
            raise OnboardingError("invalid_response")
        return status, value

    def begin(self) -> dict[str, Any]:
        status, value = self._post(
            "/oauth/device_authorization", {"client_id": CLIENT_ID, "scope": SCOPES}
        )
        if status != 200:
            raise OnboardingError(str(value.get("error") or f"http_{status}"))
        required = ("device_code", "user_code", "verification_uri", "expires_in")
        if not all(isinstance(value.get(key), (str, int)) for key in required):
            raise OnboardingError("invalid_response")
        device_code = value["device_code"]
        user_code = value["user_code"]
        if not isinstance(device_code, str) or not device_code or len(device_code) > 4096:
            raise OnboardingError("invalid_response")
        if not isinstance(user_code, str) or not user_code or len(user_code) > 64:
            raise OnboardingError("invalid_response")
        verification_uri = _hosted_url(value["verification_uri"])
        complete = value.get("verification_uri_complete") or verification_uri
        return {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "verification_uri_complete": _hosted_url(complete),
            "expires_in": max(1, min(int(value["expires_in"]), 3600)),
            "interval": max(1, min(int(value.get("interval", 5)), 60)),
        }

    def poll(self, device_code: str) -> dict[str, Any]:
        status, value = self._post(
            "/oauth/token",
            {"grant_type": DEVICE_GRANT, "device_code": device_code, "client_id": CLIENT_ID},
        )
        if status == 200:
            token = value.get("access_token")
            if (
                not isinstance(token, str) or not token or len(token) > 16384
                or str(value.get("token_type", "")).casefold() != "bearer"
                or set(str(value.get("scope", SCOPES)).split()) != set(SCOPES.split())
            ):
                raise OnboardingError("invalid_response")
            return {"status": "approved", "access_token": token}
        error = value.get("error")
        if error in {"authorization_pending", "slow_down", "access_denied", "expired_token"}:
            return {"status": str(error)}
        raise OnboardingError(str(error or f"http_{status}"))


def _empty_state() -> dict[str, Any]:
    return {
        "state_version": _STATE_VERSION,
        "phase": "new",
        "hosted_origin": HOSTED_ORIGIN,
        "updated_at": time.time(),
        "attempt": 0,
    }


def _receipt(decision: str) -> dict[str, Any]:
    return {
        "version": 1,
        "scope": "hermes_history",
        "decision": decision,
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


class OnboardingManager:
    """One Hermes profile's idempotent hosted onboarding state machine."""

    def __init__(
        self,
        home: Path,
        *,
        api: HostedOAuthClient | None = None,
        store: CredentialStore | None = None,
        capability_check: Callable[[str], dict[str, Any]] | None = None,
        import_start: Callable[[Path], dict[str, Any]] | None = None,
        opener: Callable[[str], bool] | None = None,
    ) -> None:
        self.home = home.resolve()
        self.root = self.home / "substrate_wiki" / "onboarding"
        if self.root.exists() and self.root.is_symlink():
            raise OSError("onboarding directory must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(self.root, 0o700)
        self.path = self.root / "state.json"
        self.api = api or HostedOAuthClient()
        self.store = store or credential_store(self.home)
        self.capability_check = capability_check or self._check_capabilities
        self.import_start = import_start or _start_history_import
        self.opener = opener or webbrowser.open
        self._mutex = threading.RLock()

    def _load(self) -> dict[str, Any]:
        if self.path.is_symlink():
            raise OSError("onboarding state must not be a symlink")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _empty_state()
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {**_empty_state(), "phase": "repair_required", "error_class": "state_corrupt"}
        if (
            not isinstance(value, dict)
            or value.get("state_version") != _STATE_VERSION
            or value.get("hosted_origin") != HOSTED_ORIGIN
        ):
            return {**_empty_state(), "phase": "repair_required", "error_class": "state_incompatible"}
        for forbidden in ("access_token", "api_key", "device_code", "token"):
            value.pop(forbidden, None)
        return value

    def _save(self, state: dict[str, Any]) -> None:
        clean = dict(state)
        for forbidden in ("access_token", "api_key", "device_code", "token"):
            clean.pop(forbidden, None)
        clean.update(
            state_version=_STATE_VERSION, hosted_origin=HOSTED_ORIGIN, updated_at=time.time()
        )
        secure_atomic_json_write(self.path, clean)

    def _refresh_import(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("phase") != "importing":
            return state
        job_id = state.get("import_job_id")
        if not isinstance(job_id, str) or not job_id:
            return state
        try:
            from .checkpoint import ImportCheckpoint
            from .worker import checkpoint_path

            with ImportCheckpoint(checkpoint_path(self.home, job_id)) as checkpoint:
                progress = checkpoint.status()
        except (FileNotFoundError, OSError, ValueError):
            return state
        state["import"] = {
            key: progress[key]
            for key in (
                "job_id", "state", "discovered", "eligible", "delivered", "failed",
                "skipped", "quarantined", "complete",
            )
            if key in progress
        }
        if progress.get("complete"):
            state["phase"] = "ready"
            state["completed_at"] = time.time()
        self._save(state)
        return state

    def status(self) -> dict[str, Any]:
        with self._mutex:
            state = self._refresh_import(self._load())
            result = {
                key: state[key]
                for key in (
                    "phase", "hosted_origin", "mode", "verification_uri",
                    "verification_uri_complete", "user_code", "expires_at",
                    "history_consent", "error_class", "import", "connected_at",
                    "completed_at",
                )
                if key in state
            }
            result["credential_backend"] = self.store.backend
            result["authenticated"] = bool(self.store.get())
            result["complete"] = state.get("phase") in _TERMINAL
            return result

    def _check_capabilities(self, token: str) -> dict[str, Any]:
        client = SubstrateClient(HOSTED_ORIGIN, token, timeout=15.0)
        capabilities = client.capabilities()
        validate_capabilities(capabilities, require_replay=True, require_entity=True)
        return {"provider": capabilities.get("provider"), "protocol": "stream-v2"}

    def begin(self, *, mode: str = "auto", open_browser: bool = True) -> dict[str, Any]:
        with self._mutex:
            state = self._load()
            if self.store.get():
                if state.get("phase") in {"new", "authorization_pending", "failed"}:
                    state.update(phase="awaiting_history_consent", connected_at=time.time())
                    state.pop("error_class", None)
                    self._save(state)
                return self.status()
            if (
                state.get("phase") == "authorization_pending"
                and float(state.get("expires_at", 0)) > time.time()
                and self.store.get("onboarding-device")
            ):
                return self.status()
            grant = self.api.begin()
            self.store.put(str(grant.pop("device_code")), "onboarding-device")
            selected = _select_mode(mode)
            expires_at = time.time() + int(grant.pop("expires_in"))
            state = {
                **_empty_state(),
                **grant,
                "phase": "authorization_pending",
                "mode": selected,
                "expires_at": expires_at,
                "attempt": int(state.get("attempt", 0)) + 1,
            }
            self._save(state)
            if open_browser and selected == "browser":
                try:
                    self.opener(str(state["verification_uri_complete"]))
                except (OSError, webbrowser.Error):
                    pass
            return self.status()

    def advance(self) -> dict[str, Any]:
        with self._mutex:
            state = self._load()
            if state.get("phase") != "authorization_pending":
                return self.status()
            if float(state.get("expires_at", 0)) <= time.time():
                self.store.delete("onboarding-device")
                state.update(phase="failed", error_class="authorization_expired")
                self._save(state)
                return self.status()
            device_code = self.store.get("onboarding-device")
            if not device_code:
                state.update(phase="repair_required", error_class="missing_device_credential")
                self._save(state)
                return self.status()
            response = self.api.poll(device_code)
            poll_status = response["status"]
            if poll_status in {"authorization_pending", "slow_down"}:
                if poll_status == "slow_down":
                    state["interval"] = min(60, int(state.get("interval", 5)) + 5)
                    self._save(state)
                return self.status()
            if poll_status in {"access_denied", "expired_token"}:
                self.store.delete("onboarding-device")
                state.update(
                    phase="declined" if poll_status == "access_denied" else "failed",
                    error_class=poll_status,
                )
                self._save(state)
                return self.status()
            token = response["access_token"]
            try:
                self.capability_check(token)
            except (SubstrateAPIError, OnboardingError):
                state.update(phase="failed", error_class="capability_check_failed")
                self._save(state)
                return self.status()
            self.store.put(token)
            self.store.delete("onboarding-device")
            state.update(phase="awaiting_history_consent", connected_at=time.time())
            state.pop("error_class", None)
            self._save(state)
            return self.status()

    def consent_history(self, approved: bool) -> dict[str, Any]:
        with self._mutex:
            state = self._load()
            if not self.store.get():
                raise OnboardingError("authentication_required")
            decision = "approved" if approved else "declined"
            state["history_consent"] = _receipt(decision)
            if not approved:
                state.update(phase="ready", completed_at=time.time())
                self._save(state)
                return self.status()
            # Consent must be durable before history discovery or worker launch.
            state.update(phase="import_starting")
            self._save(state)
            try:
                progress = self.import_start(self.home)
            except (OSError, RuntimeError, ValueError, SubstrateAPIError) as exc:
                state.update(phase="repair_required", error_class=type(exc).__name__)
                self._save(state)
                return self.status()
            state["import"] = progress
            job_id = progress.get("job_id")
            if isinstance(job_id, str):
                state["import_job_id"] = job_id
            state["phase"] = "ready" if progress.get("complete") else "importing"
            if state["phase"] == "ready":
                state["completed_at"] = time.time()
            self._save(state)
            return self.status()

    def run(
        self, *, mode: str = "auto", wait: bool = False, open_browser: bool = True,
        timeout: float = 900.0,
    ) -> dict[str, Any]:
        result = self.begin(mode=mode, open_browser=open_browser)
        if wait and result.get("phase") == "authorization_pending" and (
            mode == "device" or not open_browser
        ):
            print(
                f"Open {result.get('verification_uri')} and enter {result.get('user_code')}",
                file=sys.stderr,
                flush=True,
            )
        deadline = time.monotonic() + max(0.0, timeout)
        while wait and result.get("phase") == "authorization_pending" and time.monotonic() < deadline:
            interval = max(1, min(int(self._load().get("interval", 5)), 60))
            time.sleep(interval)
            result = self.advance()
        return result

    def repair(self, *, wait: bool = False) -> dict[str, Any]:
        with self._mutex:
            state = self._load()
            token = self.store.get()
            if token:
                try:
                    self.capability_check(token)
                except (SubstrateAPIError, OnboardingError):
                    self.store.delete()
                else:
                    if state.get("phase") == "importing":
                        from .supervisor import start_service
                        job_id = state.get("import_job_id")
                        if isinstance(job_id, str):
                            start_service(self.home, job_id)
                    return self.status()
            self.store.delete("onboarding-device")
            self._save(_empty_state())
        return self.run(mode="auto", wait=wait)


def _select_mode(mode: str) -> str:
    if mode in {"browser", "device"}:
        return mode
    if mode != "auto":
        raise ValueError("mode must be auto, browser, or device")
    graphical = (
        os.name == "nt" or sys.platform == "darwin"
        or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    )
    return "browser" if graphical else "device"


def _active_agent_id() -> str:
    return os.environ.get("HERMES_PROFILE") or os.environ.get("HERMES_AGENT_ID") or "default"


def _start_history_import(home: Path) -> dict[str, Any]:
    from .history import HermesHistoryImporter, select_history_source
    from .supervisor import start_service

    try:
        source = select_history_source(home)
        client = SubstrateClient.from_env(
            timeout=30.0, hermes_home=home, hosted_default=True
        )
        importer = HermesHistoryImporter(
            hermes_home=home, client=client, source=source, agent_id=_active_agent_id()
        )
        checkpoint = importer.prepare()
        status = checkpoint.status()
        checkpoint.close()
        if not status.get("complete"):
            status["import_service"] = start_service(home, str(status["job_id"]))
        return status
    except (FileNotFoundError, NotADirectoryError):
        return {
            "state": "complete", "complete": True, "discovered": 0, "eligible": 0,
            "delivered": 0, "skipped": 0, "quarantined": 0,
        }


def _prompt_history(manager: OnboardingManager) -> dict[str, Any]:
    status = manager.status()
    if status.get("phase") != "awaiting_history_consent":
        return status
    if not sys.stdin.isatty():
        return status
    print(
        "Upload all eligible past Hermes conversations to your Substrate memory? [y/N]: ",
        end="", file=sys.stderr, flush=True,
    )
    try:
        answer = sys.stdin.readline().strip().casefold()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    return manager.consent_history(answer in {"y", "yes"})


def bootstrap_package() -> None:
    parent = Path(__file__).resolve().parent.parent
    if str(parent) not in sys.path:
        sys.path.insert(0, str(parent))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--mode", choices=("auto", "browser", "device"), default="auto")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--history", choices=("ask", "approve", "decline"), default="ask")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    manager = OnboardingManager(args.hermes_home)
    try:
        if args.status:
            result = manager.status()
        elif args.repair:
            result = manager.repair(wait=args.wait)
        else:
            result = manager.run(
                mode=args.mode, wait=args.wait, open_browser=not args.no_browser
            )
        if result.get("phase") == "awaiting_history_consent":
            if args.history == "approve":
                result = manager.consent_history(True)
            elif args.history == "decline":
                result = manager.consent_history(False)
            else:
                result = _prompt_history(manager)
    except Exception as exc:  # noqa: BLE001 - never render credential-bearing details
        result = {"complete": False, "error_class": type(exc).__name__}
        code = 1
    else:
        code = 0
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return code


if __name__ == "__main__":
    bootstrap_package()
    raise SystemExit(main())
