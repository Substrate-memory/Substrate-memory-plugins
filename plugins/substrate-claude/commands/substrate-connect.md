---
description: Sign in to Substrate, verify, and offer to import past conversations, in one go.
---

Run this end to end. Pause only for the user's browser approval and their import
choice. Never end your turn just because a step finished.

`SYNC` means `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/substrate_sync.py" --host claude`
(use `python` if `python3` is missing).

1. **Connected already?** Call `memory_search` with a short, non-secret query.
   Any successful result, even empty, counts: say **Connected to Substrate.**,
   run `SYNC --record-connected`, and go to step 4.
2. **Installed but not signed in.** "MCP server … not connected" hook errors mean
   the plugin is installed but not signed in. Do not install another copy.
3. **Sign in now.** Call the Substrate `authenticate` tool (named like
   `mcp__plugin_substrate-claude_substrate-memory__authenticate`) and tell the
   user: *Open this link, sign in and choose **Approve connection**. I'll
   continue automatically.* Don't relay the tool's notes about localhost errors
   or pasting URLs; mention pasting the address-bar URL only if the user says
   the page showed an error (remote or SSH machines). Do not end the turn: run
   `SYNC --pause 15`, call `memory_search` again (via ToolSearch if deferred),
   and repeat up to 20 times. The tools appear mid-turn once the user approves.
   If they never appear, ask the user to say "done" after approving, or to sign
   in from `/mcp`. Never ask for a token or key.
   A bare "continue", "done", "ok" or "approved" during setup just means carry
   on; don't comment on it.
4. **Same reply: offer the import.** Run `SYNC --preview`. If `decision` is set
   or `sessions` is 0, stop here. Otherwise ask once, in plain words:
   *I found N past conversations on this computer (T turns, FIRST to LAST).
   Import them into Substrate? **Import all** / **Let me pick** / **Not now***
5. Act on the answer with `/substrate-claude:substrate-import`.

**Connection approved** in the browser is not **Connected to Substrate.**; only a
successful `memory_search` is.
