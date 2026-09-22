# Substrate memory for Claude Cowork and Claude Code

One package serves both hosts. It registers a remote Substrate MCP server, a
usage skill, three slash commands, and the hooks that capture turns
automatically. There is no local server and no credential handling in the
plugin: sign-in runs through the host browser flow.

## What you get

- In **Cowork** (cloud and local sessions): memory recall on every prompt,
  automatic capture of prompts, tool use, and answers, plus
  `/substrate-claude:substrate-connect`, `/substrate-claude:substrate-sync`,
  and `/substrate-claude:substrate-import`.
- In **Claude Code**: the same skill, commands, and automatic capture, with
  local transcript sync through `scripts/substrate_sync.py`.

## How it works

- `UserPromptSubmit` calls `memory_turn_context`: the server opens the pending
  turn and returns recall for this turn as extra context.
- `PostToolUse` calls `memory_capture_tool` for every tool call.
- `Stop` calls `memory_capture_turn` for the main agent; `SubagentStop` calls
  it with the subagent context.
- `SessionStart`, `PreCompact`, and `SessionEnd` call
  `memory_session_boundary` so sessions seal cleanly.
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

```text
/plugin marketplace add Substrate-memory/Substrate-memory-plugins
/plugin install substrate-claude@substrate-marketplace
/reload-plugins
```

Then run `/substrate-claude:substrate-connect`. Plugins enabled on claude.ai
also sync to Claude Code automatically.

## Sign-in flow

**Install → Sign in → Review → Approve connection → Connected to Substrate.**

The first memory call opens **Connect your agent to Substrate**. Open the exact
browser link, sign in, review the connection name and permissions, choose
**Approve connection**, then return to the agent for verification. Only after an
authenticated `memory_search` succeeds does the agent report
**Connected to Substrate.** Never paste an API key or token into chat.

## What is captured

Prompts, tool calls (names plus bounded, redacted arguments), tool results
(excerpts plus digests), and assistant replies, grouped per turn. Session
boundaries (start, clear, compact, end) seal extraction windows.

## Privacy and redaction

Secrets (API keys, tokens, passwords, `sk_…` values) are redacted on the client
before anything leaves the host, and redacted again on the server. The sync
script never contacts the network; the agent passes its printed batches to
`memory_import`. Never send credentials to a memory tool yourself.

## Known limits

- `SessionStart` at launch fires before MCP servers connect, so the hook call
  is skipped; the boundary is implied by the first turn instead.
- A cloud session that ends has a short hook budget; the server also seals a
  session after 30 minutes idle, so nothing is lost.
- Cowork cloud sessions cannot run local servers or scripts: everything goes
  through the remote server.

## Troubleshooting

- **Tools absent after install:** start a new session (Cowork) or run
  `/reload-plugins` (Claude Code), then rerun the self-check prompt.
- **`[substrate] … not saved yet` line:** run
  `/substrate-claude:substrate-sync` to send the missing turns.
- **Authorization required:** follow the browser link, approve, and rerun the
  smoke test. **Connection approved** is not **Connected to Substrate.**
