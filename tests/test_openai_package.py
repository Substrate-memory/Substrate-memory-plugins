"""The OpenAI directory package builds, validates, and versions with the plugins."""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("build_openai_package", REPO / "scripts" / "build_openai_package.py")
pkg = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["build_openai_package"] = pkg
_SPEC.loader.exec_module(pkg)


def _copy(tmp_path: Path) -> Path:
    shutil.copytree(REPO / "openai", tmp_path / "openai")
    (tmp_path / "assets").mkdir()
    shutil.copy(REPO / "assets" / "logo.png", tmp_path / "assets" / "logo.png")
    shutil.copy(REPO / "VERSION", tmp_path / "VERSION")
    return tmp_path


def test_repository_package_is_valid() -> None:
    assert pkg.validate(REPO) == []


def test_zip_holds_manifest_mcp_and_logo(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    out = pkg.build(root)
    assert out.name == "substrate-openai-%s.zip" % version
    first = out.read_bytes()
    with zipfile.ZipFile(out) as archive:
        assert sorted(archive.namelist()) == ["assets/logo.png", "mcp.json", "plugin.json"]
        assert json.loads(archive.read("plugin.json"))["version"] == version
    assert pkg.build(root).read_bytes() == first


def test_version_drift_and_limits_are_reported(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    path = root / "openai" / "plugin.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = "0.0.1"
    ui = data["extensions"]["com.openai"]["interface"]
    ui["displayName"] = "x" * 31
    ui["longDescription"] += " Works with ChatGPT for $5/month."
    data["extensions"]["com.openai"]["review"]["test_cases"]["positive"][0]["tools_triggered"] = "gmail_search"
    path.write_text(json.dumps(data), encoding="utf-8")
    text = "\n".join(pkg.validate(root))
    for needle in ("must equal VERSION", "displayName", "pricing", "another AI platform", "non-core tools"):
        assert needle in text


def test_server_is_one_https_endpoint_without_secrets() -> None:
    mcp = json.loads((REPO / "openai" / "mcp.json").read_text(encoding="utf-8"))
    (server,) = mcp["mcpServers"].values()
    assert server["type"] == "streamable-http" and server["url"].startswith("https://")
    assert "headers" not in server


def test_package_name_matches_the_live_app() -> None:
    # The OpenAI portal matches uploads to the live app by this name.
    plugin = json.loads((REPO / "openai" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["name"] == "substrate-memory"
