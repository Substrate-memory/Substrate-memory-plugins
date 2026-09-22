# Source-of-truth boundary

This repository owns the four plugin packages (`plugins/substrate-claude`, `plugins/substrate-codex`, `plugins/substrate-hermes`, `plugins/substrate-mcp`), documentation, deterministic builds, tests, and release staging. The Substrate server repository owns the remote MCP endpoint, OAuth consent, persistence, ranking, and deployment.

`docs/mcp-contract.md` (contract v2) is the single source of truth for the memory tool surface. Do not change it from this repository; client packages and docs follow it.

The v0.5.0 and v0.6.0 tags are immutable and remain available for rollback. v0.7.0 is a review candidate until explicit release approval; no tag, public release, or deployment is made by this branch.
