# Shared per-host test isolation for the Substrate multi-host suite.
#
# Every host plugin vendors a top-level substrate_core package (and some a
# top-level runtime), so plain sys.path imports make one host's copy win
# depending on collection order. This helper instead executes each host's
# modules from their explicit file paths under a unique module alias
# (_host_<slug>__...) and pins those aliases in sys.modules. Plain
# colliding names are only seeded during a host's own load (so absolute
# imports like `from substrate_core import runtime` resolve correctly)
# and are evicted on commit. Test modules hold the returned objects, so
# sibling files import fresh copies in any order. Lazy in-function imports
# keep working because each module's __package__ points at its pinned alias.
#
# Usage in a host test file (module level):
#
#     import _hostload
#     _ld = _hostload.begin("my-host", [PLUGIN_DIR, _hostload.REPO / "plugins"])
#     hermes_plugin = _ld.hermes("plugin")
#     runtime = _ld.core("runtime")
#     bridge = _ld.top("bridge.py")
#     _ld.commit()
#
# Standard library only.
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]
# Frozen Hermes 0.3.0 reference for host-adapter parity tests. The live
# reference (plugins/substrate/) moved to the durable v5 src-layout at
# 0.4.0 while the five host adapters stay byte-identical at 0.4.0, so
# parity assertions keep loading the exact 0.3.0 sources they shipped
# against. Test-only fixture: never packaged (see build_release.py).
HERMES_DIR = REPO / "tests" / "_hermes_030"

# Plain top-level module names any host test module may claim.
TOP_LEVELS = frozenset({
    "substrate",
    "substrate_core",
    "runtime",
    "contract",
    "client",
    "onboarding",
    "hosthome",
    "hooklib",
    "mcp_server",
    "bridge",
    "server",
    "transcript",
    "user_prompt_submit",
    "session_start",
})


def _colliding(name: str) -> bool:
    return (
        name in TOP_LEVELS
        or name.startswith("substrate_core.")
        or name.startswith("substrate.")
    )

def scrub() -> None:
    # Evict colliding modules and drop plugin dirs from sys.path so
    # environment-inherited state cannot pin one host's copy. Held
    # references in already-imported modules are unaffected.
    for name in list(sys.modules):
        if _colliding(name):
            del sys.modules[name]
    keep = []
    for entry in sys.path:
        normalized = entry.replace(chr(92), "/")
        if "/plugins/" in normalized or normalized.endswith("/plugins"):
            continue
        keep.append(entry)
    sys.path[:] = keep


_HERMES_ALIAS = "_hostload__substrate"


def _exec(alias: str, path: Path):
    spec = importlib.util.spec_from_file_location(alias, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module

class HostLoader:
    # Loads one host's modules from explicit file paths under a unique
    # alias; see the module header for the contract.
    def __init__(self, host: str, dirs: list) -> None:
        self.host = host
        self.slug = host.replace("-", "_")
        self.prefix = "_host_" + self.slug
        self.dirs = [str(item) for item in dirs]
        self.saved_modules = {
            name: sys.modules[name]
            for name in list(sys.modules)
            if _colliding(name)
        }
        self.saved_path = list(sys.path)
        for entry in reversed(self.dirs):
            if entry in sys.path:
                sys.path.remove(entry)
            sys.path.insert(0, entry)
        for name in list(sys.modules):
            if _colliding(name):
                del sys.modules[name]
        self.core_alias = self.prefix + "__substrate_core"
        core_dir = REPO / "plugins" / host / "substrate_core"
        self.core_dir = core_dir if core_dir.is_dir() else None
        if self.core_dir is not None:
            package = ModuleType(self.core_alias)
            package.__path__ = [str(self.core_dir)]
            sys.modules[self.core_alias] = package
            sys.modules["substrate_core"] = package
        if _HERMES_ALIAS not in sys.modules:
            package = ModuleType(_HERMES_ALIAS)
            package.__path__ = [str(HERMES_DIR)]
            sys.modules[_HERMES_ALIAS] = package
        sys.modules["substrate"] = sys.modules[_HERMES_ALIAS]

    def core(self, modname: str):
        # Load plugins/<host>/substrate_core/<modname>.py.
        assert self.core_dir is not None, self.host
        alias = self.core_alias + "." + modname
        if alias not in sys.modules:
            _exec(alias, self.core_dir / (modname + ".py"))
        module = sys.modules[alias]
        setattr(sys.modules[self.core_alias], modname, module)
        sys.modules["substrate_core." + modname] = module
        return module

    def hermes(self, modname: str):
        # Load the frozen reference <modname>.py once under a session-shared
        # alias (same frozen source for every host).
        alias = _HERMES_ALIAS + "." + modname
        if alias not in sys.modules:
            _exec(alias, HERMES_DIR / (modname + ".py"))
        module = sys.modules[alias]
        setattr(sys.modules[_HERMES_ALIAS], modname, module)
        sys.modules["substrate." + modname] = module
        return module

    def top(self, relpath: str, as_name: str | None = None):
        # Load plugins/<host>/<relpath> under a unique top-level alias.
        # The plain short name is seeded during the load only, so sibling
        # imports (import hooklib / import runtime) bind the right object.
        short = as_name or Path(relpath).name[:-len(".py")]
        alias = self.prefix + "__" + short
        if alias not in sys.modules:
            _exec(alias, REPO / "plugins" / self.host / relpath)
        module = sys.modules[alias]
        sys.modules[short] = module
        return module

    def commit(self) -> None:
        # Evict every plain colliding name (our seeds plus stragglers),
        # restore any pre-existing ones, restore sys.path. Aliases stay
        # pinned under their unique names for lazy in-function imports.
        for name in list(sys.modules):
            if _colliding(name):
                del sys.modules[name]
        sys.modules.update(self.saved_modules)
        sys.path[:] = self.saved_path


def begin(host: str, dirs: list) -> HostLoader:
    return HostLoader(host, dirs)
