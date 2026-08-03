#!/usr/bin/env python3
"""Validate the canonical SUB-75 receipt and its mechanically derived budget."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT / "scripts"))

from benchmark_migration import (  # noqa: E402
    derive_budget,
    load_manifest,
    validate_receipt,
    verify_budget,
)

DEFAULT_MANIFEST = ROOT / "benchmarks" / "hermes-migration-manifest.json"
DEFAULT_SCHEMA = ROOT / "benchmarks" / "hermes-migration-receipt.schema.json"
DEFAULT_RECEIPT = ROOT / "benchmarks" / "evidence" / "hermes-migration-baseline.json"
DEFAULT_BUDGET = ROOT / "benchmarks" / "hermes-migration-budget.json"
BENCHMARK = ROOT / "scripts" / "benchmark_migration.py"


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _resolve_reference(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError(f"only local schema references are supported: {reference}")
    value: Any = root
    for component in reference[2:].split("/"):
        value = value[component.replace("~1", "/").replace("~0", "~")]
    if not isinstance(value, dict):
        raise ValueError(f"schema reference does not resolve to an object: {reference}")
    return value


def _schema_errors(
    value: Any, schema: dict[str, Any], root: dict[str, Any], path: str = "<root>"
) -> list[str]:
    errors: list[str] = []
    if "$ref" in schema:
        errors.extend(_schema_errors(value, _resolve_reference(root, schema["$ref"]), root, path))
    expected_type = schema.get("type")
    type_matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }
    if expected_type is not None and not type_matches.get(expected_type, False):
        return errors + [f"{path}: expected {expected_type}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: value does not match const")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value is not in enum")
    if isinstance(value, dict):
        required = schema.get("required", [])
        errors.extend(
            f"{path}: missing required property {key}" for key in required if key not in value
        )
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            errors.extend(
                f"{path}: unexpected property {key}" for key in value if key not in properties
            )
        for key, child in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                errors.extend(_schema_errors(child, child_schema, root, f"{path}/{key}"))
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            errors.append(f"{path}: too few items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path}: too many items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                errors.extend(_schema_errors(child, item_schema, root, f"{path}/{index}"))
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            errors.append(f"{path}: string is too short")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            errors.append(f"{path}: string does not match pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: value is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: value is above maximum")
    return errors


def validate_schema_document(schema: dict[str, Any], receipt: dict[str, Any]) -> None:
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("receipt schema must declare JSON Schema draft 2020-12")
    errors = _schema_errors(receipt, schema, schema)
    if errors:
        raise ValueError("receipt schema validation failed: " + "; ".join(sorted(errors)))


def verify_receipt_matrix(
    receipt: dict[str, Any], manifest: dict[str, Any], manifest_path: Path
) -> None:
    expected_manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if receipt.get("manifest_sha256") != expected_manifest_digest:
        raise ValueError("receipt manifest digest does not match canonical manifest bytes")
    expected_harness_digest = hashlib.sha256(BENCHMARK.read_bytes()).hexdigest()
    if receipt.get("harness_sha256") != expected_harness_digest:
        raise ValueError("receipt harness digest does not match canonical benchmark bytes")
    profile = receipt.get("profile")
    selected = manifest["profiles"].get(profile)
    if not isinstance(selected, list) or not selected:
        raise ValueError("receipt profile is not selected by the manifest")
    expected = {(str(case_id), concurrency) for case_id in selected for concurrency in (1, 2, 3, 4)}
    observed = {(run["case_id"], run["concurrency"]) for run in receipt["runs"]}
    if len(receipt["runs"]) != len(expected) or observed != expected:
        raise ValueError("receipt does not contain the exact case/concurrency matrix")
    if receipt.get("hosted_calls") != 0:
        raise ValueError("hosted calls are forbidden in canonical baseline evidence")
    provider = receipt.get("representative_provider", {})
    if (
        provider.get("mode") != "deterministic_simulation"
        or provider.get("network_access") is not False
    ):
        raise ValueError(
            "receipt must label provider behavior as an offline deterministic simulation"
        )
    for run in receipt["runs"]:
        concurrency = int(run["concurrency"])
        measured = int(run["provider"]["max_in_flight"])
        threads = int(run["provider"]["worker_threads_used"])
        if not (1 <= measured <= concurrency and 1 <= threads <= concurrency):
            raise ValueError("measured worker concurrency exceeds its configured seam")
    for concurrency in (1, 2, 3, 4):
        if (
            max(
                int(run["provider"]["max_in_flight"])
                for run in receipt["runs"]
                if run["concurrency"] == concurrency
            )
            != concurrency
        ):
            raise ValueError(f"receipt does not demonstrate measured concurrency {concurrency}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--budget", type=Path, default=DEFAULT_BUDGET)
    parser.add_argument(
        "--write-budget",
        action="store_true",
        help="replace --budget with the canonical derivation from --receipt",
    )
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    schema = _load_object(args.schema)
    receipt = _load_object(args.receipt)
    validate_schema_document(schema, receipt)
    validate_receipt(receipt)
    verify_receipt_matrix(receipt, manifest, args.manifest)

    if args.write_budget:
        budget = derive_budget(receipt)
        budget["receipt_sha256"] = hashlib.sha256(args.receipt.read_bytes()).hexdigest()
        args.budget.parent.mkdir(parents=True, exist_ok=True)
        args.budget.write_text(
            json.dumps(budget, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    budget = _load_object(args.budget)
    failures = verify_budget(receipt, budget)
    if failures:
        raise ValueError("; ".join(failures))
    print(
        json.dumps(
            {
                "budget": os.fspath(args.budget),
                "cases": len(budget["cases"]),
                "receipt": os.fspath(args.receipt),
                "runs": len(receipt["runs"]),
                "schema": "draft-2020-12",
                "status": "pass",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
