"""Load contract/redaction-fixtures.json.

Values that look like real key material are stored base64-encoded under "<key>_b64"
(so the repository carries no literal secret-shaped text, which the install scanners
of agent hosts reject). ``load`` decodes them back to their plain form.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent.parent / "contract" / "redaction-fixtures.json"


def decode(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key.endswith("_b64"):
                out[key[:-4]] = base64.b64decode(item).decode("utf-8")
            else:
                out[key] = decode(item)
        return out
    if isinstance(value, list):
        return [decode(item) for item in value]
    return value


def load(path: Path = FIXTURE):
    return decode(json.loads(Path(path).read_text(encoding="utf-8")))
