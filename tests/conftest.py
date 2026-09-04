# Session-level isolation for the multi-host test suite.
#
# Each host plugin vendors top-level module names (substrate_core and, for
# some hosts, runtime/contract/bridge/...), so this reaps any colliding
# modules and plugin sys.path entries before collection and after every
# test. Host test files additionally wrap their own imports in
# _hostload.isolated() so collection order never decides which hosts copy
# wins. Frozen reference tests (test_retrieval_*.py) import the Hermes
# plugin themselves and are unaffected: they hold their own references.
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _hostload

_hostload.scrub()


@pytest.fixture(autouse=True)
def _reap_host_module_leak():
    before_path = list(sys.path)
    before_modules = {
        name for name in sys.modules if _hostload._colliding(name)
    }
    yield
    sys.path[:] = before_path
    for name in list(sys.modules):
        if _hostload._colliding(name) and name not in before_modules:
            del sys.modules[name]
