# Substrate Memory for Codex and ChatGPT Work

One package serves ChatGPT Work, Codex in the ChatGPT desktop app, and Codex CLI.

## What you get

- Automatic recall: relevant facts from your knowledge base arrive as context
  each turn. Use them naturally.
- Automatic capture: prompts, tool activity, and answers are saved per turn
  for future recall.
- Explicit memory: ask the agent to remember or forget a fact.
- Catch-up sync: if some turns were not saved yet, the agent imports them
  from the local transcript on request.

## Install

### ChatGPT / Codex app

1. Open Plugins and add the marketplace from the GitHub repo
   `Substrate-memory/Substrate-memory-plugins` (workspace admins can import
   a GitHub marketplace for the team; a personal marketplace file lives at
   `~/.agents/plugins/marketplace.json`).
2. Install **Substrate Memory**.
3. Start a new chat.

### Codex CLI

Use the `/plugins` browser, or run:

```text
codex plugin marketplace add Substrate-memory/Substrate-memory-plugins
codex plugin add substrate-codex@substrate-marketplace
```

Then start a new session. Direct MCP fallback (no plugin):

```text
codex mcp add substrate-memory --url https://app.trysubstrate.co/mcp
codex mcp login substrate-memory
```

## Sign-in flow

Install → Sign in → Review → Approve connection → Connected to Substrate.

The first memory call opens **Connect your agent to Substrate**. Sign in,
review the connection, choose **Approve connection**, then return to the agent.
The agent confirms with an authenticated `memory_search` check and reports
**Connected to Substrate.**

## Hook trust step

This plugin calls memory tools automatically during the agent loop. Codex
treats these as hooks that need your review. Open `/hooks`, review the
Substrate entries, and trust them. Until trusted, Codex skips automatic
capture and recall still works when you ask.

## What is captured

Prompts, tool activity, and answers per turn, plus session boundaries
(start, resume, clear, compact). Captures are redacted on the way in:
passwords, tokens, and keys are replaced before storage.

## Privacy

- Memory tools never receive pasted secrets; the agent redacts them first.
- `memory_forget` suppresses a fact from normal recall on your explicit
  request.
- History import only runs after you approve it, and only from transcripts
  on your own machine.

## Known limits

- There is no session-end hook: Codex cannot call memory tools when a session
  ends, so the server seals idle sessions after 30 minutes.
- Plugins installed on the web do not deploy scripts, so the sync and import
  scripts require the desktop app or the CLI.
- At session launch, the start hook can run before the memory server is
  connected; the boundary is then picked up on the first turn.

## Troubleshooting

- Tools absent after install: start a new session so the plugin loads.
- Sign-in loops: complete **Approve connection** in the browser, return to
  the agent, and ask it to verify with `memory_search`.
- `[substrate] ... not saved yet`: ask the agent to run `$substrate-sync`.
- Still stuck: ask the agent to run `$substrate-connect` and follow its steps.
