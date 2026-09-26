"""Hermes entry point when the whole repository is installed by its URL.

``hermes plugins install https://github.com/Substrate-memory/Substrate-memory-plugins``
installs the repository root. This file loads the Hermes plugin from
``plugins/substrate-hermes``; other hosts ignore it. Standard library only.
"""

from importlib import import_module

# Hermes imports this file as a package (``hermes_plugins.<name>``). Test
# runners may import it as a plain module; then there is nothing to load.
if __package__:
    register = import_module(".plugins.substrate-hermes", __name__).register
    __all__ = ["register"]
