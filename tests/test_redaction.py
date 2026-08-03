from __future__ import annotations

import json
from pathlib import Path

import pytest

from substrate_wiki.redaction import redact_text


@pytest.mark.parametrize("vector", json.loads((Path(__file__).parent / "fixtures" / "credential_redaction_vectors.json").read_text(encoding="utf-8")), ids=lambda item: item["name"])
def test_credential_vectors_are_redacted(vector: dict[str, object]) -> None:
    rendered = redact_text(str(vector["text"]), ())
    fragments = vector.get("secret_fragments", [vector.get("secret_fragment")])
    assert isinstance(fragments, list)
    for fragment in fragments:
        assert isinstance(fragment, str)
        assert fragment not in rendered


@pytest.mark.parametrize(
    "value",
    (
        "%79%61%32%39%2EFAKE0123456789abcdef",
        "%2579%2561%2532%2539%252EFAKE0123456789",
        "%41%4B%49%41FAKE0123456789AB",
        "%2541%254B%2549%2541FAKE0123456789AB",
    ),
)
def test_encoded_provider_credentials_are_redacted(value: str) -> None:
    assert value not in redact_text(value, ())
