"""Regression tests for the multi-host home-resolution defect.

Live defect: the Codex MCP server, run from a repo checkout with no
SUBSTRATE_HOME/CODEX_HOME set, wrote onboarding state into the checkout at
``plugins/substrate/onboarding.json`` (plus a device credential) instead of
``~/.codex/substrate/``. The vendored ``onboarding.py`` used the
Hermes-derived ``active_home()`` (HERMES_HOME plus the
``<home>/plugins/substrate`` installed-layout walk), so a checkout path
resolved as a home.

These tests fail on the old behavior for every host:

- with no env overrides and CWD inside the checkout, the resolved home must
  equal the host default home (``Path.home()`` patched to a temp dir) and
  must never be inside the repo;
- onboarding state and credential writes land under ``<host home>/substrate/``;
- no vendored host copy consults HERMES_HOME, a ``plugins/substrate`` walk,
  or a ``~/.hermes`` fallback, and no path under ``plugins/substrate/`` is
  ever written.

Offline, stdlib + pytest only.
"""

from __future__ import annotations

import pathlib
from pathlib import Path

import pytest

import _hostload  # noqa: E402

REPO = _hostload.REPO
SUBSTRATE_DIR = REPO / "plugins" / "substrate"

_ENV_VARS = (
    "SUBSTRATE_HOME",
    "CODEX_HOME",
    "GROK_HOME",
    "CLAUDE_HOME",
    "CLAUDE_CONFIG_DIR",
    "OPENCLAW_STATE_DIR",
    "HERMES_HOME",
    "SUBSTRATE_API_KEY",
)


def _load(host: str):
    plugin_dir = REPO / "plugins" / host
    loader = _hostload.begin(host, [plugin_dir, REPO / "plugins"])
    hosthome = loader.core("hosthome")
    client = loader.core("client")
    onboarding = loader.core("onboarding")
    loader.commit()
    return hosthome, client, onboarding


_cc_hosthome, _cc_client, _cc_onboarding = _load("claude-code")
_cw_hosthome, _cw_client, _cw_onboarding = _load("claude-cowork")
_cx_hosthome, _cx_client, _cx_onboarding = _load("codex")
_gk_hosthome, _gk_client, _gk_onboarding = _load("grok-bot")
_oc_hosthome, _oc_client, _oc_onboarding = _load("openclaw")

_HOSTS = (
    ("claude-code", "host_home", ".claude",
     _cc_hosthome, _cc_client, _cc_onboarding),
    ("claude-cowork", "active_home", ".claude",
     _cw_hosthome, _cw_client, _cw_onboarding),
    ("codex", "codex_home", ".codex",
     _cx_hosthome, _cx_client, _cx_onboarding),
    ("grok-bot", "grok_home", ".grok",
     _gk_hosthome, _gk_client, _gk_onboarding),
    ("openclaw", "openclaw_home", ".openclaw",
     _oc_hosthome, _oc_client, _oc_onboarding),
)


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _patched_fake_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Path:
    fake = tmp_path / "fake-home"
    fake.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pathlib.Path, "home", lambda: fake)
    return fake


def _assert_outside_repo(path: Path) -> None:
    repo = REPO.resolve()
    resolved = path.resolve()
    assert resolved != repo
    assert repo not in resolved.parents, resolved
    assert not str(resolved).startswith(str(repo) + "/")


@pytest.mark.parametrize(
    "slug,func,default,hosthome,client,onboarding",
    list(_HOSTS),
    ids=[h[0] for h in _HOSTS],
)
def test_default_home_is_host_home_not_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    slug: str,
    func: str,
    default: str,
    hosthome,
    client,
    onboarding,
) -> None:
    """No env + CWD in checkout -> host default home, never the repo."""
    _clean_env(monkeypatch)
    fake = _patched_fake_home(monkeypatch, tmp_path)
    monkeypatch.chdir(REPO)
    expected = (fake / default).resolve()
    assert getattr(hosthome, func)().resolve() == expected
    homes = client._profile_homes()
    assert homes, slug
    assert homes[0].resolve() == expected
    assert onboarding.active_home().resolve() == expected
    for candidate in (getattr(hosthome, func)().resolve(),
                      homes[0].resolve(),
                      onboarding.active_home().resolve()):
        _assert_outside_repo(candidate)


@pytest.mark.parametrize(
    "slug,func,default,hosthome,client,onboarding",
    list(_HOSTS),
    ids=[h[0] for h in _HOSTS],
)
def test_state_and_credential_writes_stay_under_host_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    slug: str,
    func: str,
    default: str,
    hosthome,
    client,
    onboarding,
) -> None:
    """State/credential writes via the resolved home land under it."""
    _clean_env(monkeypatch)
    fake = _patched_fake_home(monkeypatch, tmp_path)
    monkeypatch.chdir(REPO / "plugins" / "codex")
    home = onboarding.active_home().resolve()
    assert home == (fake / default).resolve()
    _assert_outside_repo(home)
    onboarding._save_state(home, {"phase": "new"})
    state_path = home / "substrate" / "onboarding.json"
    assert state_path.is_file()
    token = "regression-token-" + slug
    assert len(token) < 64
    onboarding._store_token_file(home, token)
    credential = home / "substrate" / "credentials" / "access-token"
    assert credential.read_text(encoding="utf-8").strip() == token
    onboarding.write_env_key(home, token)
    assert (home / ".env").is_file()
    onboarding._save_origin(home, "http://127.0.0.1:1/")
    assert (home / "substrate" / "config.json").is_file()
    assert not (SUBSTRATE_DIR / "onboarding.json").exists()
    assert not (SUBSTRATE_DIR / "credentials" / "onboarding-device").exists()


def _code_lines(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    code = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        code.append(line.split("#", 1)[0])
    return code


def test_no_hermes_home_resolution_in_vendored_copies() -> None:
    """No HERMES_HOME lookup, Hermes walk, or ~/.hermes fallback in code."""
    for slug, _, _, _, _, _ in _HOSTS:
        for name in ("client.py", "onboarding.py", "hosthome.py"):
            path = REPO / "plugins" / slug / "substrate_core" / name
            if not path.is_file():
                continue
            for line in _code_lines(path):
                assert "HERMES_HOME" not in line, f"{slug}/{name}: {line}"
                if ".hermes" in line:
                    assert "co.trysubstrate.hermes" in line \
                        or "HERMES_PROFILE" in line, f"{slug}/{name}: {line}"
        for name in ("client.py", "onboarding.py", "hosthome.py"):
            path = REPO / "plugins" / slug / "substrate_core" / name
            if not path.is_file():
                continue
            for line in _code_lines(path):
                assert 'parent.name == "substrate"' not in line, \
                    f"{slug}/{name}: {line}"
                assert "plugins/substrate" not in line, f"{slug}/{name}: {line}"
        onboarding_path = REPO / "plugins" / slug / "substrate_core" / "onboarding.py"
        for line in _code_lines(onboarding_path):
            assert "_profile_homes" not in line, f"{slug}/onboarding.py: {line}"
    for setup in (
        REPO / "plugins" / "codex" / "setup.py",
        REPO / "plugins" / "grok-bot" / "setup.py",
    ):
        for line in _code_lines(setup):
            assert "_profile_homes" not in line, f"{setup}: {line}"


def test_no_paths_under_plugins_substrate_exist() -> None:
    """Guard: the frozen Hermes dir never gains onboarding/device state."""
    assert not (SUBSTRATE_DIR / "onboarding.json").exists()
    assert not (SUBSTRATE_DIR / "credentials" / "onboarding-device").exists()


def test_onboarding_messages_use_host_names() -> None:
    """Codex/Grok messages name their host; no Hermes profile remains."""
    codex_runtime = (REPO / "plugins" / "codex" / "substrate_core" / "runtime.py")
    grok_runtime = (REPO / "plugins" / "grok-bot" / "substrate_core" / "runtime.py")
    codex_text = codex_runtime.read_text(encoding="utf-8")
    grok_text = grok_runtime.read_text(encoding="utf-8")
    assert "this Hermes profile" not in codex_text
    assert "this Hermes profile" not in grok_text
    assert "this Codex profile" in codex_text
    assert "this Grok home" in grok_text
    cowork_text = (
        REPO / "plugins" / "claude-cowork" / "substrate_core" / "runtime.py"
    ).read_text(encoding="utf-8")
    openclaw_text = (
        REPO / "plugins" / "openclaw" / "substrate_core" / "runtime.py"
    ).read_text(encoding="utf-8")
    assert "this Cowork home" in cowork_text
    assert "this OpenClaw home" in openclaw_text
