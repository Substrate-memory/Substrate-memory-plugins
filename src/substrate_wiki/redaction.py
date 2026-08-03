"""Best-effort credential redaction before session material reaches disk or network."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence, Set
from itertools import islice
from pathlib import Path
from typing import Any

_REDACTED = "[REDACTED]"
# Live capture hashes only the latest 512 messages and history replay redacts
# one streamed message/event at a time.  This ceiling still covers unusually
# wide bounded tool payloads without allowing a hook value to amplify into a
# gateway-sized second object graph.
_MAX_CAPTURE_ITEMS = 16_384
_MAX_ENVIRONMENT_ITEMS = 4_096
_MAX_CONFIGURED_SECRETS = 64
_MAX_CONFIGURED_SECRET_BYTES = 64 * 1024
_MAX_CONFIGURED_SECRET_CHARS = 16 * 1024
# The longest bounded fixed-pattern match is the orphan/private-key form: up to
# 128 lines of 4,096 base64 characters plus indentation and delimiters.  Keep a
# conservative overlap so a match cannot straddle released text.  The overlap
# is independent of total message size and stays comfortably below the import
# worker's 256 MiB RSS ceiling even for wide Unicode strings.
_STREAM_REDACTION_OVERLAP_CHARS = 640 * 1024
_STREAM_REDACTION_SCAN_CHARS = 256 * 1024
_STREAM_CONTINUATION_TERMINATORS = frozenset("\r\n\t ,;}]&#@'\"")
_PRIORITY_SECRET_KEYS = (
    "HERMES_API_KEY",
    "MINIMAX_API_KEY",
    "NVIDIA_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "MISTRAL_API_KEY",
    "COHERE_API_KEY",
)
_CREDENTIAL_LABEL = (
    r"(?:api[-_ ]?key|access[-_ ]?key|secret[-_ ]?key|client[-_ ]?secret|"
    r"access[-_ ]?token|refresh[-_ ]?token|id[-_ ]?token|"
    r"auth(?:entication|orization)?[-_ ]?token|bearer[-_ ]?token|"
    r"signing[-_ ]?key|password|passwd|passphrase|private[-_ ]?key|"
    r"connection[-_ ]?string|credential|secret)"
)
_GENERIC_CREDENTIAL_LABEL = r"(?:[A-Z][A-Z0-9_]{0,255}(?:KEY|TOKEN|SECRET|PASSWORD))"
_SIGNED_CREDENTIAL_LABEL = (
    r"(?:x[-_ ]?amz[-_ ]?(?:signature|credential|security[-_ ]?token)|"
    r"x[-_ ]?goog[-_ ]?(?:signature|credential)|google[-_ ]?access[-_ ]?id|"
    r"aws[-_ ]?access[-_ ]?key[-_ ]?id|signature)"
)
_SENSITIVE_KEYS = re.compile(
    rf"(?:^|[-_. ]){_CREDENTIAL_LABEL}(?:$|[-_. ])|"
    r"^(?:authorization|proxy-authorization|cookie|set-cookie|"
    rf"{_SIGNED_CREDENTIAL_LABEL})$",
    re.IGNORECASE,
)
_PRIVATE_KEY_KIND = r"(?:OPENSSH|ENCRYPTED|RSA|EC|DSA)?(?:[ -]+)?PRIVATE KEY"
_PRIVATE_KEY_BLOCK = re.compile(
    rf"-----BEGIN {_PRIVATE_KEY_KIND}-----"
    rf"[\s\S]{{0,262144}}?-----END {_PRIVATE_KEY_KIND}-----",
    re.IGNORECASE,
)
_PRIVATE_KEY_BEGIN = re.compile(
    rf"-----BEGIN {_PRIVATE_KEY_KIND}-----[\s\S]{{0,262144}}",
    re.IGNORECASE,
)
_PRIVATE_KEY_ORPHAN = re.compile(
    rf"(?m)(?:^[ \t]{{0,32}}(?:[A-Za-z0-9+/]{{16,4096}}={{0,2}}|"
    rf"(?:Proc-Type|DEK-Info):[^\r\n]{{1,4096}})[ \t]{{0,32}}\r?\n){{1,128}}"
    rf"^-----END {_PRIVATE_KEY_KIND}-----",
    re.IGNORECASE,
)
_PRIVATE_KEY_FOOTER = re.compile(
    rf"-----END {_PRIVATE_KEY_KIND}-----",
    re.IGNORECASE,
)
_PRIVATE_KEY_ADJACENT = re.compile(
    rf"(?i)\b{_PRIVATE_KEY_KIND}\b\s*(?:(?::|=|\bis\b)\s*)?"
    r"[A-Za-z0-9+/]{16,4096}={0,2}"
    r"(?:\r?\n[ \t]{0,32}[A-Za-z0-9+/]{16,4096}={0,2}){0,127}"
)
_SAS_SIGNATURE = re.compile(
    r"(?i)((?:[?&;]|&amp;|^)sig=)[^&#;\s]{1,262144}"
)
_SAS_COMPANION = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:sv|se|sp|sr|srt|ss|spr|st|skoid|sktid|skt|ske|sks|skv)="
    r"[^&#;\s]{1,4096}"
)
_EXPLICIT_ASSIGNMENT = re.compile(
    rf"(?i)(?<![A-Za-z0-9])['\"]?{_CREDENTIAL_LABEL}['\"]?"
    r"\s*(?::|=|\bis\b)\s*(?:\"[^\"\r\n]{1,4096}\"|"
    r"'[^'\r\n]{1,4096}'|[^\s,;}\]\r\n]{1,4096})"
)
_GENERIC_ASSIGNMENT = re.compile(
    rf"(?i)(?<![A-Za-z0-9])['\"]?{_GENERIC_CREDENTIAL_LABEL}['\"]?"
    r"\s*(?::|=)\s*(?:\"[^\"\r\n]{1,4096}\"|"
    r"'[^'\r\n]{1,4096}'|[^\s,;}\]\r\n]{1,4096})"
)
# BEGIN GENERATED SERVER PROVIDER DETECTORS
# Imported from Substrate-v2 at the 1.4.1 extraction boundary.
# These plugin-owned release bytes are now canonical; update with tests and a release.
_PROVIDER_TOKEN = re.compile('(?<![A-Za-z0-9])(?:sk-(?:live-|test-)?|sk_(?:live_|test_)|pk_(?:live_|test_)?|gh[opusr]_|github_pat_|xox[baprse]-|xoxe\\.|glpat-|hf_|npm_|pypi-|AIza|AKIA|ASIA|ya29\\.|xapp-|ntn_[A-Za-z0-9]{20,}|secret_[A-Za-z0-9]{32,}|lin_(?:api|oauth)_[A-Za-z0-9]{20,}|ak_[A-Za-z0-9]{20,}|uak_[A-Za-z0-9]{20,}|oak_[A-Za-z0-9]{20,}|(?<![/\\\\])1//(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{16,}|hooks\\.slack\\.com/(?:services|triggers|workflows)/[A-Za-z0-9/_-]{8,})[A-Za-z0-9_\\-.*\\u2022\\u2026]{4,}')
_PERCENT_ENCODED_PROVIDER_TOKEN = re.compile('(?i)(?<![A-Za-z0-9])(?:(?:y|%(?:25)*79)(?:a|%(?:25)*61)(?:2|%(?:25)*32)(?:9|%(?:25)*39)(?:\\.|%(?:25)*2e)|(?:A|%(?:25)*41)(?:K|%(?:25)*4b)(?:I|%(?:25)*49)(?:A|%(?:25)*41))[A-Za-z0-9_%\\-.*•…]{4,}')
_PROVIDER_TOKEN_HINTS = ('sk-', 'sk_', 'pk_', 'gho_', 'ghp_', 'ghu_', 'ghs_', 'ghr_', 'github_pat_', 'xox', 'xapp-', 'glpat-', 'hf_', 'npm_', 'pypi-', 'aiza', 'akia', 'asia', 'ya29.', 'ntn_', 'secret_', 'lin_api_', 'lin_oauth_', 'ak_', 'uak_', 'oak_', '1//', 'hooks.slack.com/')
# END GENERATED SERVER PROVIDER DETECTORS
_PATTERNS = (
    re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{4,}"),
    re.compile(
        r"(?i)\b(?:proxy-)?authorization\s*[:=]\s*"
        r"(?:AWS4-HMAC-SHA256|GOOG1|GOOG4-RSA-SHA256|SharedKey(?:Lite)?|Signature)"
        r"\s+[^\r\n]{1,16384}"
    ),
    _EXPLICIT_ASSIGNMENT,
    _GENERIC_ASSIGNMENT,
    re.compile(
        r"(?i)[a-z][a-z0-9+.-]{1,63}://"
        r"[^/@:\s]{0,256}:[^/@\s]{1,262144}@"
    ),
    re.compile(
        rf"(?i)([?&](?:{_CREDENTIAL_LABEL}|key|token)=)"
        r"[^&#\s]{1,4096}"
    ),
    re.compile(
        rf"(?i)((?:[?&]|&amp;)(?:{_SIGNED_CREDENTIAL_LABEL})=)[^&#\s]{{1,262144}}"
    ),
    re.compile(
        rf"(?i)['\"]?{_SIGNED_CREDENTIAL_LABEL}['\"]?\s*(?::|=|\bis\b)\s*"
        r"(?:[\"']?)[A-Za-z0-9%._~+/=-]{12,4096}(?:[\"']?)"
    ),
    _PROVIDER_TOKEN,
    _PERCENT_ENCODED_PROVIDER_TOKEN,
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    _PRIVATE_KEY_BLOCK,
    _PRIVATE_KEY_BEGIN,
    _PRIVATE_KEY_ORPHAN,
    _PRIVATE_KEY_FOOTER,
    _PRIVATE_KEY_ADJACENT,
)
_PATTERN_HINTS = (
    ("bearer", "basic"),
    ("authorization",),
    (
        "key",
        "token",
        "secret",
        "password",
        "passwd",
        "passphrase",
        "credential",
        "connection",
    ),
    ("key", "token", "secret", "password"),
    ("://",),
    ("?", "&"),
    ("?", "&"),
    ("signature", "credential", "access id", "token", "key"),
    _PROVIDER_TOKEN_HINTS,
    ("%",),
    ("eyj",),
    ("private key",),
    ("private key",),
    ("private key",),
    ("private key",),
    ("private key",),
)


def _candidate_patterns(value: str) -> Iterator[re.Pattern[str]]:
    """Skip regexes whose mandatory literal anchors are absent."""

    folded = value.casefold()
    for pattern, hints in zip(_PATTERNS, _PATTERN_HINTS, strict=True):
        if any(hint in folded for hint in hints):
            yield pattern


def configured_secret_values() -> tuple[str, ...]:
    """Return a bounded, deterministic set of environment-backed secrets.

    Provider credentials are admitted first.  An oversized or overpopulated
    sensitive environment fails closed instead of silently omitting a value
    that could subsequently reach the durable spool.
    """
    values: set[str] = set()
    total_bytes = 0

    def admit(value: str) -> None:
        nonlocal total_bytes
        if not value or len(value) < 6 or value in values:
            return
        if len(value) > _MAX_CONFIGURED_SECRET_CHARS:
            raise ValueError("configured secrets exceed redaction bounds")
        encoded_bytes = len(value.encode("utf-8"))
        if (
            len(values) >= _MAX_CONFIGURED_SECRETS
            or total_bytes + encoded_bytes > _MAX_CONFIGURED_SECRET_BYTES
        ):
            raise ValueError("configured secrets exceed redaction bounds")
        values.add(value)
        total_bytes += encoded_bytes

    priority = set(_PRIORITY_SECRET_KEYS)
    for key in _PRIORITY_SECRET_KEYS:
        admit(os.environ.get(key, ""))
    for index, (key, value) in enumerate(os.environ.items()):
        if index >= _MAX_ENVIRONMENT_ITEMS:
            raise ValueError("environment exceeds redaction scan bounds")
        if key not in priority and value and _SENSITIVE_KEYS.search(key):
            admit(value)
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def redact_text(value: str, secrets: Sequence[str] | None = None) -> str:
    redacted = value
    for secret in secrets if secrets is not None else configured_secret_values():
        redacted = redacted.replace(secret, _REDACTED)
    for pattern in _candidate_patterns(redacted):
        redacted = pattern.sub(_REDACTED, redacted)
    if _SAS_COMPANION.search(redacted):
        redacted = _SAS_SIGNATURE.sub(r"\1[REDACTED]", redacted)
    return redacted


def _redaction_spans(value: str, secrets: Sequence[str]) -> list[tuple[int, int]]:
    """Return merged raw-input spans covered by the shared credential rules.

    ``redact_text`` applies substitutions sequentially, which is ideal for a
    materialized hook value but does not retain a raw-to-output position map.
    Streaming needs that map so it can release a stable prefix without losing
    or duplicating adjacent safe text.  Matching every rule against the same
    raw bounded window is deliberately conservative: overlapping detections
    collapse to one tombstone instead of exposing either match.
    """

    spans: list[tuple[int, int]] = []
    for secret in secrets:
        if not secret:
            continue
        start = value.find(secret)
        while start >= 0:
            spans.append((start, start + len(secret)))
            start = value.find(secret, start + max(1, len(secret)))
    for pattern in _candidate_patterns(value):
        spans.extend((match.start(), match.end()) for match in pattern.finditer(value))
    if _SAS_COMPANION.search(value):
        spans.extend(
            (match.start(), match.end()) for match in _SAS_SIGNATURE.finditer(value)
        )
    if not spans:
        return []
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


class StreamingTextRedactor:
    """Bounded deterministic redaction for a repeatable streamed text source.

    The class retains only a fixed overlap large enough for every configured
    secret and bounded shared rule.  A lexical continuation state handles the
    few shared token rules with intentionally open-ended quantifiers.  No raw
    match is released, including when its opener/value is split at any source
    chunk boundary.
    """

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        self._secrets = tuple(secrets)
        self._buffer = ""
        self._suppress_continuation = False

    @property
    def buffered_chars(self) -> int:
        """Content-free test/diagnostic measure of retained normalization state."""

        return len(self._buffer)

    def _strip_continuation(self, value: str) -> str:
        if not self._suppress_continuation:
            return value
        for index, character in enumerate(value):
            if character.isspace() or character in _STREAM_CONTINUATION_TERMINATORS:
                self._suppress_continuation = False
                return value[index:]
        return ""

    def _release(self, cutoff: int) -> str:
        spans = _redaction_spans(self._buffer, self._secrets)
        crossing = [
            (start, end) for start, end in spans if start < cutoff and end > cutoff
        ]
        if crossing:
            safe_cutoff = min(start for start, _end in crossing)
            # Bounded rules cannot span the conservative overlap.  A match
            # which nevertheless crosses it and reaches the buffer boundary
            # is one of the open-ended bearer/provider token forms.  Emit its
            # single tombstone now and suppress lexical continuation so an
            # arbitrarily large token cannot grow the retained buffer.
            if any(end == len(self._buffer) for _start, end in crossing):
                rendered = redact_text(
                    self._buffer[:safe_cutoff], self._secrets
                ) + _REDACTED
                self._buffer = ""
                self._suppress_continuation = True
                return rendered
            cutoff = safe_cutoff
        rendered = redact_text(self._buffer[:cutoff], self._secrets)
        self._buffer = self._buffer[cutoff:]
        return rendered

    def feed(self, value: str) -> Iterator[str]:
        """Accept text and yield only prefixes safe against future input."""

        if not isinstance(value, str):
            raise TypeError("streamed message chunks must be strings")
        offset = 0
        while offset < len(value):
            end = min(len(value), offset + _STREAM_REDACTION_SCAN_CHARS)
            piece = self._strip_continuation(value[offset:end])
            offset = end
            if piece:
                self._buffer += piece
            if (
                len(self._buffer)
                > _STREAM_REDACTION_OVERLAP_CHARS + _STREAM_REDACTION_SCAN_CHARS
            ):
                cutoff = len(self._buffer) - _STREAM_REDACTION_OVERLAP_CHARS
                rendered = self._release(cutoff)
                if rendered:
                    yield rendered

    def finish(self) -> Iterator[str]:
        """Redact and release the final bounded suffix."""

        if self._suppress_continuation:
            self._suppress_continuation = False
        if not self._buffer:
            return
        rendered = self._release(len(self._buffer))
        if rendered:
            yield rendered


def iter_redacted_text_chunks(
    chunks: Iterable[str],
    secrets: Sequence[str] = (),
) -> Iterator[str]:
    """Yield deterministic redacted text without materializing the source."""

    redactor = StreamingTextRedactor(secrets)
    for chunk in chunks:
        yield from redactor.feed(chunk)
    yield from redactor.finish()


def redact(value: Any, secrets: Sequence[str] | None = None) -> Any:
    """Return a JSON-safe redacted copy without invoking arbitrary object reprs."""
    secret_values = tuple(secrets) if secrets is not None else configured_secret_values()
    return _redact(value, secret_values, seen=set(), depth=0)


def _redact(value: Any, secrets: Sequence[str], *, seen: set[int], depth: int) -> Any:
    if depth > 32:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "[NONFINITE]"
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, Path):
        return redact_text(os.fspath(value), secrets)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[BINARY]"
    identity = id(value)
    if identity in seen:
        return "[CYCLE]"
    if isinstance(value, Mapping):
        seen.add(identity)
        try:
            result: dict[str, Any] = {}
            for key, child in islice(value.items(), _MAX_CAPTURE_ITEMS):
                raw_key = key if isinstance(key, str) else f"[{type(key).__name__}]"
                text_key = redact_text(raw_key, secrets)
                if _SENSITIVE_KEYS.search(raw_key):
                    text_key = _REDACTED
                unique_key = text_key
                suffix = 2
                while unique_key in result:
                    unique_key = f"{text_key}#{suffix}"
                    suffix += 1
                result[unique_key] = (
                    _REDACTED
                    if _SENSITIVE_KEYS.search(raw_key)
                    else _redact(child, secrets, seen=seen, depth=depth + 1)
                )
            return result
        finally:
            seen.discard(identity)
    if isinstance(value, Set) and not isinstance(value, (str, bytes, bytearray)):
        seen.add(identity)
        try:
            sanitized = [
                _redact(child, secrets, seen=seen, depth=depth + 1)
                for child in islice(iter(value), _MAX_CAPTURE_ITEMS)
            ]
            return sorted(sanitized, key=lambda child: (type(child).__name__, str(child)[:256]))
        finally:
            seen.discard(identity)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        seen.add(identity)
        try:
            return [
                _redact(child, secrets, seen=seen, depth=depth + 1)
                for child in islice(iter(value), _MAX_CAPTURE_ITEMS)
            ]
        finally:
            seen.discard(identity)
    return f"[UNSUPPORTED:{type(value).__name__}]"
