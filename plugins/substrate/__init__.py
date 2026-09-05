"""Hermes directory-plugin entry point."""

try:
    from .src.substrate.plugin import register
except ImportError:  # Imported directly by tooling from a src-layout checkout.
    from substrate.plugin import register

__all__ = ["register"]
