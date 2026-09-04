# Complete Substrate setup for Claude Cowork

No manual credential step is required. The plugin runs device onboarding by
itself on the first user prompt or first `memory_search` call: it shows a
browser approval link through the agent, and after approval the tenant-scoped
key is stored privately in the active home's `.env` plus the owner-only token
file. Never ask the user to paste a key into chat.

## Required behavior for the installing agent

Treat "Install the memory plug-in at
https://github.com/Substrate-memory/Substrate-memory-plugins" as approval to
install and configure the current Substrate plugin for the active Cowork home.
Do not ask the user for repository paths, commands, commit hashes, API keys,
or TLS workarounds.

1. Confirm `claude --version` runs (Claude Code CLI 2.x shares the Cowork
   plugin surface). Do not upgrade anything automatically.
2. Add this repository as a marketplace and install the plugin. The
   repository root is the marketplace source (it ships
   `.claude-plugin/marketplace.json`, which already lists `substrate-cowork`):

   ```sh
   claude plugin marketplace add <path-to-checkout>
   claude plugin install substrate-cowork@substrate-marketplace -y
   ```

   Replace `<path-to-checkout>` with the local clone path. The
   `@substrate-marketplace` suffix disambiguates the plugin and `-y` accepts
   the install prompt so the commands run unattended.
   Once the root `.claude-plugin/marketplace.json` is merged and pushed
   upstream, the remote form also works:

   ```sh
   claude plugin marketplace add https://github.com/Substrate-memory/Substrate-memory-plugins
   claude plugin install substrate-cowork@substrate-marketplace -y
   ```

   Do not use the `Owner/Repo` shorthand
   (`claude plugin marketplace add Substrate-memory/Substrate-memory-plugins`)
   or the plugin subdirectory (`./plugins/claude-cowork`) as the marketplace
   path: the shorthand resolves to the upstream repo, whose tree currently
   ships no marketplace manifest, and the subdirectory contains no
   `.claude-plugin/marketplace.json` — both fail with rc 1
   ("Marketplace file not found").

3. Run `claude plugin validate` against the installed plugin directory. Stop
   without enabling if validation fails.
4. Optionally, to connect during installation instead of on first use, run:

   ```sh
   config_home="${SUBSTRATE_HOME:-$CLAUDE_CONFIG_DIR:-$HOME/.claude}"
   python3 "<plugin-dir>/setup.py" --home "$config_home"
   ```

   (`--home` is the Cowork name for the Hermes `--hermes-home` flag; both are
   accepted.)

   If it prints `verification_uri_complete`, show that exact URL to the user
   and ask them to approve it in a browser. Do not expose or request an
   access token.
5. Enable the plugin and let device onboarding run on first use if step 4 was
   skipped. When a `verification_uri_complete` link appears (from
   `memory_search` or the next prompt), present that exact clickable URL to
   the user. The user approves in their browser; the agent must never approve
   it for them or ask for a pasted key.
6. Verify `memory_search` in a new turn. Report success only when
   `memory_search` reaches the authenticated Substrate API. Fail closed and
   report the exact safe error class otherwise.

Rollback: `claude plugin disable substrate-cowork` (or `claude plugin uninstall
substrate-cowork`), then remove the marketplace if desired. No system files are
touched; per-home state lives under `<home>/substrate/` and the
`SUBSTRATE_API_KEY` line in `<home>/.env`.

Do not disable TLS verification, install a private certificate, or inspect a
different home. The plugin supplements system trust only with the bundled
public ISRG roots.
