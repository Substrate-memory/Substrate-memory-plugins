"""Deterministic release packaging tests."""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
from build_release import (  # noqa: E402
    ARCHIVE_NAME, FIXED_TIMESTAMP, PLUGINS, build_archive_bytes, build_archive_bytes_for,
)

def test_root_readme_has_exact_install_contract() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text()
    assert "Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins" in readme
    assert "plugins/substrate-hermes" in readme and "plugins/substrate-mcp" in readme
    assert "Cowork tab → Customize → Plugins → Personal plugins" in readme
    assert "authenticated memory smoke test" in readme
    assert "manual tokens" in readme

def test_release_workflows_have_explicit_gates() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    staging = (REPOSITORY_ROOT / ".github/workflows/release-staging.yml").read_text(encoding="utf-8")
    assert "github.ref == 'refs/heads/main' && github.sha == inputs.candidate_sha" in workflow
    assert "inputs.publish_approval == 'PUBLISH'" in workflow
    assert "actions/attest-build-provenance@e3fe62ef559997059fe8380e7d2b4c909e2d65f4" in workflow
    assert "substrate-mcp.zip" in workflow and "substrate-mcp.zip" in staging
    assert "draft-${{ inputs.candidate_sha }}" in staging
    assert "PUBLISH" in workflow
    assert "Promote only a matching owner-created draft" in workflow
    assert "gh release edit" in workflow and "gh release create" not in workflow


def test_archives_are_deterministic_and_clean(tmp_path: Path) -> None:
    first = build_archive_bytes()
    assert build_archive_bytes() == first
    for name in PLUGINS:
        archive_path = tmp_path / f"{name}.zip"
        archive_path.write_bytes(build_archive_bytes_for(name))
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            assert names == sorted(names)
            assert f"{name}/LICENSE" in names
            assert all(info.date_time == FIXED_TIMESTAMP and info.create_system == 3 for info in archive.infolist())
            assert all("__pycache__" not in member and not member.endswith((".pyc", ".pyo")) for member in names)
    assert "substrate-hermes/plugin.yaml" in zipfile.ZipFile(tmp_path / ARCHIVE_NAME).namelist()

def test_release_metadata_and_mcp_manifest() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", (REPOSITORY_ROOT / "VERSION").read_text().strip())  # copies: test_versions
    plugin_manifest = json.loads((REPOSITORY_ROOT / "plugins/substrate-mcp/.claude-plugin/plugin.json").read_text())
    assert plugin_manifest["name"] == "substrate-mcp"
    manifest = json.loads((REPOSITORY_ROOT / "plugins/substrate-mcp/.mcp.json").read_text())
    server = manifest["mcpServers"]["substrate-memory"]
    assert server == {"type": "http", "url": "https://app.trysubstrate.co/mcp"}
    skill = (REPOSITORY_ROOT / "plugins/substrate-mcp/skills/substrate-memory/SKILL.md").read_text()
    assert skill.startswith("---\nname: substrate-memory\ndescription:")
    assert "best effort" in skill
    assert "full conversation transcript" in skill
    assert "API keys" in skill

def test_exactly_four_release_archives() -> None:
    assert set(PLUGINS) == {"substrate-hermes", "substrate-claude", "substrate-codex", "substrate-mcp"}
    assert len(PLUGINS) == 4


def test_hermes_archive_keeps_installed_identity() -> None:
    """The repo dir renamed, but installs stay ``substrate``.

    ``substrate-hermes.zip`` ships the renamed tree while ``plugin.yaml``
    keeps ``name: substrate`` (the identity Hermes derives install
    directories from), so existing installs and cutover scripts keep
    working while the repo folder is ``substrate-hermes``.
    """
    import io

    raw = build_archive_bytes_for("substrate-hermes")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        assert "substrate-hermes/plugin.yaml" in names
        manifest = archive.read("substrate-hermes/plugin.yaml").decode("utf-8")
        assert "name: substrate" in manifest.splitlines()
        version = (REPOSITORY_ROOT / "VERSION").read_text().strip()
        assert "version: %s" % version in manifest.splitlines()


def test_onboarding_has_one_shared_user_facing_contract() -> None:
    guide = (REPOSITORY_ROOT / "docs/installation.md").read_text()
    for phrase in (
        "Connect your agent to Substrate", "Approve connection", "Connection approved",
        "Connected to Substrate.", "authenticated memory smoke test", "manual tokens",
        "Verification",  # checked case-insensitively below
    ):
        assert phrase.lower() in guide.lower()
    for path in ("README.md", "AGENTS.md", "plugins/substrate-mcp/INSTALL.md"):
        text = (REPOSITORY_ROOT / path).read_text()
        assert "docs/installation.md" in text
        assert "Connection approved" in text
        assert "Connected to Substrate." in text
    assert "not proof" in guide
    assert "hermes plugins install https://github.com/Substrate-memory/Substrate-memory-plugins --enable" in guide
    assert "Do not select Hermes merely" in guide
    assert "automatically" in guide


def test_repository_root_is_an_installable_hermes_plugin() -> None:
    """``hermes plugins install <repo URL>`` installs the root: it must be a
    valid Hermes plugin that loads plugins/substrate-hermes (v0.11.0)."""
    import importlib.util

    root_manifest = (REPOSITORY_ROOT / "plugin.yaml").read_text(encoding="utf-8")
    package_manifest = (REPOSITORY_ROOT / "plugins/substrate-hermes/plugin.yaml").read_text(encoding="utf-8")
    body = [line for line in root_manifest.splitlines() if not line.startswith("#")]
    assert body == package_manifest.splitlines()
    assert "requires_hermes" not in root_manifest  # soft version policy: never a hard gate
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins_root_probe", REPOSITORY_ROOT / "__init__.py",
        submodule_search_locations=[str(REPOSITORY_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert callable(module.register)
    assert module.register.__module__.endswith("src.substrate.plugin")


def test_repository_root_is_not_a_python_project_for_hermes() -> None:
    """Hermes treats a root pyproject.toml as a plugin dependency project and
    installs it on `hermes plugins install <repo URL>` (the customer test saw
    "Installing Python dependencies" take 2 min 47 s). The dev tooling project
    lives in tooling/ and the plugin installs nothing."""
    assert not (REPOSITORY_ROOT / "pyproject.toml").exists()
    assert not (REPOSITORY_ROOT / "uv.lock").exists()
    tooling = (REPOSITORY_ROOT / "tooling" / "pyproject.toml").read_text(encoding="utf-8")
    # Pinned, but not to a given version: Dependabot bumps these pins.
    assert re.search(r'"ruff==[0-9][^"]*"', tooling) and re.search(r'"pytest==[0-9][^"]*"', tooling)
    assert "dependencies = []" in tooling
    assert (REPOSITORY_ROOT / "tooling" / "uv.lock").exists()
    assert (REPOSITORY_ROOT / "ruff.toml").exists() and (REPOSITORY_ROOT / "pytest.ini").exists()
    root_manifest = (REPOSITORY_ROOT / "plugin.yaml").read_text(encoding="utf-8")
    assert "python_runtime: external" in root_manifest
