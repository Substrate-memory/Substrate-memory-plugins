"""Hermes once-a-day update check: cached, silent on failure, one line a day. No network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "plugins" / "substrate-hermes" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from substrate import update_check  # noqa: E402
from substrate.client import PLUGIN_VERSION  # noqa: E402


@pytest.fixture(autouse=True)
def _network_off(monkeypatch):
    monkeypatch.delenv("SUBSTRATE_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update_check, "_fetch", lambda: "")


def _newer() -> str:
    major, minor, patch = (int(p) for p in PLUGIN_VERSION.split("."))
    return "%d.%d.%d" % (major, minor, patch + 1)


def test_fetch_failure_leaves_no_notice(tmp_path) -> None:
    update_check.refresh(tmp_path, now=1_000_000.0)
    assert update_check.notice(tmp_path, now=1_000_000.0) == ""


def test_newer_version_is_shown_once_a_day(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(update_check, "_fetch", lambda: _newer())
    update_check.refresh(tmp_path, now=1_000_000.0)
    line = update_check.notice(tmp_path, now=1_000_000.0)
    assert _newer() in line and "hermes plugins update substrate" in line
    assert update_check.notice(tmp_path, now=1_000_100.0) == ""
    assert update_check.notice(tmp_path, now=1_000_000.0 + 86_401) != ""


def test_refresh_respects_the_daily_interval(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(update_check, "_fetch", lambda: calls.append(1) or _newer())
    update_check.refresh(tmp_path, now=1_000_000.0)
    update_check.refresh(tmp_path, now=1_000_000.0 + 3_600)
    assert len(calls) == 1
    update_check.refresh(tmp_path, now=1_000_000.0 + 86_401)
    assert len(calls) == 2


def test_same_or_older_version_is_silent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(update_check, "_fetch", lambda: PLUGIN_VERSION)
    update_check.refresh(tmp_path, now=1.0)
    assert update_check.notice(tmp_path, now=1.0) == ""


def test_opt_out_never_fetches(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SUBSTRATE_UPDATE_CHECK", "0")
    monkeypatch.setattr(update_check, "_fetch", lambda: pytest.fail("network used"))
    update_check.refresh(tmp_path, now=1.0)
    assert update_check.notice(tmp_path, now=1.0) == ""


def test_corrupt_state_never_raises(tmp_path) -> None:
    (tmp_path / update_check.STATE_NAME).write_text("[1, 2")
    update_check.refresh(tmp_path, now=1.0)
    assert update_check.notice(tmp_path, now=1.0) == ""
