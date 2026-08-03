#!/usr/bin/env python3
"""Validate a fresh privacy-safe migration matrix against retained beta ceilings."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT / "scripts"))

from benchmark_migration import evaluate_budget, load_manifest, validate_receipt  # noqa: E402
from verify_migration_baseline import (  # noqa: E402
    DEFAULT_BUDGET,
    DEFAULT_MANIFEST,
    DEFAULT_SCHEMA,
    validate_schema_document,
    verify_receipt_matrix,
)


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--budget", type=Path, default=DEFAULT_BUDGET)
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    schema = load_object(args.schema)
    receipt = load_object(args.receipt)
    budget = load_object(args.budget)
    validate_schema_document(schema, receipt)
    validate_receipt(receipt)
    verify_receipt_matrix(receipt, manifest, args.manifest)

    case_budgets = budget.get("cases")
    if not isinstance(case_budgets, dict):
        raise ValueError("retained budget cases must be a mapping")
    failures: list[str] = []
    for run in receipt["runs"]:
        case_id = str(run["case_id"])
        case_budget = case_budgets.get(case_id)
        if not isinstance(case_budget, dict):
            failures.append(f"missing retained budget for case {case_id}")
            continue
        failures.extend(
            f"{case_id}/c{run['concurrency']}: {failure}"
            for failure in evaluate_budget(run, case_budget)
        )
    if failures:
        raise ValueError("; ".join(failures))
    print(
        json.dumps(
            {
                "budget": os.fspath(args.budget),
                "receipt": os.fspath(args.receipt),
                "runs": len(receipt["runs"]),
                "status": "pass",
                "verification": "fresh_run_against_retained_ceilings",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
