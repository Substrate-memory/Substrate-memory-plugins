---
description: Sign in to Substrate, verify, and offer to import past conversations, in one go.
allowed-tools: Bash(python3 "*/scripts/substrate_sync.py" *) Bash(python "*/scripts/substrate_sync.py" *)
---

Run this end to end. Pause only for the user's browser approval and their import
choice. Never end your turn just because a step finished.

`SYNC` means `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/substrate_sync.py" --host claude`
(use `python` if `python3` is missing).

The check tool is `mcp__plugin_substrate-claude_substrate-memory__memory_search`
(load it with ToolSearch if it is deferred). Only that plugin server counts.
Tools named `mcp__claude_ai_*` come from a claude.ai connector: a successful call to
them does not mean this plugin is connected, so never use them as the check.
If a "[substrate] Offer this once" note appears, ignore it: this is the plugin, and
the plugin handles the import.

1. **Connected already?** Call the plugin's `memory_search` with a short, non-secret
   query. Any successful result, even empty, counts: say
   **Connected to Substrate.**, run `SYNC --record-connected`, and go to step 4.
2. **Installed but not signed in.** "MCP server … not connected" hook errors, or the
   plugin's tool reporting "needs authentication", mean the plugin is installed but
   not signed in. Do not install another copy, and do not say Connected.
3. **Sign in now.** Call the Substrate `authenticate` tool (named like
   `mcp__plugin_substrate-claude_substrate-memory__authenticate`). In your reply,
   paste the full sign-in URL it returns, as plain text, then say: *Open this link,
   sign in and choose **Approve connection**. I'll continue automatically.* The user
   cannot see tool output, so paste the link every time, and paste it again whenever
   the user says they cannot see it. If the result has no link, say so and ask the
   user to run `/mcp`, choose plugin:substrate-claude:substrate-memory, Authenticate,
   and send you the link it shows.
   Never mention ports, callback addresses, localhost, or remote or SSH machines
   unless the user reports an error page; only then ask them to paste the full
   address-bar URL.
   Do not end the turn: run `SYNC --pause 15`, call the plugin's `memory_search`
   again (via ToolSearch if deferred), and repeat up to 20 times. The tools appear
   mid-turn once the user approves. Only a successful call to the plugin's tool means
   connected. If it never works, ask the user to say "done" after approving, or to
   finish Authenticate in `/mcp` (plugin:substrate-claude:substrate-memory), then say
   continue. Never ask for a token or key.
   A bare "continue", "done", "ok" or "approved" during setup just means carry
   on; don't comment on it.
4. **Same reply: offer the import.** Run `SYNC --preview`. If `sessions` is 0 and
   `already_imported` is above 0, go to the account note below. Otherwise, if
   `decision` is set or `sessions` is 0, stop here. Otherwise ask once, in plain
   words, with the count and range from its output:
   *I found N past conversations on this computer (T turns, FIRST to LAST).
   Import them into Substrate? **Import all** / **Let me pick** / **Not now***
5. Act on the answer with `/substrate-claude:substrate-import`.

**Account note.** This computer already imported these conversations, but the
connected account may be a different one. Call `memory_import_status` with no
arguments. Only if it reports no imports for this account (`no_imports`, or no
batch), say: *The conversations I imported earlier from this computer are not in
this Substrate account. Import them into this account now? **Import all** / **Not
now***. On yes, run `/substrate-claude:substrate-import` and choose Import all; it
re-sends them. Otherwise stop. Do not blame the import or the server before this check.

**Connection approved** in the browser is not **Connected to Substrate.**; only a
successful call to the plugin's `memory_search` is.
