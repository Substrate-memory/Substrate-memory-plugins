# Substrate Memory Plugins

One connection experience, with two supported implementations:

- `plugins/substrate`: the golden Hermes plugin, unchanged from v0.5.0.
- `plugins/substrate-mcp`: a thin remote MCP client for Cowork, Claude Code, Codex,
  and other compatible agents.

## Host support

| Host | Package | Setup and session note |
|---|---|---|
| Hermes | `plugins/substrate` | Device login and verification in the active agent. |
| Claude Code | `plugins/substrate-mcp` | Use the confirmed host interface; do not use the deprecated API-key plugin. |
| Claude Cowork | `plugins/substrate-mcp` | Install through the marketplace, then start a **new session** before tools appear. |
| Codex | `plugins/substrate-mcp` | Use the confirmed host interface; do not use the deprecated API-key plugin. |
| Other MCP clients | `plugins/substrate-mcp` | Use the host's documented remote Streamable HTTP interface. |

See [docs/installation.md](docs/installation.md) for the self-check protocol and
legacy-plugin migration.

## Install

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

For both Hermes and MCP: **Install → Sign in → Review → Approve connection →
Return to the agent → Connected to Substrate.**

Both use **Connect your agent to Substrate** in the browser. Only report
**Connected to Substrate.** after an authenticated memory smoke test succeeds.
The browser's **Connection approved** state is not proof that the agent has
finished connecting. Never request API keys, manual tokens, or client secrets.

The installing agent identifies its current host and automates supported setup.
If Cowork needs manual installation, use **Cowork tab → Customize → Plugins → Personal plugins
→ + → Add marketplace → Add from a repository (GitHub URL)**, then browse and
install `substrate-mcp`. Client-required permission dialogs still need the user.

Follow the single [installation and recovery guide](docs/installation.md).
The endpoint is `https://app.trysubstrate.co/mcp`; use remote **Streamable HTTP**
and browser OAuth. This repository configures clients, not a second server.

Hermes retains automatic durable capture. MCP clients have explicit/best-effort
memory tools, not a universal full-transcript capture guarantee.

## Releases and development

The v0.6.0 GitHub release remains gated separately from code changes and deployment.
Older immutable releases remain available. Hermes installation stays pinned to the
already-published v0.5.0 tag. No release status replaces live connection verification.

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev python -m pytest -q
python3 scripts/check_public_hygiene.py --root .
uv run --frozen --extra dev python scripts/build_release.py
uv run --frozen --extra dev python scripts/build_release.py --check
```

See [COMPATIBILITY.md](COMPATIBILITY.md), [docs/releasing.md](docs/releasing.md),
and [SECURITY.md](SECURITY.md).
