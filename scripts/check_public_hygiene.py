"""Public-hygiene check for the Substrate plugin repository.

Fails closed on real-secret shapes (private keys, credential-like tokens,
bearer assignments) and on production endpoints outside the explicit
allowlist. Findings name only the path and class, never the matched value.
Standard library only.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

SECRET_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("credential", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("credential", re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}\b")),
    ("credential", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    (
        "credential",
        re.compile(r"Authorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/-]{20,}", re.IGNORECASE),
    ),
    (
        "credential",
        re.compile(r"(?<![A-Za-z0-9_])(?:SUBSTRATE_API_KEY|OPENAI_API_KEY)\s*=\s*[\"']?[A-Za-z0-9._~+/-]{20,}"),
    ),
)

URL_PATTERN = re.compile(r"https?://[^\s'\"<> ()]+", re.IGNORECASE)

# Hosts the repository may legitimately reference: the configured Substrate
# origins, the public code host, the Python package registry, the DCO
# reference, loopback test fixtures, and RFC 2606 documentation domains.
ALLOWED_HOSTS = frozenset({
    "github.com",
    "vm-substrate-ar-01.taile961d2.ts.net",
    "app.trysubstrate.co",
    "pypi.org",
    "files.pythonhosted.org",
    "developercertificate.org",
    "127.0.0.1",
    "localhost",
})

ALLOWED_SUFFIXES = (".example",)


def host_allowed(host: str) -> bool:
    host = host.strip().strip("`.,;:!?)]}").lower()
    if not host or "." not in host:
        return True
    if host in ALLOWED_HOSTS:
        return True
    return host.endswith(ALLOWED_SUFFIXES)

SKIP_DIRS = frozenset({".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "htmlcov"})
SKIP_SUFFIXES = (".pyc", ".pyo")


def iter_files(root: Path):
    for directory, dirnames, filenames in __import__("os").walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(SKIP_SUFFIXES):
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                yield path, "unsafe-or-missing"
                continue
            yield path, None


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for path, early in iter_files(root):
        relative = path.relative_to(root).as_posix()
        if early:
            findings.append(f"{relative}: {early}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for _class, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(f"{relative}: {_class}")
                break
        else:
            for match in URL_PATTERN.finditer(text):
                host = match.group(0).split("://", 1)[1].split("/", 1)[0].split("@")[-1].split(":")[0]
                if not host_allowed(host):
                    findings.append(f"{relative}: endpoint")
                    break
    return sorted(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    findings = scan(args.root.resolve())
    if findings:
        print({"status": "fail", "findings": findings})
        return 1
    print({"status": "pass", "findings": []})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
