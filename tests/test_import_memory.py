from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BENCHMARK = ROOT / "scripts" / "benchmark_import_memory.py"


@pytest.mark.skipif(sys.platform == "win32", reason="ru_maxrss byte accounting is POSIX-only")
# Each parameter case has a 900-second child kill bound; keep pytest bounded just above it.
@pytest.mark.timeout(960)
@pytest.mark.parametrize(
    ("arguments", "single", "source"),
    [
        (["--size-mib", "256"], False, "sqlite"),
        (["--size-mib", "1", "--single-message-mib", "64"], True, "sqlite"),
        (
            ["--size-mib", "1", "--single-message-mib", "64", "--source", "jsonl"],
            True,
            "jsonl",
        ),
    ],
)
def test_importer_rss_stays_below_256_mib(
    arguments: list[str], single: bool, source: str
) -> None:
    result = subprocess.run(
        [sys.executable, str(BENCHMARK), *arguments, "--limit-mib", "256"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    status = json.loads(result.stdout)
    assert status["complete"] is True
    assert status["single_message"] is single
    assert status["source"] == source
    assert status["peak_rss_bytes"] <= status["limit_bytes"]
