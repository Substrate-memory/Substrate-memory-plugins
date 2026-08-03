from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

REPOSITORY_ROOT = Path(__file__).parents[1]
VERIFIER_PATH = REPOSITORY_ROOT / "scripts" / "verify_public_plugin_candidate.py"


def load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_public_plugin_candidate", VERIFIER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)


def make_candidate(tmp_path: Path) -> tuple[ModuleType, Path, Path]:
    verifier = load_verifier()
    root = tmp_path / "candidate"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Scanner Test")
    git(root, "config", "user.email", "scanner@example.test")

    tracked = root / "README.md"
    tracked.write_text("safe candidate\n", encoding="utf-8")
    manifest = root / "docs" / "extraction-manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "source": "host/README.md",
                        "destination": "README.md",
                        "source_sha256": "0" * 64,
                        "destination_sha256": __import__("hashlib").sha256(
                            tracked.read_bytes()
                        ).hexdigest(),
                        "class": "documentation",
                    }
                ],
                "destination_only": [],
                "self_excluded_path": "docs/extraction-manifest.json",
            }
        ),
        encoding="utf-8",
    )
    value = json.loads(manifest.read_text(encoding="utf-8"))
    policy = {
        "entries": sorted(
            (
                {
                    "source": item["source"],
                    "destination": item["destination"],
                    "class": item["class"],
                }
                for item in value["entries"]
            ),
            key=lambda item: item["destination"],
        ),
        "destination_only": [],
    }
    verifier.TRUSTED_INVENTORY_POLICY_SHA256 = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    git(root, "add", ".")
    git(root, "commit", "-qm", "safe base")
    return verifier, root, manifest


def test_destination_scanner_rejects_untracked_secret_file(tmp_path: Path) -> None:
    verifier, root, manifest = make_candidate(tmp_path)
    secret = "sk-" + "A" * 32
    extra = root / "private" / "production.env"
    extra.parent.mkdir()
    extra.write_text(secret, encoding="utf-8")

    findings = verifier.scan_candidate(root, manifest, layout="destination")

    assert any("unexpected untracked candidate file" in finding for finding in findings)
    assert any("OpenAI-shaped credential" in finding for finding in findings)


def test_destination_scanner_invalidates_exact_file_exemption_on_mutation(tmp_path: Path) -> None:
    verifier, root, manifest = make_candidate(tmp_path)
    tracked = root / "README.md"
    original_digest = verifier.hashlib.sha256(tracked.read_bytes()).hexdigest()
    verifier.SYNTHETIC_FILE_SHA256_ALLOWLIST["README.md"] = frozenset({original_digest})
    assert verifier.scan_candidate(root, manifest, layout="destination") == []

    tracked.write_text("Authorization: Bearer " + "B" * 32, encoding="utf-8")

    findings = verifier.scan_candidate(root, manifest, layout="destination")
    assert any("Bearer credential" in finding for finding in findings)


def test_destination_scanner_rejects_secret_in_reachable_history(tmp_path: Path) -> None:
    verifier, root, manifest = make_candidate(tmp_path)
    historical = root / "historical.txt"
    historical.write_text("sk-" + "C" * 32, encoding="utf-8")
    git(root, "add", "historical.txt")
    git(root, "commit", "-qm", "unsafe history")
    historical.unlink()
    git(root, "add", "-u")
    git(root, "commit", "-qm", "remove working-tree secret")

    findings = verifier.scan_candidate(root, manifest, layout="destination")

    assert any(finding.startswith("git:") for finding in findings)
    assert any("OpenAI-shaped credential" in finding for finding in findings)


def test_destination_scanner_rejects_ignored_secret_file(tmp_path: Path) -> None:
    verifier, root, manifest = make_candidate(tmp_path)
    secret_name = "ignored-production.env"
    (root / ".git" / "info" / "exclude").write_text(secret_name + "\n", encoding="utf-8")
    (root / secret_name).write_text("sk-" + "D" * 32, encoding="utf-8")

    findings = verifier.scan_candidate(root, manifest, layout="destination")

    assert any("unexpected untracked candidate file" in finding for finding in findings)
    assert any("OpenAI-shaped credential" in finding for finding in findings)


def test_destination_scanner_rejects_duplicate_manifest_mapping(tmp_path: Path) -> None:
    verifier, root, manifest = make_candidate(tmp_path)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["entries"].append(dict(value["entries"][0]))
    manifest.write_text(json.dumps(value), encoding="utf-8")

    findings = verifier.scan_candidate(root, manifest, layout="destination")

    assert any("duplicate source inventory entry" in finding for finding in findings)
    assert any("duplicate destination inventory entry" in finding for finding in findings)


def test_destination_scanner_rejects_coordinated_held_code_inventory_rewrite(
    tmp_path: Path,
) -> None:
    verifier, root, manifest = make_candidate(tmp_path)
    held = root / "infra" / "deploy.py"
    held.parent.mkdir()
    held.write_text("def deploy_private_broker():\n    return 'held'\n", encoding="utf-8")
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["destination_only"].append(
        {
            "path": "infra/deploy.py",
            "class": "standalone_repository_policy_or_test",
            "sha256": hashlib.sha256(held.read_bytes()).hexdigest(),
        }
    )
    manifest.write_text(json.dumps(value), encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "attempt coordinated held-code admission")

    findings = verifier.scan_candidate(root, manifest, layout="destination")

    assert "candidate: closed inventory path/class policy mismatch" in findings


def test_destination_scanner_rejects_extraction_source_rewrite(tmp_path: Path) -> None:
    verifier, root, manifest = make_candidate(tmp_path)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["entries"][0]["source"] = "private-server/held-broker.py"
    manifest.write_text(json.dumps(value), encoding="utf-8")

    findings = verifier.scan_candidate(root, manifest, layout="destination")

    assert "candidate: closed inventory path/class policy mismatch" in findings


def test_destination_scanner_rejects_secret_in_reachable_commit_message(tmp_path: Path) -> None:
    verifier, root, manifest = make_candidate(tmp_path)
    message = "unsafe commit " + "sk-" + "E" * 32
    git(root, "commit", "--allow-empty", "-qm", message)

    findings = verifier.scan_candidate(root, manifest, layout="destination")

    assert any(finding.startswith("git-object:") for finding in findings)
    assert any("OpenAI-shaped credential" in finding for finding in findings)


def test_destination_scanner_rejects_secret_in_annotated_tag_message(tmp_path: Path) -> None:
    verifier, root, manifest = make_candidate(tmp_path)
    message = "unsafe tag " + "sk-" + "F" * 32
    git(root, "tag", "-a", "unsafe", "-m", message)

    findings = verifier.scan_candidate(root, manifest, layout="destination")

    assert any(finding.startswith("git-object:") for finding in findings)
    assert any("OpenAI-shaped credential" in finding for finding in findings)


def test_destination_scanner_rejects_directly_referenced_secret_blob(tmp_path: Path) -> None:
    verifier, root, manifest = make_candidate(tmp_path)
    secret = ("sk-" + "G" * 32).encode("ascii")
    object_id = subprocess.check_output(
        ("git", "hash-object", "-w", "--stdin"),
        cwd=root,
        input=secret,
    ).decode("ascii").strip()
    git(root, "update-ref", "refs/tags/unsafe-direct-blob", object_id)

    findings = verifier.scan_candidate(root, manifest, layout="destination")

    assert any(finding.startswith(f"git-object:{object_id}:blob") for finding in findings)
    assert any("OpenAI-shaped credential" in finding for finding in findings)


def test_destination_scanner_rejects_historical_hermes_api_key_assignment(
    tmp_path: Path,
) -> None:
    verifier, root, manifest = make_candidate(tmp_path)
    historical = root / "production.env"
    historical.write_text(
        "HERMES_API_" + "KEY=" + "H" * 40 + "\n",
        encoding="utf-8",
    )
    git(root, "add", "production.env")
    git(root, "commit", "-qm", "unsafe Hermes credential history")
    historical.unlink()
    git(root, "add", "-u")
    git(root, "commit", "-qm", "remove working-tree credential")

    findings = verifier.scan_candidate(root, manifest, layout="destination")

    assert any(finding.startswith("git:") for finding in findings)
    assert any("Hermes API credential assignment" in finding for finding in findings)
