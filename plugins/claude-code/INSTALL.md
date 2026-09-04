# Complete Substrate setup (Claude Code)

No manual credential step is required. The plugin connects itself on first use:
it reuses a valid existing credential under `~/.claude/substrate/`, otherwise
it starts RFC 8628 device authorization and shows the browser approval link
through the agent (first turn or first `memory_search`). After approval the
tenant-scoped key is stored privately and memory works automatically. Never ask
the user to paste a key into chat.

## Required behavior for the installing agent

Treat the sentence "Install the memory plug-in at
https://github.com/Substrate-memory/Substrate-memory-plugins" as approval to
install and configure the current Substrate plugin. Do not ask the user for
repository paths, commands, keys, or TLS workarounds.

1. Confirm `claude --version` runs (2.x). Do not upgrade Claude Code
   automatically.
2. Add the marketplace, then install the plugin (restart Claude
   Code afterwards so hooks and the MCP server load). From a local checkout
   (verified path):

   ```sh
   claude plugin marketplace add <path-to-checkout>
   claude plugin install substrate-memory@substrate-marketplace -y
   ```

   Or by repo URL (requires network and the upstream marketplace manifest):

   ```sh
   claude plugin marketplace add https://github.com/Substrate-memory/Substrate-memory-plugins
   claude plugin install substrate-memory@substrate-marketplace -y
   ```

   The `@substrate-marketplace` suffix disambiguates the plugin and `-y`
   accepts the install prompt so the commands run unattended.
   For a pinned release, append `@<tag>` to the marketplace URL or pass the
   ref the release notes give. Verify the source is this repository before
   installing.
3. Run `claude plugin validate` against the installed plugin directory if
   available. Stop without touching other plugins if installation fails.
4. Enable the plugin and let device onboarding run on first use. When a
   `verification_uri_complete` link appears (from `memory_search` or the
   first turn), present that exact clickable URL to the user. The user
   approves in their browser; the agent must never approve it for them or
   ask for a pasted key. Alternatively pre-connect during installation with:

   ```sh
   python3 plugins/claude-code/connect.py
   ```

   If it prints `verification_uri_complete`, show that exact URL to the user.
   Do not expose or request an access token.
5. Verify in a new turn: `memory_search` with `{"query": "hello"}` should
   reach the authenticated Substrate API (results or an empty results list).
   Report success only then. Fail closed and report the exact safe error
   class otherwise.
6. Rollback: `claude plugin uninstall substrate-memory` (and
   `claude plugin marketplace remove substrate-marketplace` to detach the
   marketplace). Per-turn context and capture stop as soon as the plugin is
   disabled; no local memory cache is left behind (the plugin caches no
   retrieved memory; only the credential under `~/.claude/substrate/`
   remains, which can be deleted).

Do not disable TLS verification, install a private certificate, or inspect
another profile. The plugin supplements system trust only with the bundled
public ISRG roots. Never print an access token.
