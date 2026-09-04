# Installing the Substrate memory plugin for Codex CLI

Required behavior for the installing agent. Treat

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

as approval to install and configure the current Substrate plugin for the
active Codex profile. Do not ask the user for repository paths, commands,
API keys, or TLS workarounds.

## Steps

1. Confirm `codex --version` runs (verified on `codex-cli 0.144.5`; newer
   0.14x releases use the same manifest layout). Do not upgrade Codex
   automatically.
2. Fetch this repository without committing anything:
   `git clone --filter=blob:none --sparse`
   `https://github.com/Substrate-memory/Substrate-memory-plugins.git`,
   then `git sparse-checkout set plugins/codex` inside the clone.
3. Run the installer from the checkout (idempotent, non-interactive):
   `sh plugins/codex/install.sh`. It creates the active Codex home when
   missing (mode 0700) and registers the MCP server with the native
   command `codex mcp add substrate -- python3 <checkout>/plugins/codex/server.py`.
   Stop without changing anything else if the installer fails.
4. Register the plugin content (hooks + memory skill) with the native
   marketplace mechanism. The repository root ships
   `.agents/plugins/marketplace.json` (marketplace `substrate-memory`,
   entry `substrate` → `./plugins/codex`), so from the checkout directory
   run:
   ```sh
   codex plugin marketplace add "$PWD"
   codex plugin add substrate@substrate-memory
   ```
   (The same two commands work against the repo Git URL; do not add
   `--sparse plugins/codex` alone — the sparse set omits the root manifest
   and the add fails with `marketplace root does not contain a supported
   manifest`.) Confirm with `codex plugin list --json` (the entry shows
   `"installed": true, "enabled": true`; verified against `codex-cli
   0.144.5` in a sandbox `CODEX_HOME`). If only the `plugins/codex`
   directory is available instead of a full checkout, build the equivalent
   local marketplace root:
   ```sh
   checkout="$PWD"
   mp="$HOME/.substrate-marketplace"
   mkdir -p "$mp/.agents/plugins" "$mp/plugins"
   ln -sfn "$checkout/plugins/codex" "$mp/plugins/codex"
   ```
   then write `$mp/.agents/plugins/marketplace.json` with the same shape
   as the root manifest (`name: substrate-memory`, plugin `substrate` →
   `./plugins/codex`, `AVAILABLE` / `ON_USE`), then run
   `codex plugin marketplace add "$mp"` plus
   `codex plugin add substrate@substrate-memory`.
   If plugin installation fails on this Codex version, keep the MCP
   registration from step 3 and continue: the memory tools keep working,
   and the agent can call `memory_search` directly.
5. Let device onboarding run on first use. When a
   `verification_uri_complete` link appears (from `memory_search` or the
   first turn's injected context), present that exact clickable URL to the
   user. The user approves in their browser; the agent must never approve
   it for them or ask for a pasted key.
6. The plugin stores the tenant-scoped key privately in the active Codex
   home (`~/.codex/substrate/credentials/access-token`, owner-only) and in
   the profile `.env` as `SUBSTRATE_API_KEY`, then retries automatically.
   No manual credential migration is required.
7. Verify in a new turn: list MCP servers with `codex mcp get substrate`
   and call `memory_search`. Report success only after `memory_search`
   reaches the authenticated Substrate API. Fail closed and report the
   exact safe error class otherwise.

TLS verification must remain enabled. The plugin supplements the host
trust store with unmodified public ISRG Root X1 and X2 certificates. It
never trusts a replacement leaf certificate or a private certificate.

## Rollback

```sh
codex mcp remove substrate
```

Removing the MCP registration stops all memory traffic. Delete the sparse
checkout afterwards. Hook-injected context stops as soon as the plugin is
removed or uninstalled; spooled capture files under
`~/.codex/substrate/spool-codex/` are bounded (at most 64 files, expired
unsent after 7 days) and safe to delete.
