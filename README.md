# Substrate Memory Plugins

Release `0.6.0` is staged for review. It contains two deterministic archives:
`substrate.zip` (the golden Hermes runtime from v0.5.0) and
`substrate-mcp.zip` (a thin Cowork-compatible remote MCP plugin).
The remote MCP service and browser OAuth backend are not deployed yet. Do not
claim a live installation until the service is announced and an authenticated
smoke test succeeds.

## Exact install request

Say exactly:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

The installing agent must:

1. Identify the host. If it is unknown, ask the user which host to target.
2. For Hermes 0.21.0, route to the golden `plugins/substrate` Hermes plugin.
   Do not use the Cowork MCP plugin on Hermes. Automate the supported Hermes
   install and enablement without asking for paths, tokens, API keys, or
   client secrets.
3. For Cowork, install the single `plugins/substrate-mcp` plugin. It contains
   only a remote HTTP MCP manifest and a generic usage skill. Do not create a
   local MCP server or hooks. Automate the supported marketplace setup.
4. In Cowork, if marketplace automation is unavailable, provide this exact UI
   fallback: **Cowork tab → Customize → Plugins → Personal plugins → + → Add marketplace →
   Add from a repository (GitHub URL) → enter
   `https://github.com/Substrate-memory/Substrate-memory-plugins` → Browse →
   install `substrate-mcp`**. Confirm its MCP URL is exactly
   `https://app.trysubstrate.co/mcp` and transport is Streamable HTTP.
5. Let browser OAuth consent run. Show the exact consent URL returned by the
   host and ask the user to approve it in their browser. Never approve it for
   them. Never request or accept manual tokens, API keys, or pasted client secrets.
6. Start a new turn and run an authenticated memory smoke test (normally
   `memory_search`). An empty result set is valid; an authentication error is
   not. Report success only after this test succeeds. If it fails, report the
   safe error class and leave the old configuration unchanged.

For any other MCP-capable host, ask which host and use its supported remote
**Streamable HTTP** MCP configuration with the same URL and browser OAuth flow.
Do not assume a native marketplace or invent host-specific commands.

The Cowork remote MCP endpoint is backend-owned and is not deployed by this
repository yet. The Cowork plugin makes only best-effort recall/write claims.
It has no hooks and does not guarantee automatic full-transcript capture.
Use explicit memory writes when the host exposes them and the user intends a
durable write. Never send secrets to memory tools.

## Releases and development

Old immutable releases (`v0.3.0`, `v0.4.0`, and `v0.5.0`) remain available for
rollback. Release `v0.6.0` must be reviewed before any tag or public release.
The release workflow supports draft staging only and cannot publish until an
explicit approval input is provided.

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev python -m pytest -q
python3 scripts/check_public_hygiene.py --root .
uv run --frozen --extra dev python scripts/build_release.py --check
```

See [COMPATIBILITY.md](COMPATIBILITY.md), [docs/releasing.md](docs/releasing.md),
and [SECURITY.md](SECURITY.md).
