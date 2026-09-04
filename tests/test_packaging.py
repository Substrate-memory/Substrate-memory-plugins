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
    assert "refs/tags/v0.3.0" in readme
    assert "--ref \"$substrate_ref\"" in readme
    assert "Hermes 0.21" in readme
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
        assert f"{PREFIX}plugin.py" in names
        for info in archive.infolist():
            assert info.date_time == FIXED_TIMESTAMP
            assert info.create_system == 3
            assert "__pycache__" not in info.filename
            assert not info.filename.endswith((".pyc", ".pyo"))
        assert archive.read(f"{PREFIX}LICENSE") == (REPOSITORY_ROOT / "LICENSE").read_bytes()


def test_plugin_manifest_matches_release_version() -> None:
    assert plugin_version() == "0.3.0"
