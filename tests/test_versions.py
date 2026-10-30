"""VERSION is the single source: every copy of the plugin version must match it."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("check_versions", REPO / "scripts" / "check_versions.py")
check_versions = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["check_versions"] = check_versions
_SPEC.loader.exec_module(check_versions)


def test_every_version_copy_matches_version_file() -> None:
    assert check_versions.check(REPO) == []


def test_every_listed_copy_exists() -> None:
    listed = (
        list(check_versions.JSON_COPIES) + [check_versions.MARKETPLACE_COPY]
        + list(check_versions.YAML_COPIES) + list(check_versions.PY_COPIES)
        + list(check_versions.README_PIN_COPIES)
    )
    assert all((REPO / rel).is_file() for rel in listed)


def test_rewriters_change_only_the_version_field() -> None:
    text = json.dumps({"name": "x", "version": "0.1.0", "keywords": ["0.1.0"]}, indent=2)
    new, count = check_versions._set_json(text, "0.2.0")
    assert count == 1 and '"version": "0.2.0"' in new and '"keywords": [\n    "0.1.0"' in new
    assert check_versions._set_yaml("name: a\nversion: 0.1.0\n", "0.2.0")[0] == "name: a\nversion: 0.2.0\n"
    assert check_versions._set_py('X = 1\nPLUGIN_VERSION = "0.1.0"\n', "0.2.0")[0] == 'X = 1\nPLUGIN_VERSION = "0.2.0"\n'
    readme = "use --ref <SHA of the v0.1.0 tag>"
    assert check_versions._set_readme(readme, "0.2.0")[0] == "use --ref <SHA of the v0.2.0 tag>"


def test_check_reports_a_drifted_copy(tmp_path: Path) -> None:
    """A copy that disagrees with VERSION is reported, by path."""
    for rel in (
        "VERSION", *check_versions.JSON_COPIES, check_versions.MARKETPLACE_COPY,
        *check_versions.YAML_COPIES, *check_versions.PY_COPIES, *check_versions.README_PIN_COPIES,
    ):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO / rel).read_bytes())
    assert check_versions.check(tmp_path) == []
    (tmp_path / "VERSION").write_text("9.9.9\n")
    problems = check_versions.check(tmp_path)
    assert problems and all("VERSION '9.9.9'" in line or "9.9.9" in line for line in problems)
    assert check_versions.write(tmp_path)  # sets every copy
    assert check_versions.check(tmp_path) == []
