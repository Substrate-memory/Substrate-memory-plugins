"""Profile-scoped credential custody for hosted Substrate onboarding.

The public API never returns credential values.  Native credential helpers are
preferred; an owner-private file is the deliberately small portability fallback.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

_SERVICE = "co.trysubstrate.hermes"


def _profile_account(home: Path, slot: str) -> str:
    digest = hashlib.sha256(os.fsencode(str(home.resolve()))).hexdigest()[:24]
    return f"{digest}:{slot}"


class CredentialStore:
    """Abstract secret slot storage."""
    backend = "unknown"

    def get(self, slot: str = "access-token") -> str:
        raise NotImplementedError

    def put(self, value: str, slot: str = "access-token") -> None:
        raise NotImplementedError

    def delete(self, slot: str = "access-token") -> None:
        raise NotImplementedError


class SecretToolStore(CredentialStore):
    backend = "secret-service"

    def __init__(self, home: Path) -> None:
        self.account = _profile_account(home, "profile")

    def get(self, slot: str = "access-token") -> str:
        result = subprocess.run(
            ("secret-tool", "lookup", "service", _SERVICE, "account", self.account, "slot", slot),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False,
        )
        return result.stdout.rstrip("\n") if result.returncode == 0 else ""

    def put(self, value: str, slot: str = "access-token") -> None:
        if not value or len(value) > 16384:
            raise ValueError("invalid credential")
        subprocess.run(
            ("secret-tool", "store", "--label", "Substrate for Hermes", "service", _SERVICE,
             "account", self.account, "slot", slot), input=value, text=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=True,
        )

    def delete(self, slot: str = "access-token") -> None:
        subprocess.run(
            ("secret-tool", "clear", "service", _SERVICE, "account", self.account, "slot", slot),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )


class MacOSKeychainStore(CredentialStore):
    backend = "macos-keychain"

    def __init__(self, home: Path) -> None:
        self.account = _profile_account(home, "profile")

    def get(self, slot: str = "access-token") -> str:
        result = subprocess.run(
            ("security", "find-generic-password", "-a", self.account, "-s", f"{_SERVICE}.{slot}", "-w"),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False,
        )
        return result.stdout.rstrip("\n") if result.returncode == 0 else ""

    def put(self, value: str, slot: str = "access-token") -> None:
        if not value or len(value) > 16384:
            raise ValueError("invalid credential")
        # Apple's security tool has no stdin form.  Avoid it when process
        # inspection is not private by falling back to the protected file.
        raise OSError("non-interactive keychain write unavailable")

    def delete(self, slot: str = "access-token") -> None:
        subprocess.run(
            ("security", "delete-generic-password", "-a", self.account, "-s", f"{_SERVICE}.{slot}"),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )


class PrivateFileStore(CredentialStore):
    backend = "owner-private-file"

    def __init__(self, home: Path) -> None:
        root = home / "substrate_wiki" / "credentials"
        if root.exists() and root.is_symlink():
            raise OSError("credential directory must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(root, 0o700)
        elif os.name == "nt":
            icacls, username = shutil.which("icacls"), os.environ.get("USERNAME", "")
            if not icacls or not username:
                raise OSError("private Windows credential ACL unavailable")
            subprocess.run(
                (icacls, str(root), "/inheritance:r", "/grant:r", f"{username}:F"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=True,
            )
        self.root = root

    def _path(self, slot: str) -> Path:
        if not slot or not slot.replace("-", "").isalnum():
            raise ValueError("invalid credential slot")
        return self.root / slot

    def get(self, slot: str = "access-token") -> str:
        path = self._path(slot)
        if path.is_symlink():
            return ""
        try:
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                return ""
            if os.name == "posix":
                getuid = getattr(os, "getuid", None)
                if not callable(getuid) or info.st_uid != getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    return ""
            value = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeError):
            return ""
        return value if 0 < len(value) <= 16384 else ""

    def put(self, value: str, slot: str = "access-token") -> None:
        if not value or len(value) > 16384 or "\x00" in value:
            raise ValueError("invalid credential")
        path = self._path(slot)
        if path.is_symlink():
            raise OSError("credential path must not be a symlink")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name == "posix":
                os.chmod(temporary, 0o600)
            elif os.name == "nt":
                icacls, username = shutil.which("icacls"), os.environ.get("USERNAME", "")
                if not icacls or not username:
                    raise OSError("private Windows credential ACL unavailable")
                subprocess.run(
                    (icacls, str(temporary), "/inheritance:r", "/grant:r", f"{username}:F"),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=True,
                )
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def delete(self, slot: str = "access-token") -> None:
        path = self._path(slot)
        if path.is_symlink():
            raise OSError("credential path must not be a symlink")
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class PreferredCredentialStore(CredentialStore):
    """Use a functioning native vault, otherwise the explicit private fallback."""
    def __init__(self, home: Path) -> None:
        self.fallback = PrivateFileStore(home)
        self.native: CredentialStore | None = None
        self._active_backend = self.fallback.backend
        if shutil.which("secret-tool"):
            self.native = SecretToolStore(home)
        elif sys_platform() == "darwin" and shutil.which("security"):
            self.native = MacOSKeychainStore(home)

    @property
    def backend(self) -> str:
        return self._active_backend

    def get(self, slot: str = "access-token") -> str:
        if self.native is not None:
            try:
                value = self.native.get(slot)
                if value:
                    self._active_backend = self.native.backend
                    return value
            except (OSError, subprocess.SubprocessError):
                pass
        return self.fallback.get(slot)

    def put(self, value: str, slot: str = "access-token") -> None:
        if self.native is not None:
            try:
                self.native.put(value, slot)
                self.fallback.delete(slot)
                self._active_backend = self.native.backend
                return
            except (OSError, subprocess.SubprocessError):
                pass
        self.fallback.put(value, slot)
        self._active_backend = self.fallback.backend

    def delete(self, slot: str = "access-token") -> None:
        if self.native is not None:
            try:
                self.native.delete(slot)
            except (OSError, subprocess.SubprocessError):
                pass
        self.fallback.delete(slot)


def sys_platform() -> str:
    import sys
    return sys.platform


def credential_store(home: Path) -> CredentialStore:
    return PreferredCredentialStore(home.resolve())
