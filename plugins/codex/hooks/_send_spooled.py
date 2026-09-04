#!/usr/bin/env python3
"""Detached spool sender for Substrate Codex capture (standard library only).

Delivers spooled envelopes to ``/api/v1/ledger/events`` with each
envelope's ``event_id`` as the ``Idempotency-Key``. Every invocation sends
the explicitly named files (if any) plus a bounded sweep of the oldest
spool files, so captures spooled while the server was unreachable are
retried on the next hook fire instead of being lost. Every failure is
swallowed (fail closed); files that fail stay queued, bounded by age
(``_MAX_SPOOL_AGE_SECONDS``) and by the per-run attempt cap
(``_MAX_PER_RUN``). Never prints secrets.

Spool semantics vs the Hermes in-process worker: Hermes holds up to 64
envelopes in memory and attempts each once with ``Idempotency-Key`` equal
to ``event_id``; this spool holds up to 64 files on disk (see
``substrate_hook._spool_envelope``) and attempts each file at least once,
plus redelivery on later hook fires. Remaining gap: there is no timed
background retry, so a capture spooled while the server is down waits for
a future hook fire (at most ``_MAX_SPOOL_AGE_SECONDS`` old); see
``README.md``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from substrate_core.client import ClientError, SubstrateClient  # noqa: E402
from substrate_core import onboarding  # noqa: E402

_MAX_SPOOL_AGE_SECONDS = 7 * 24 * 3600
_MAX_PER_RUN = 10


def _spool_dir() -> Path | None:
    try:
        home = onboarding.active_home()
    except Exception:
        return None
    return home / "substrate" / "spool-codex"


def _send_one(path: Path) -> None:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return
    event_id = envelope.get("event_id") if isinstance(envelope, dict) else None
    if not isinstance(event_id, str) or not event_id:
        try:
            path.unlink()
        except OSError:
            pass
        return
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return
    if age > _MAX_SPOOL_AGE_SECONDS:
        try:
            path.unlink()
        except OSError:
            pass
        return
    try:
        SubstrateClient.from_env().post_json(
            "/api/v1/ledger/events",
            envelope,
            timeout=5.0,
            idempotency_key=event_id,
            max_response_bytes=65_536,
        )
    except ClientError:
        return
    except Exception:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _sweep(spool: Path, extra: list[Path]) -> None:
    ordered: list[Path] = []
    seen: set[str] = set()
    for path in extra:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            ordered.append(path)
    try:
        candidates = sorted(
            (p for p in spool.glob("*.json") if p.is_file()),
            key=lambda p: (p.stat().st_mtime, p.name),
        )
    except OSError:
        candidates = []
    for path in candidates:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            ordered.append(path)
    for path in ordered[:_MAX_PER_RUN]:
        _send_one(path)


def main(argv: list[str]) -> int:
    spool = _spool_dir()
    if spool is None:
        return 0
    extra = [Path(arg) for arg in argv[1:] if arg]
    try:
        _sweep(spool, extra)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
