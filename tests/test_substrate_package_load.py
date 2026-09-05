"""Package-load regression: the plugin loads the way the host loads it.

The host execs the repo root ``__init__.py`` as ``hermes_plugins.<slug>``
with ``submodule_search_locations=[repo root]`` and WITHOUT ``src/`` on
``sys.path`` (no PYTHONPATH, no editable install). This test replays that
in a subprocess with a scrubbed environment, so it stays independent of
the pytest ``pythonpath`` setting. It also asserts the load neither needs
a top-level ``substrate`` module nor mutates ``sys.path`` (no sys.path
hack).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "substrate"

EXPECTED_HOOKS = {
    "pre_llm_call",
    "post_llm_call",
    "on_session_end",
    "on_session_finalize",
    "on_session_reset",
    "subagent_start",
    "subagent_stop",
}
EXPECTED_TOOLS = {
    "memory_search",
    "memory_expand",
    "memory_evidence",
    "memory_remember",
    "memory_forget",
}

_LOADER = textwrap.dedent(
    """
    import importlib.util
    import json
    import sys

    from pathlib import Path

    root = Path(sys.argv[1])
    before_path = list(sys.path)
    before_modules = set(sys.modules)

    import types
    import importlib.abc

    # Faithful host replay: the host creates the namespace parent first.
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        ns.__package__ = "hermes_plugins"
        sys.modules["hermes_plugins"] = ns

    # Block top-level substrate: the doctor env has no src on path and no
    # install (uv may install the project into the test env; that must not
    # mask a broken relative chain).
    class Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "substrate" or name.startswith("substrate."):
                raise ImportError("blocked top-level import: " + name)
            return None

    sys.meta_path.insert(0, Blocker())

    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.substrate",
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "hermes_plugins.substrate"
    module.__path__ = [str(root)]
    sys.modules["hermes_plugins.substrate"] = module
    spec.loader.exec_module(module)


    class Context:
        def __init__(self):
            self.hooks = {}
            self.tools = {}

        def register_hook(self, name, callback):
            self.hooks[name] = callback

        def register_tool(self, *, name, toolset, schema, handler, **kwargs):
            self.tools[name] = handler

        def register_system_prompt_section(self, *args, **kwargs):
            pass


    ctx = Context()
    module.register(ctx)
    print(json.dumps({
        "hooks": sorted(ctx.hooks),
        "tools": sorted(ctx.tools),
        "sys_path_changed": list(sys.path) != before_path,
        "top_level_substrate": "substrate" in sys.modules,
        "new_top_level": sorted(
            name for name in sys.modules
            if name not in before_modules and "." not in name
        ),
    }))
    """
)


def _run_host_style_load(tmp_path: Path) -> dict:
    script = tmp_path / "host_style_load.py"
    script.write_text(_LOADER)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
    }
    completed = subprocess.run(
        [sys.executable, str(script), str(PLUGIN_DIR)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
        env=env,
    )
    assert completed.returncode == 0, (
        f"host-style load failed:\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    )
    return json.loads(completed.stdout)


def test_host_style_load_registers_without_src_on_path(tmp_path):
    result = _run_host_style_load(tmp_path)
    assert result["hooks"] == sorted(EXPECTED_HOOKS)
    assert result["tools"] == sorted(EXPECTED_TOOLS)
    assert result["sys_path_changed"] is False
    assert result["top_level_substrate"] is False
