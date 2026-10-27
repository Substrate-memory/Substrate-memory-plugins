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

The order is fixed: approve the connection, install the hooks, the user approves
the hooks in `/hooks`, a new session, then "continue".

1. **Connected already?** Call `memory_search` with a short, non-secret query.
   Any successful result, even empty: say **Connected to Substrate.**, run
   `SYNC --record-connected`, and go to step 5.
2. **Not installed?** ChatGPT/Codex app: Plugins → add the marketplace from the
   GitHub repo `Substrate-memory/Substrate-memory-plugins` → install
   **Substrate Memory** → new chat. Codex CLI:
   `codex plugin marketplace add Substrate-memory/Substrate-memory-plugins` and
   `codex plugin add substrate-codex@substrate-marketplace`. If it is installed, do
   not install it again.
3. **Approve the connection first.** The hooks and the sign-in call the
   `substrate-memory` server by name. If `codex mcp list` does not show
   `substrate-memory` (a local plugin install in Codex 0.154 does not register it),
   run `codex mcp add substrate-memory --url <the url in this plugin's mcp.json>`; this
   is the endpoint in `mcp.json`. Then run `codex mcp list --json`. If the
   `substrate-memory` row's `auth_status` is `not_logged_in`, or `memory_search` reports
   an expired or missing sign-in, say one sentence: "Your Substrate sign-in needs
   renewing; here is the link." Then run `codex mcp login substrate-memory` with network
   permission. Always log in this way, even after `codex mcp add` (that add does not
   store a sign-in from inside a session). The command prints the approval link. Paste that full link into your reply as plain text, and
   say: *Open this link, sign in and choose **Approve connection**. I'll continue
   automatically.* The user cannot see the terminal, so paste the link again whenever
   they say they cannot see it. The command returns once they approve; then rerun
   `memory_search` in the same turn. Never mention ports, callback addresses, or
   remote machines unless the user reports an error page. Never ask for a token or
   key. A bare "continue"/"done"/"ok" during setup just means carry on; don't
   comment on it.
4. **Same reply: install the Substrate hooks.** Run
   `python3 "${PLUGIN_ROOT}/scripts/codex_user_hooks.py" install` now, right after sign-in and before anything else the user is
   asked to do. It merges the Substrate hook entries into the user's Codex hook
   file (`$CODEX_HOME/hooks.json`, default `~/.codex/hooks.json`), leaves every
   other entry alone, keeps a one-time backup, and is safe to run again. Its
   JSON output says `"changed"`. If it says `"skipped"` (Codex already runs the
   plugin's own hooks), add nothing about hooks.
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
6. **Last line, only if step 4 said `"changed"`:** *Substrate hooks were added to
   your Codex config. Approve them once in /hooks, then start a new session and
   say continue.* In the new session the plugin's offer hook carries setup on by
   itself, so "continue" is enough. Until they approve, Codex skips automatic
   capture and recall still works when asked.

**Connection approved** is not **Connected to Substrate.**; only a successful
`memory_search` is.

## Disconnect

To disconnect, or before uninstalling the plugin, run `HOOKS remove` first. It
removes exactly the Substrate entries (and their approval records in
`config.toml`), and nothing else. Then say: *Substrate hooks were removed from
your Codex config.* `codex plugin remove substrate-codex` does not remove them.
