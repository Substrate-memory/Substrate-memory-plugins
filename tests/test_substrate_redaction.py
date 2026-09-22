"""Shared redaction parity: plugin helpers pass contract/redaction-fixtures.json.

Both the Hermes plugin and the server redact with the rules in
docs/mcp-contract.md section 6. This test loads the shared fixture and
checks the plugin's ``_redact_text`` (text rules 1-2) and ``_safe_value``
(object rules 3-4) case by case.
"""

from __future__ import annotations

import json
from pathlib import Path

from substrate import plugin

FIXTURES = json.loads(
    (Path(__file__).resolve().parent.parent / "contract" / "redaction-fixtures.json")
    .read_text(encoding="utf-8")
)


def test_redaction_fixture_version() -> None:
    assert FIXTURES["version"] == 1
    assert FIXTURES["text"] and FIXTURES["objects"]


def test_text_cases() -> None:
    for case in FIXTURES["text"]:
        assert plugin._redact_text(case["in"]) == case["out"], case["in"]


def test_object_cases() -> None:
    for case in FIXTURES["objects"]:
        assert plugin._safe_value(case["in"]) == case["out"], case["in"]
