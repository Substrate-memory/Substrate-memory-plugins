"""Release packaging tests for the Substrate retrieval plugin."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"

sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from build_release import ARCHIVE_NAME, FIXED_TIMESTAMP, PREFIX, build_archive_bytes  # noqa: E402


def plugin_version() -> str:
    for line in (REPOSITORY_ROOT / "plugins" / "substrate" / "plugin.yaml").read_text().splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("plugin.yaml has no version")


def test_root_readme_describes_the_released_install() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    assert "plugins/substrate" in readme
    assert "refs/tags/v0.5.0" in readme
    assert "--ref \"$substrate_ref\"" in readme
    assert "Hermes 0.21" in readme
    assert "verification_uri_complete" in readme
    assert "PLUGIN_SHA256_PENDING" not in readme
    assert "is not published yet" not in readme


def test_release_workflow_keeps_dependency_execution_out_of_privileged_publisher() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    verify, publish = workflow.split("\n  publish:\n", maxsplit=1)

    assert "github.ref == 'refs/heads/main' && github.sha == inputs.candidate_sha" in verify
    assert "persist-credentials: false" in verify
    assert "uv sync --frozen --extra dev --no-install-project" in verify
    assert "uv run --no-sync" in verify
    assert "contents: write" not in verify
    assert "id-token: write" not in verify
    assert "attestations: write" not in verify

    assert "environment: public-release" in publish
    assert "github.ref == 'refs/heads/main' && github.sha == inputs.candidate_sha" in publish
    assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in publish
    assert "contents: write" in publish
    assert "id-token: write" in publish
    assert "attestations: write" in publish
    assert "actions/checkout@" not in publish
    assert "uv sync" not in publish
    assert "uv run" not in publish
    assert "setuptools" not in publish


def test_archive_is_deterministic_and_release_clean(tmp_path: Path) -> None:
    first = build_archive_bytes()
    assert build_archive_bytes() == first

    archive_path = tmp_path / ARCHIVE_NAME
    archive_path.write_bytes(first)
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert f"{PREFIX}LICENSE" in names
        assert f"{PREFIX}plugin.yaml" in names
        assert f"{PREFIX}__init__.py" in names
        assert f"{PREFIX}onboard.py" in names
        assert f"{PREFIX}setup.py" in names
        assert f"{PREFIX}CONTRACT.md" in names
        assert f"{PREFIX}src/substrate/plugin.py" in names
        assert f"{PREFIX}src/substrate/spool.py" in names
        assert f"{PREFIX}src/substrate/onboarding.py" in names
        assert f"{PREFIX}src/substrate/credentials.py" in names
        assert f"{PREFIX}src/substrate/client.py" in names
        assert f"{PREFIX}src/substrate/contract.py" in names
        assert f"{PREFIX}src/substrate/contract/envelope-fixtures.json" in names
        for info in archive.infolist():
            assert info.date_time == FIXED_TIMESTAMP
            assert info.create_system == 3
            assert "__pycache__" not in info.filename
            assert not info.filename.endswith((".pyc", ".pyo"))
        assert archive.read(f"{PREFIX}LICENSE") == (REPOSITORY_ROOT / "LICENSE").read_bytes()


def test_plugin_manifest_matches_release_version() -> None:
    assert plugin_version() == "0.4.0"


def test_release_metadata_permits_mixed_set_versions() -> None:
    """0.5.0 set / Hermes 0.4.0 / adapters 0.4.0: pins must match the plan."""
    import json
    import re

    version = (REPOSITORY_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
    assert version == "0.5.0"
    assert plugin_version() == "0.4.0"
    manifests = [
        "plugins/claude-code/.claude-plugin/plugin.json",
        "plugins/claude-cowork/.claude-plugin/plugin.json",
        "plugins/codex/.codex-plugin/plugin.json",
        "plugins/grok-bot/plugin.json",
        "plugins/openclaw/openclaw.plugin.json",
    ]
    assert len(manifests) == 5
    for rel in manifests:
        data = json.loads((REPOSITORY_ROOT / rel).read_text(encoding="utf-8"))
        assert data["version"] == "0.4.0", rel
    # The Release workflow must pin the frozen adapters to 0.4.0 explicitly
    # and must not require them to equal the repo-level release version.
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert 'test "$hermes_version" = "0.4.0"' in workflow
    assert ')" = "0.4.0"' in workflow
    assert ')" = "$version"' not in workflow


def test_other_host_archives_stay_byte_identical_to_0_4_0() -> None:
    """The five non-Hermes adapters are frozen: 0.5.0 must not alter them."""
    from build_release import build_archive_bytes_for, digest

    frozen = {
        "claude-code": "84d4b87e4e6ce713630bbd78fbea5f606768a66920fb2e3d88819e4965aff6ff",
        "claude-cowork": "c1d3d60c8dcb993c42046a4241c4a569a70ff3142760199a32dc2e3b44465b3e",
        "codex": "8bb96b72d217bde9bf5403185547fd8f51ebbf01cfbbfc0438672888f25bf416",
        "grok-bot": "e37e67daca5de58eca4057b0119889b9d12deb2f5364a8e046562cfbb316ab21",
        "openclaw": "0059fd81135b1eacd7f36801caa54533605400f2e6374909158cfe9e3dd55976",
    }
    for name, sha in frozen.items():
        assert digest(build_archive_bytes_for(name)) == sha, name


def test_archives_never_ship_private_or_generated_files() -> None:
    from build_release import PLUGINS

    forbidden_fragments = (
        "__pycache__/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".venv/",
        "/tests/",
        "/test/",
        "htmlcov/",
        "node_modules/",
        "/dist/",
        "/build/",
        ".env",
    )
    for name in PLUGINS:
        archive_path = REPOSITORY_ROOT / "dist" / f"{name}.zip"
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.namelist()
        for member in members:
            assert not member.endswith((".pyc", ".pyo")), member
            for fragment in forbidden_fragments:
                assert fragment not in member, member
        # No absolute filesystem literals or build-machine checkout paths
        # inside shipped code (doc comments may still show example paths).
        with zipfile.ZipFile(archive_path) as archive:
            for member in members:
                assert not member.startswith("/"), member
                payload = archive.read(member)
                if member.endswith(".py"):
                    text = payload.decode("utf-8")
                    assert 'Path("/' not in text, member
                    assert str(REPOSITORY_ROOT) not in text, member
