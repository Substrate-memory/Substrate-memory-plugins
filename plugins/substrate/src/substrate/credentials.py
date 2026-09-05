"""Private credential custody for the Substrate Hermes plugin.

Resolution order (first hit wins; environment overrides are for advanced
setups only and are never written):

1. ``SUBSTRATE_API_KEY`` / ``SUBSTRATE_API_URL`` from the environment.
2. The active profile's owner-private token file
   ``<home>/substrate/credentials/access-token`` (mode 0600).
3. The active profile's ``<home>/.env`` ``SUBSTRATE_API_KEY`` line
   (Hermes update semantics; preserved on write).
4. The legacy ``<home>/substrate_wiki/credentials/access-token`` file,
   for migration from the previous provider.
5. The freedesktop Secret Service ``co.trysubstrate.hermes`` entry
   (read-only, when ``secret-tool`` is available).

Only the active profile home is ever inspected: ``HERMES_HOME`` when set,
else the install parent (``<home>/plugins/substrate`` layout, flat or
``src/``), else ``~/.hermes``. No other profile is touched.

Standard library only. Secrets never reach logs, state files, or status
output; every helper here returns booleans or redacted summaries.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

ENV_KEY = "SUBSTRATE_API_KEY"
ENV_URL = "SUBSTRATE_API_URL"
LEGACY_ENV_URL = "SUBSTRATE_WIKI_ORIGIN"
DEFAULT_ORIGIN = "https://app.trysubstrate.co"

_MAX_TOKEN_BYTES = 16_384
_MAX_FILE_BYTES = 16_384

_ENV_LINE_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?SUBSTRATE_API_KEY[ \t]*=.*$", re.MULTILINE
)


class CredentialError(RuntimeError):
    """A bounded local error; never carries secret material."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _install_parent_home() -> Path | None:
    """Detect HERMES_HOME from an installed plugin layout, if present."""
    try:
        location = Path(__file__).resolve()
    except OSError:
        return None
    parts = location.parts
    # Flat layout: <home>/plugins/substrate/credentials.py (public repo).
    # src layout:  <home>/plugins/substrate/src/substrate/credentials.py.
    if len(parts) >= 4 and parts[-2] == "substrate" and parts[-3] == "plugins":
        return Path(*parts[:-3])
    if (
        len(parts) >= 6
        and parts[-2] == "substrate"
        and parts[-3] == "substrate"
        and parts[-4] == "src"
        and parts[-6] == "plugins"
    ):
        return Path(*parts[:-5])
    return None


def profile_homes() -> list[Path]:
    """Return the one active profile home; no other profile is inspected."""
    values: list[Path] = []
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        values.append(Path(configured).expanduser())
    detected = _install_parent_home()
    if detected is not None:
        values.append(detected)
    values.append(Path.home() / ".hermes")
    # Only the first known home is active; fallbacks are never inspected
    # once an active/configured profile is known.
    for value in values[:1]:
        try:
            return [value.resolve()]
        except OSError:
            continue
    return []


def active_home() -> Path:
    homes = profile_homes()
    return homes[0] if homes else Path.home() / ".hermes"


def _is_private_file(path: Path, maximum: int) -> bool:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return False
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        return False
    if info.st_size <= 0 or info.st_size > maximum:
        return False
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        try:
            if info.st_uid != getuid():
                return False
        except OSError:
            return False
        if stat.S_IMODE(info.st_mode) & 0o077:
            return False
    return True


def _read_private_file(path: Path, maximum: int = _MAX_TOKEN_BYTES) -> str:
    if not _is_private_file(path, maximum):
        return ""
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    value = value.strip()
    return value if 0 < len(value) <= maximum else ""


def _read_env_file_key(home: Path) -> str:
    try:
        raw = (home / ".env").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    for line in raw.splitlines():
        text = line.strip()
        if text.startswith("export "):
            text = text[len("export ") :].strip()
        if not text.startswith(f"{ENV_KEY}="):
            continue
        value = text[len(ENV_KEY) + 1 :].strip().strip("'\"").strip()
        if 0 < len(value) <= _MAX_TOKEN_BYTES:
            return value
        return ""
    return ""


def _secret_service_token(home: Path) -> str:
    """Read-only legacy lookup; the secret never touches argv or logs."""
    executable = shutil.which("secret-tool")
    if not executable:
        return ""
    try:
        account = (
            hashlib.sha256(os.fsencode(str(home.resolve()))).hexdigest()[:24]
            + ":profile"
        )
    except OSError:
        return ""
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


def stored_api_key(home: Path | None = None) -> str:
    """Best stored key for the active profile, or ``""`` when absent."""
    root = (home or active_home()).resolve() if not isinstance(home, Path) else home
    for relative in (
        ("substrate", "credentials", "access-token"),
        ("substrate_wiki", "credentials", "access-token"),
    ):
        value = _read_private_file(root.joinpath(*relative))
        if value:
            return value
    value = _read_env_file_key(root)
    if value:
        return value
    return _secret_service_token(root)


def api_key() -> str:
    """Environment override first, then the active profile store."""
    value = os.environ.get(ENV_KEY, "").strip()
    if value:
        return value
    return stored_api_key()


def stored_origin(home: Path | None = None) -> str:
    root = (home or active_home()).resolve() if not isinstance(home, Path) else home
    path = root / "substrate" / "config.json"
    if not _is_private_file(path, _MAX_FILE_BYTES):
        # Config carries no secret but a symlink must still not be followed.
        try:
            if path.is_symlink():
                return ""
        except OSError:
            return ""
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ""
        if len(raw) > _MAX_FILE_BYTES:
            return ""
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return ""
        origin = value.get("api_url") if isinstance(value, dict) else None
        return origin if isinstance(origin, str) and origin else ""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return ""
    origin = value.get("api_url") if isinstance(value, dict) else None
    return origin if isinstance(origin, str) and origin else ""


def api_origin() -> str:
    return (
        os.environ.get(ENV_URL, "").strip()
        or stored_origin()
        or os.environ.get(LEGACY_ENV_URL, "").strip()
        or DEFAULT_ORIGIN
    ).rstrip("/")


def _atomic_private_write(path: Path, text: str, mode: int = 0o600) -> None:
    if path.exists() and path.is_symlink():
        raise CredentialError("refusing_symlink")
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


def _write_env_key(home: Path, token: str) -> None:
    """Mirror the key into the profile ``.env`` (Hermes update semantics)."""
    env_path = home / ".env"
    if env_path.is_symlink():
        raise CredentialError("refusing_symlink")
    try:
        raw = env_path.read_text(encoding="utf-8-sig", errors="replace")
    except FileNotFoundError:
        raw = ""
    except OSError as exc:
        raise CredentialError("credential_store_failed") from exc
    if _ENV_LINE_RE.search(raw):
        text = _ENV_LINE_RE.sub(lambda _match: f"{ENV_KEY}={token}", raw)
    else:
        text = raw if (raw == "" or raw.endswith("\n")) else raw + "\n"
        text += f"{ENV_KEY}={token}\n"
    _atomic_private_write(env_path, text, mode=0o600)


def store_token(home: Path, origin: str, token: str) -> None:
    """Persist a freshly issued token privately; env overrides are untouched."""
    if not token or len(token) > _MAX_TOKEN_BYTES:
        raise CredentialError("invalid_response")
    root = home / "substrate"
    if root.exists() and root.is_symlink():
        raise CredentialError("refusing_symlink")
    _atomic_private_write(root / "credentials" / "access-token", token, mode=0o600)
    _write_env_key(home, token)
    _atomic_private_write(
        root / "config.json", json.dumps({"api_url": origin}, sort_keys=True) + "\n"
    )


def save_origin(home: Path, origin: str) -> None:
    root = home / "substrate"
    if root.exists() and root.is_symlink():
        raise CredentialError("refusing_symlink")
    _atomic_private_write(
        root / "config.json", json.dumps({"api_url": origin}, sort_keys=True) + "\n"
    )


def clear_stored_token(home: Path) -> bool:
    """Delete profile-stored credentials after a 401/403 (reconnect).

    Removes the token file and the ``.env`` key line so a stale secret is
    never reused; the environment override (advanced setups) is never
    touched. Spooled events are unrelated to this store and stay spooled.
    Returns True when anything was removed.
    """
    removed = False
    for relative in (
        ("substrate", "credentials", "access-token"),
        ("substrate", "credentials", "onboarding-device"),
    ):
        try:
            path = home.joinpath(*relative)
            if path.is_symlink():
                continue
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
        except OSError:
            continue
    env_path = home / ".env"
    try:
        if not env_path.is_symlink() and env_path.is_file():
            raw = env_path.read_text(encoding="utf-8-sig", errors="replace")
            if _ENV_LINE_RE.search(raw):
                _atomic_private_write(
                    env_path, _ENV_LINE_RE.sub(lambda _m: "", raw), mode=0o600
                )
                removed = True
    except OSError:
        pass
    return removed
