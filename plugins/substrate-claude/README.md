# Substrate memory for Claude Cowork and Claude Code

One package serves both hosts. It registers a remote Substrate MCP server, a
usage skill, three slash commands, and the hooks that capture turns
automatically. There is no local server and the plugin stores no credentials:
sign-in runs through the host browser flow, and history import uses a
short-lived, import-only ticket that the agent writes to a temporary file and
the upload script deletes after reading it.

## What you get

- In **Cowork** (cloud and local sessions): memory recall on every prompt,
  automatic capture of prompts, tool use, and answers, plus
  `/substrate-claude:substrate-connect`, `/substrate-claude:substrate-sync`,
  and `/substrate-claude:substrate-import`.
- In **Claude Code**: the same skill, commands, and automatic capture, plus
  past-conversation import and catch-up sync through `scripts/substrate_sync.py`.

## How it works

- `UserPromptSubmit` calls `memory_turn_context`: the server opens the pending
  turn and returns recall for this turn as extra context.
- `PostToolUse` calls `memory_capture_tool` for every tool call.
- `Stop` calls `memory_capture_turn` for the main agent. Subagent results are
  captured as the main agent's tool results.
- `PreCompact` calls `memory_session_boundary`. Claude Code allows no MCP tool
  hook at session start or end, so the first turn implies the start and the
  server seals a session after 30 minutes idle.
- A second `UserPromptSubmit` hook runs `scripts/substrate_sync.py --offer-check`
  (a local command, no network): until Substrate is connected it tells the
  agent to finish setup (sign in, verify), and until you have answered the
  import offer once it reminds the agent to offer it. Afterwards it is silent.
- Every hook fails open: a failed memory call never blocks the session.

## Install for Cowork

1. Open **Cowork → Customize → Plugins → Personal plugins → + → Add
   marketplace**.
2. Choose **Add from a repository (GitHub URL)** and enter
   `https://github.com/Substrate-memory/Substrate-memory-plugins`.
3. Choose **Browse**, then install `substrate-claude`.
4. **Start a new session.** Plugin MCP servers register only at session start.
5. In the new session, paste:

   ```text
   Verify my Substrate connection with memory_search and report Connected to Substrate.
   ```

## Install for Claude Code

If `claude plugin list` already shows `substrate-claude@synced` (synced from
claude.ai), do not install another copy: just sign in. Otherwise:

```text
claude plugin marketplace add Substrate-memory/Substrate-memory-plugins
claude plugin install substrate-claude@substrate-marketplace
```

Then type `/reload-plugins` and say "continue": the plugin's hook makes the
agent finish setup on that message.

## Sign-in and import

**Install → Sign in → Review → Approve connection → Connected to Substrate.**

The agent opens **Connect your agent to Substrate** for you. Sign in, review
the connection name and permissions, choose **Approve connection**. The agent
notices the approval by itself, checks it with `memory_search`, says
**Connected to Substrate.**, and in the same reply offers once:

> I found 87 past conversations on this computer (1,204 turns, 3 Mar to 7 Oct
> 2026). Import them into Substrate? **Import all** / **Let me pick** / **Not now**

- **Import all**: everything on this computer except the current conversation
  (already captured live) and anything already imported. The upload runs in the
  background straight from the local script to Substrate with a short-lived
  import-only ticket, so your history never passes through the chat. The agent
  reports progress and ends with `Imported N sessions (T turns): S stored, D
  duplicate, R rejected.`
- **Let me pick**: the agent lists conversations by date and title, not counting
  the ones already imported.
- **Not now**: never asked again. Say "import my past conversations" any time.

Never paste an API key or token into chat.

## What is captured

Prompts, tool calls (names plus bounded, redacted arguments), tool results
(excerpts plus digests), and assistant replies, grouped per turn. Session
boundaries (start, clear, compact, end) seal extraction windows.

## Privacy and redaction

Secrets (API keys, tokens, passwords, `sk_…` values) are redacted on the client
before anything leaves the host, and redacted again on the server. History
import uploads only to the `mcp_url` returned by `memory_import_ticket`. The
agent sends a claim (made by `--new-claim`) with that call; the script exchanges
the claim for the ticket itself, so the ticket never reaches the agent. A server
that still returns the ticket directly gets it through a temporary file, passed
with `--ticket-file` and deleted by the script as soon as it is read. The ticket
never appears in a command line or in the chat; a claim works once and expires
after 10 minutes.
Catch-up sync prints batches for the agent to pass to `memory_import`. Never
send credentials to a memory tool yourself.

## Known limits

- The capture and recall hooks are MCP tool hooks, which Claude Code calls
  directly. When the connection is gone (signed out, or removed on the Agents
  page), Claude Code shows their errors ("Unauthorized", "MCP server not
  connected"). A plugin script cannot silence those; it can only add the one
  quiet note described above when it sees the removal locally.
- The import ticket reaches the model once, as the `memory_import_ticket` result
  (and the agent's file write), so it is part of the session transcript. It is
  60 minutes long, works only for import, and is redacted by Substrate as a
  `sk_…` value. It never appears in a command line.
- Claude Code does not allow MCP tool hooks on `SessionStart`, so there is
  none; the boundary is implied by the first turn instead.
- Use the plugin or the claude.ai Substrate connector, not both. With both
  present and the connector not signed in, Claude Code in print mode
  (`claude -p`, the Agent SDK) dropped the plugin's server at startup, so
  that run got no recall and no capture (seen on Claude Code 2.1.289;
  interactive sessions were unaffected).
- Claude Code allows no MCP tool hook when a session ends, so the server seals
  a session after 30 minutes idle (checked on that account's next request);
  nothing is lost, it becomes memory a little later.
- Cowork cloud sessions cannot run local servers or scripts: everything goes
  through the remote server.

## Troubleshooting

- **Tools absent after install:** start a new session (Cowork) or run
  `/reload-plugins` (Claude Code), then rerun the self-check prompt.
- **`[substrate] … not saved yet` line:** run
  `/substrate-claude:substrate-sync` to send the missing turns.
- **Authorization required:** follow the browser link, approve, and rerun the
  smoke test. **Connection approved** is not **Connected to Substrate.**
- **"MCP server … not connected" or "Unauthorized" hook errors:** installed but
  not signed in, or the connection was removed on the Agents page. Run
  `/substrate-claude:substrate-connect` to reconnect; do not install a second
  copy. Claude Code shows these hook errors itself, and the plugin cannot hide
  them: the capture hooks are MCP tool hooks that Claude Code calls directly.
- **Import stopped:** ask the agent to continue the import; finished
  conversations are skipped and the server ignores repeats.
