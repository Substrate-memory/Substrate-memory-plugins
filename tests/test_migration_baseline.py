from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.benchmark_migration import (
    PHASE_NAMES,
    evaluate_budget,
    load_manifest,
    validate_receipt,
)

ROOT = Path(__file__).parents[1]
BENCHMARK = ROOT / "scripts" / "benchmark_migration.py"
VERIFIER = ROOT / "scripts" / "verify_migration_baseline.py"
MANIFEST = ROOT / "benchmarks" / "hermes-migration-manifest.json"
SCHEMA = ROOT / "benchmarks" / "hermes-migration-receipt.schema.json"


def test_manifest_covers_required_sources_scales_and_stream_v2_concurrency() -> None:
    manifest = load_manifest(MANIFEST)

    cases = {case["id"]: case for case in manifest["cases"]}
    assert {
        "small-sqlite",
        "current-production-jsonl",
        "large-sqlite",
        "oversized-message-jsonl",
        "sqlite-adapter",
        "official-export-jsonl",
    } <= set(cases)
    assert {case["source"] for case in cases.values()} == {"sqlite", "jsonl"}
    assert {case["fixture_kind"] for case in cases.values()} >= {
        "sqlite",
        "official_export_jsonl",
    }
    assert manifest["protocol"] == "stream-v2"
    assert manifest["concurrency"] == [1, 2, 3, 4]
    assert cases["current-production-jsonl"]["sessions"] == 1212
    assert cases["large-sqlite"]["sessions"] >= 2048
    assert cases["oversized-message-jsonl"]["oversized_message_bytes"] > 262_144


def test_receipt_schema_requires_every_phase_and_lifecycle_boundary() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    run = schema["$defs"]["run"]
    assert set(PHASE_NAMES) == set(run["properties"]["phase_seconds"]["required"])
    assert run["properties"]["lifecycle_seconds"]["required"] == [
        "transferred",
        "first_usable",
        "fully_ready",
    ]
    assert schema["additionalProperties"] is False
    assert run["additionalProperties"] is False


# The benchmark child has a 240-second kill bound; preserve 60 seconds for verification/cleanup.
@pytest.mark.timeout(300)
def test_schema_and_budget_verifier_accepts_a_fresh_contract_matrix(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    budget_path = tmp_path / "budget.json"
    benchmark = subprocess.run(
        [
            sys.executable,
            str(BENCHMARK),
            "--profile",
            "contract",
            "--output",
            str(receipt_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert benchmark.returncode == 0, benchmark.stdout + benchmark.stderr
    verification = subprocess.run(
        [
            sys.executable,
            str(VERIFIER),
            "--receipt",
            str(receipt_path),
            "--budget",
            str(budget_path),
            "--write-budget",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert verification.returncode == 0, verification.stdout + verification.stderr
    result = json.loads(verification.stdout)
    assert result["runs"] == 4
    assert result["schema"] == "draft-2020-12"
    assert result["status"] == "pass"


def test_default_verifier_selects_retained_canonical_receipt() -> None:
    verification = subprocess.run(
        [sys.executable, str(VERIFIER)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert verification.returncode == 0, verification.stdout + verification.stderr
    assert json.loads(verification.stdout)["receipt"].endswith(
        "benchmarks/evidence/hermes-migration-baseline.json"
    )


def test_single_case_worker_reports_process_local_rss(tmp_path: Path) -> None:
    fixture_path = tmp_path / "state.db"
    row_path = tmp_path / "row.json"
    generated = subprocess.run(
        [
            sys.executable,
            str(BENCHMARK),
            "--generate-case",
            "small-sqlite",
            "--output",
            str(fixture_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr

    worker = subprocess.run(
        [
            sys.executable,
            str(BENCHMARK),
            "--run-case",
            "small-sqlite",
            "--concurrency",
            "1",
            "--fixture",
            str(fixture_path),
            "--output",
            str(row_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert worker.returncode == 0, worker.stdout + worker.stderr
    row = json.loads(row_path.read_text(encoding="utf-8"))
    assert row["case_id"] == "small-sqlite"
    assert row["concurrency"] == 1
    assert 0 < row["peak_rss_bytes"] < 256 * 1024 * 1024


def test_idempotency_replay_covers_side_effecting_event_and_full_durable_state() -> None:
    receipt = json.loads(
        (ROOT / "benchmarks" / "evidence" / "hermes-migration-baseline.json").read_text(
            encoding="utf-8"
        )
    )
    expected_components = [
        "duplicate_acks",
        "encoded_bytes",
        "max_queue_depth",
        "projected_digest",
        "projected_rows",
        "queue_items",
        "received_digest",
        "received_rows",
        "redacted_events",
        "redaction_failures",
        "requests",
        "work_queue_items",
    ]
    assert all(
        run["integrity"]["idempotency_replay_kind"] == "session_end"
        and run["integrity"]["idempotency_state_components"] == expected_components
        and run["integrity"]["idempotency_replays"] == 1
        and run["integrity"]["duplicate_acks"] == 1
        and run["integrity"]["duplicate_side_effects"] == 0
        for run in receipt["runs"]
    )


def test_every_child_process_denies_socket_construction(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["SUBSTRATE_BENCHMARK_NETWORK_DENIED"] = "1"
    probe = subprocess.run(
        [sys.executable, str(BENCHMARK), "--probe-network", "--output", str(tmp_path / "unused")],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode != 0
    assert "network access is disabled for this benchmark" in probe.stderr


def test_partial_startup_probe_terminates_without_barrier_hang(tmp_path: Path) -> None:
    probe = subprocess.run(
        [
            sys.executable,
            str(BENCHMARK),
            "--probe-partial-failure",
            "--output",
            str(tmp_path / "unused"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr


@pytest.mark.parametrize("case_id", ["../escape", "/absolute", "bad/slash", "UPPER"])
def test_manifest_rejects_unsafe_case_ids(tmp_path: Path, case_id: str) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["cases"][0]["id"] = case_id
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="case id"):
        load_manifest(path)


# The benchmark child has a 240-second kill bound; preserve 60 seconds for receipt checks.
@pytest.mark.timeout(300)
def test_ci_profile_emits_content_free_aggregate_receipt(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    result = subprocess.run(
        [
            sys.executable,
            str(BENCHMARK),
            "--manifest",
            str(MANIFEST),
            "--profile",
            "contract",
            "--output",
            str(receipt_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    validate_receipt(receipt)
    assert receipt["schema_version"] == 1
    assert receipt["protocol"] == "stream-v2"
    assert receipt["harness_sha256"] == hashlib.sha256(BENCHMARK.read_bytes()).hexdigest()
    assert receipt["hosted_calls"] == 0
    assert receipt["representative_provider"]["mode"] == "deterministic_simulation"
    assert {run["concurrency"] for run in receipt["runs"]} == {1, 2, 3, 4}
    assert all(set(run["phase_seconds"]) == set(PHASE_NAMES) for run in receipt["runs"])
    assert all(
        run["lifecycle_seconds"]["transferred"] < run["lifecycle_seconds"]["fully_ready"]
        and run["lifecycle_seconds"]["first_usable"] < run["lifecycle_seconds"]["fully_ready"]
        and run["lifecycle_seconds"]["first_usable"] != run["lifecycle_seconds"]["transferred"]
        for run in receipt["runs"]
    )
    assert all(
        1 <= run["provider"]["max_in_flight"] <= run["concurrency"]
        and 1 <= run["provider"]["worker_threads_used"] <= run["concurrency"]
        for run in receipt["runs"]
    )
    assert max(run["provider"]["max_in_flight"] for run in receipt["runs"]) == 4
    assert all(
        run["integrity"]["idempotency_replays"] == 1
        and run["integrity"]["duplicate_acks"] == 1
        and run["integrity"]["duplicate_side_effects"] == 0
        for run in receipt["runs"]
    )
    serialized = json.dumps(receipt, sort_keys=True).casefold()
    for forbidden in (
        "prompt",
        "transcript",
        "api_key",
        "secret",
        "password",
        "sk-benchmark-canary",
    ):
        assert forbidden not in serialized


def test_receipt_validator_rejects_content_bearing_trace_fields() -> None:
    with pytest.raises(ValueError, match="content-free"):
        validate_receipt({"runs": [{"prompt": "source content"}]})


@pytest.mark.parametrize(
    "receipt",
    [
        {"runs": [{"source_locator": "/private/history.jsonl"}]},
        {"runs": [{"session_id": "private-session"}]},
        {"runs": [{"trace": "Authorization: Bearer private"}]},
        {"runs": [{"trace": "sk-private-looking-value"}]},
    ],
)
def test_receipt_validator_rejects_source_identifiers_and_secret_shapes(
    receipt: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="content-free"):
        validate_receipt(receipt)


def test_budget_gate_covers_speed_resources_integrity_quality_quota_and_cost() -> None:
    budget = {
        "max_transferred_seconds": 2.0,
        "max_first_usable_seconds": 3.0,
        "max_fully_ready_seconds": 4.0,
        "max_peak_rss_bytes": 256 * 1024 * 1024,
        "max_cpu_seconds": 2.0,
        "max_terminal_failures": 0,
        "max_projection_failures": 0,
        "max_redaction_failures": 0,
        "min_redacted_events": 1,
        "min_duplicate_acks": 1,
        "max_duplicate_side_effects": 0,
        "min_integrity_ratio": 1.0,
        "max_provider_retries": 0,
        "max_modeled_cost_usd": 0.01,
        "max_modeled_quota_seconds": 60.0,
    }
    run: dict[str, Any] = {
        "case_id": "synthetic",
        "concurrency": 1,
        "lifecycle_seconds": {
            "transferred": 1.0,
            "first_usable": 2.0,
            "fully_ready": 3.0,
        },
        "peak_rss_bytes": 32 * 1024 * 1024,
        "cpu_seconds": 1.0,
        "terminal_failures": 0,
        "integrity": {
            "expected_windows": 4,
            "projected_windows": 4,
            "redacted_events": 4,
            "redaction_failures": 0,
            "idempotency_replays": 1,
            "duplicate_acks": 1,
            "duplicate_side_effects": 0,
            "complete": True,
        },
        "quality": {"projection_failures": 0, "terminal_failures": 0},
        "provider": {
            "retries": 0,
            "modeled_cost_usd": 0.001,
            "modeled_quota_seconds": 4.0,
        },
    }
    assert evaluate_budget(run, budget) == []

    run["integrity"]["projected_windows"] = 3
    failures = evaluate_budget(run, budget)
    assert any("integrity_ratio" in failure for failure in failures)
