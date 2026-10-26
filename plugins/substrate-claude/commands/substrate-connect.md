---
description: Sign in to Substrate, verify, and offer to import past conversations, in one go.
allowed-tools: Bash(python3 *) Bash(python *)
---

Run this end to end. Pause only for the user's browser approval and their import
choice. Never end your turn just because a step finished.

`SYNC` means `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/substrate_sync.py" --host claude`
(use `python` if `python3` is missing).

The check tool is `mcp__plugin_substrate-claude_substrate-memory__memory_search`
(load it with ToolSearch if it is deferred). Only that plugin server counts.
Tools named `mcp__claude_ai_*` come from a claude.ai connector: a successful call to
them does not mean this plugin is connected, so never use them as the check.

1. **Connected already?** Call the plugin's `memory_search` with a short, non-secret
   query. Any successful result from the plugin's tool, even empty, counts: say
   **Connected to Substrate.**, run `SYNC --record-connected`, and go to step 4.
2. **Installed but not signed in.** "MCP server … not connected" hook errors, or the
   plugin's tool reporting "needs authentication", mean the plugin is installed but
   not signed in. Do not install another copy, and do not say Connected.
3. **Sign in now.** Call the Substrate `authenticate` tool (named like
   `mcp__plugin_substrate-claude_substrate-memory__authenticate`) and tell the
   user: *Open this link, sign in and choose **Approve connection**. I'll
   continue automatically.* Don't relay the tool's notes about localhost errors
   or pasting URLs; mention pasting the address-bar URL only if the user says
   the page showed an error (remote or SSH machines). Do not end the turn: run
   `SYNC --pause 15`, call the plugin's `memory_search` again (via ToolSearch if
   deferred), and repeat up to 20 times. The tools appear mid-turn once the user
   approves. Only a successful call to the plugin's tool means connected. If it
   never works, ask the user to say "done" after approving, or to finish
   Authenticate in `/mcp` (plugin:substrate-claude:substrate-memory), then say
   continue. Never ask for a token or key.
   A bare "continue", "done", "ok" or "approved" during setup just means carry
   on; don't comment on it.
4. **Same reply: offer the import.** Run `SYNC --preview`. If `decision` is set
   or `sessions` is 0, stop here. Otherwise ask once, in plain words, with the
   count and range from its output:
   *I found N past conversations on this computer (T turns, FIRST to LAST).
   Import them into Substrate? **Import all** / **Let me pick** / **Not now***
5. Act on the answer with `/substrate-claude:substrate-import`.

**Connection approved** in the browser is not **Connected to Substrate.**; only a
successful call to the plugin's `memory_search` is.
