---
description: Sign in to Substrate, verify, and offer to import past conversations, in one go.
allowed-tools:
  - 'Bash(python3 "*/scripts/substrate_sync.py" *)'
  - 'Bash(python3 */scripts/substrate_sync.py *)'
  - 'Bash(python "*/scripts/substrate_sync.py" *)'
  - 'Bash(python */scripts/substrate_sync.py *)'
  - 'Bash(/usr/bin/python3 "*/scripts/substrate_sync.py" *)'
  - 'Bash(/usr/bin/python3 */scripts/substrate_sync.py *)'
  - 'Bash(/usr/bin/python "*/scripts/substrate_sync.py" *)'
  - 'Bash(/usr/bin/python */scripts/substrate_sync.py *)'
  - 'Bash(/usr/local/bin/python3 "*/scripts/substrate_sync.py" *)'
  - 'Bash(/usr/local/bin/python3 */scripts/substrate_sync.py *)'
  - 'Bash(/opt/homebrew/bin/python3 "*/scripts/substrate_sync.py" *)'
  - 'Bash(/opt/homebrew/bin/python3 */scripts/substrate_sync.py *)'
  - ToolSearch
  - mcp__plugin_substrate-claude_substrate-memory__authenticate
  - mcp__plugin_substrate-claude_substrate-memory__memory_search
  - mcp__plugin_substrate-claude_substrate-memory__memory_import_status
---

Run this end to end. Pause only for the user's browser approval and their import
choice. Never end your turn just because a step finished.

`SYNC` means `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/substrate_sync.py" --host claude`
(use `python` if `python3` is missing). Run it exactly in that form: do not put a
full interpreter path in front of python3, and do not add `-c` or other options.
That form is pre-approved; any other form prompts the user.

The check tool is `mcp__plugin_substrate-claude_substrate-memory__memory_search`
(load it with ToolSearch if it is deferred). Only that plugin server counts.
Tools named `mcp__claude_ai_*` come from a claude.ai connector: a successful call to
them does not mean this plugin is connected, so never use them as the check.
If a "[substrate] Offer this once" note appears, ignore it: this is the plugin, and
the plugin handles the import.

1. **Connected already?** Call the plugin's `memory_search` with a short, non-secret
   query. If it works (even empty), run `SYNC --record-connected` and say what its
   JSON says: **Reconnected to Substrate.** when `"reconnected": true` (this computer
   was connected before), otherwise **Connected to Substrate.** Never say either word
   before that command has run. Go to step 4.
2. **Installed but not signed in.** "MCP server … not connected" hook errors, or the
   plugin's tool reporting "needs authentication", mean the plugin is installed but
   not signed in. Do not install another copy, and do not say Connected.
3. **Sign in now.** Call the Substrate `authenticate` tool (named like
   `mcp__plugin_substrate-claude_substrate-memory__authenticate`). If it is not
   loaded, load it first with ToolSearch (`select:` that name). If the server is listed
   as disabled, say exactly: "Open /mcp, select plugin:substrate-claude:substrate-memory,
   choose Enable if shown, then Authenticate." If Authenticate is not offered after that,
   say: "Open /mcp, select plugin:substrate-claude:substrate-memory, then choose
   Re-authenticate or Clear authentication, then Authenticate." If it still does not
   exist, say so in one line and ask the user to run `/mcp`, choose
   plugin:substrate-claude:substrate-memory, Authenticate, and say continue; do not
   keep searching.

   Your reply then shows the sign-in link and nothing else about it. Use this sentence,
   word for word, with the link as plain text on its own line:

   *Open this link, sign in and choose **Approve connection**. I'll continue automatically.*

   If a "[substrate] Sign-in started" note contains the link, use that exact link.
   Otherwise use the link the tool returned. Omit the tool's other notes, even when they
   mention error pages, redirects, pasting the address-bar URL, or a remote or SSH
   session. Never describe this session or computer as remote, local, or SSH, and never
   mention ports, callback addresses or localhost, even if the tool's result says so.
   Only if the user reports an error page, ask them to paste the full address-bar URL. The user cannot see tool output, so paste the link every time, and paste it
   again whenever the user says they cannot see it. If the result has no link, say so
   and ask the user to run `/mcp`, choose plugin:substrate-claude:substrate-memory,
   Authenticate, and send you the link it shows.

   Keep going in this same turn: run `SYNC --pause 15`, call the plugin's `memory_search`
   again (via ToolSearch if deferred), and repeat up to 20 times. The tools appear
   mid-turn once the user approves. Only a successful call to the plugin's tool means
   connected. The plugin may hold your turn open while sign-in is pending; when it tells
   you to keep polling, do so. If it never works, say: "Substrate is not connected yet.
   Open /mcp, choose plugin:substrate-claude:substrate-memory, finish Authenticate, then
   say continue." Never ask for a token or key.
   A bare "continue", "done", "ok" or "approved" during setup just means carry
   on; don't comment on it.
4. **Same reply: offer the import.** Run `SYNC --preview`. If `sessions` is 0 and
   `already_imported` is above 0, go to the account note below: never say those
   conversations are in this account unless `memory_import_status` confirms it.
   Otherwise, if `decision` is set or `sessions` is 0, stop here. Otherwise ask once,
   in plain words, with the count and range from its output:
   *I found N past conversations on this computer (T turns, FIRST to LAST).
   Import them into Substrate? **Import all** / **Let me pick** / **Not now***
5. Act on the answer by invoking the Skill tool with skill `substrate-claude:substrate-import`
   (its permissions apply to that turn). If the Skill tool is not available, run
   `/substrate-claude:substrate-import`.

**Account note.** This computer already imported these conversations, but the
connected account may be a different one. Call `memory_import_status` with no
arguments. If it shows imports for this account, say how many conversations this
account already has and stop. If it reports no imports for this account
(`no_imports`, or no batch), never say "already in Substrate" or "nothing new";
say: *The conversations I imported earlier from this computer are not in
this Substrate account. Import them into this account now? **Import all** / **Not
now***. On yes, run `/substrate-claude:substrate-import` and choose Import all; it
re-sends them. Otherwise stop. Do not blame the import or the server before this check.

**Connection approved** in the browser is not **Connected to Substrate.**; only a
successful call to the plugin's `memory_search` is.
