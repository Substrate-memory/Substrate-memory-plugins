---
name: substrate-connect
description: Sign in to Substrate, verify, install the Substrate hooks in the Codex config, and offer to import past Codex conversations, in one go.
---

# Substrate connect

Run this end to end. Pause only for the user's browser approval and their import
choice; never end the turn just because a step finished.

`SYNC` means `python3 "${PLUGIN_ROOT}/scripts/substrate_sync.py" --host codex`
(`python` if `python3` is missing; if `${PLUGIN_ROOT}` is not filled in, the
plugin is under `$CODEX_HOME/plugins/cache/*/substrate-codex/*/`, default
`~/.codex`). `HOOKS` means `python3 "${PLUGIN_ROOT}/scripts/codex_user_hooks.py"`
with the same fallbacks. Ignore any `[substrate] Offer this once` note: the plugin
handles the import.

The order is fixed: install the hooks, start the sign-in and end the turn with the
link and one approval instruction. The user approves the link and the hooks, then
starts a new session. That session confirms the connection.

1. **Connected already?** Call `memory_search` with a short, non-secret query.
   Any successful result, even empty: say **Connected to Substrate.**, run
   `SYNC --record-connected`, and go to step 5.
2. **Not installed?** ChatGPT/Codex app: Plugins → add the marketplace from the
   GitHub repo `Substrate-memory/Substrate-memory-plugins` → install
   **Substrate Memory** → new chat. Codex CLI:
   `codex plugin marketplace add Substrate-memory/Substrate-memory-plugins` and
   `codex plugin add substrate-codex@substrate-marketplace`. If it is installed, do
   not install it again.
3. **Install the hooks first.** Run `codex mcp add substrate-memory --url
   <the url in this plugin's mcp.json>` if `codex mcp list` does not show
   `substrate-memory`. Then run `python3 "${PLUGIN_ROOT}/scripts/codex_user_hooks.py" install`
   (safe to run again; it merges only the
   Substrate entries into `$CODEX_HOME/hooks.json`, default `~/.codex/hooks.json`, and
   keeps a one-time backup). The hook command runs a stable entry point in
   `$CODEX_HOME/substrate/`, so plugin updates do not need a new approval. Until
   sign-in the hooks stay quiet: they only read the connection.
4. **Start the sign-in and end the turn.** Run
   `python3 "${PLUGIN_ROOT}/scripts/substrate_sync.py" --host codex --signin-detach` with
   escalated permissions (network and a local listener). It starts `codex mcp login
   substrate-memory` in its own session, so the approval still works after this turn
   ends, and prints JSON. If its `"status"` is `"connected"`, go to step 5. Otherwise
   paste the full link from its `"link"` on its own line, end your turn, and add this one
   approval instruction, word for word:
   *Open this link and choose Approve connection. Then approve Substrate's hooks: in the
   Codex app, in its hooks settings; in the terminal, /hooks. Then start a new session.*
   Add nothing else: no second approval instruction, no "continue", no waiting. If the
   status is `"error"`, say the message and ask the user to try again. Never mention
   ports, callback addresses or remote machines unless the user reports an error page.
   Never ask for a token or key.
5. **The import offer, once.** If this turn has a `[substrate] Import offer pending` note,
   follow that note: it is the one offer, made after the connection checked out, and this
   step is done. Otherwise run `SYNC --preview`. If `sessions` is 0 and `already_imported` is
   above 0, check `memory_import_status` with no arguments: if it reports no imports for this
   account (`no_imports`, or no batch), say: *The conversations I imported earlier from this
   computer are not in this Substrate account. Import them into this account now? **Import all**
   / **Not now***, and on yes use `$substrate-import` with Import all (it re-sends them). Do not
   blame the import or the server before that check. Otherwise, if `decision` is set or
   `sessions` is 0, say nothing about the import now. The offer comes once, on the user's next
   message.
6. **New session.** The first reply of the next session confirms the connection with
   "Connected to Substrate." after `memory_search` works, and the import offer follows
   once. Until the user approves the hooks, Codex skips automatic capture; recall works
   when asked.

**Connection approved** is not **Connected to Substrate.**; only a successful
`memory_search` is.

## Disconnect

To disconnect, or before uninstalling the plugin, run `HOOKS remove` first. It
removes exactly the Substrate entries (and their approval records in
`config.toml`), and the stable entry point, and nothing else. Then say: *Substrate
hooks were removed from your Codex config.* `codex plugin remove substrate-codex`
does not remove them.
