---
name: substrate-connect
description: Sign in to Substrate, verify, install the Substrate hooks in the Codex config, and offer to import past Codex conversations, in one go.
---

# Substrate connect

Run this end to end. Pause only for the user's browser approval and their
import choice; never end the turn just because a step finished.

`SYNC` means `python3 "${PLUGIN_ROOT}/scripts/substrate_sync.py" --host codex`
(`python` if `python3` is missing; if `${PLUGIN_ROOT}` is not filled in, the
plugin is under `$CODEX_HOME/plugins/cache/*/substrate-codex/*/`, default
`~/.codex`). A `[substrate] Import offer pending` note gives the exact command.
`HOOKS` means `python3 "${PLUGIN_ROOT}/scripts/codex_user_hooks.py"` with the
same fallbacks. A `[substrate] Offer this once` note is not for this plugin:
ignore it, because the plugin handles the import.

The order is fixed: install the hooks, start the sign-in and end the turn with the
link, then the user approves the link, trusts the hooks in `/hooks`, and starts a new
session with `/new`. That new session says "Connected to Substrate." on its own.

1. **Connected already?** Call `memory_search` with a short, non-secret query.
   Any successful result, even empty: say **Connected to Substrate.**, run
   `SYNC --record-connected`, and go to step 5.
2. **Not installed?** ChatGPT/Codex app: Plugins → add the marketplace from the
   GitHub repo `Substrate-memory/Substrate-memory-plugins` → install
   **Substrate Memory** → new chat. Codex CLI:
   `codex plugin marketplace add Substrate-memory/Substrate-memory-plugins` and
   `codex plugin add substrate-codex@substrate-marketplace`. If it is installed, do
   not install it again.
3. **Register and install the hooks first.** Run `codex mcp add substrate-memory --url
   <the url in this plugin's mcp.json>` if `codex mcp list` does not show
   `substrate-memory`. Then run `python3 "${PLUGIN_ROOT}/scripts/codex_user_hooks.py" install`
   (safe to run again; it merges only the Substrate entries into `$CODEX_HOME/hooks.json`,
   default `~/.codex/hooks.json`, and keeps a one-time backup). Until sign-in the hooks
   stay quiet: they only read the connection.
4. **Start the sign-in and end the turn.** Run
   `python3 "${PLUGIN_ROOT}/scripts/substrate_sync.py" --host codex --signin-detach` with
   escalated permissions (network and a local listener). It starts `codex mcp login
   substrate-memory` in its own session, so the approval still works after this turn
   ends, and prints JSON. If its `"status"` is `"connected"`, go to step 5. Otherwise paste
   the full link from its `"link"` on its own line, then one line:
   *Open this link and choose Approve connection. Then run /hooks, trust Substrate's
   hooks, and start a new session with /new.* Do not ask anything else and do not wait
   for a reply. If the status is `"error"`, say the message and ask the user to try again.
   Never mention ports, callback addresses or remote machines unless the user reports an
   error page. Never ask for a token or key.

5. **Same reply: offer the import.** Run `SYNC --preview`. If `sessions` is 0 and
   `already_imported` is above 0, check `memory_import_status` with no arguments:
   if it reports no imports for this account (`no_imports`, or no batch), say:
   *The conversations I imported earlier from this computer are not in this
   Substrate account. Import them into this account now? **Import all** / **Not
   now***, and on yes use `$substrate-import` with Import all (it re-sends them).
   Otherwise, if `decision` is set or `sessions` is 0, stop. Otherwise ask once, in
   plain words:
   *I found N past conversations on this computer (T turns, FIRST to LAST).
   Import them into Substrate? **Import all** / **Let me pick** / **Not now***
   Act on the answer with `$substrate-import`. Do not blame the import or the
   server before the `memory_import_status` check.
6. **New session.** The SessionStart hook says "Connected to Substrate." and offers the import
   in the first reply of the next session, once the user has approved the link, trusted the
   hooks in /hooks and said /new. If the sign-in is still pending there, that hook shows the
   link again. Until the user trusts the hooks, Codex skips automatic capture; recall works
   when asked.

**Connection approved** is not **Connected to Substrate.**; only a successful
`memory_search` is.

## Disconnect

To disconnect, or before uninstalling the plugin, run `HOOKS remove` first. It
removes exactly the Substrate entries (and their approval records in
`config.toml`), and nothing else. Then say: *Substrate hooks were removed from
your Codex config.* `codex plugin remove substrate-codex` does not remove them.
