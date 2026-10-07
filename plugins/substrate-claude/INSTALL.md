# Install Substrate memory for Claude (agent runbook)

**Install → Sign in → Review → Approve connection → Connected to Substrate. →
offer to import past conversations.** Chain every step in the same turn. Pause
only for the browser approval and the user's import choice.

## Claude Code

1. **One copy only.** Run `claude plugin list --json`. If any
   `substrate-claude@…` entry exists (a copy synced from claude.ai is
   `substrate-claude@synced`), do not install another. If it is disabled, run
   `claude plugin enable <id>`. Otherwise install it yourself:

   ```text
   claude plugin marketplace add Substrate-memory/Substrate-memory-plugins
   claude plugin install substrate-claude@substrate-marketplace
   ```
2. If the Substrate tools (or their `authenticate` tool) are already in this
   session, go straight to `/substrate-claude:substrate-connect` step 1.
   Otherwise ask the user for one thing only: *Type `/reload-plugins`, then
   `/substrate-claude:substrate-connect`.* That command signs in (the user
   approves in the browser), verifies with `memory_search`, says **Connected to
   Substrate.**, and in the same reply offers **Import all** / **Let me pick** /
   **Not now**.
3. On **Import all**, follow `/substrate-claude:substrate-import`: it starts the
   upload in the background and reports progress until the final summary
   line, without waiting for the user.

## Cowork

1. **Cowork → Customize → Plugins → Personal plugins → + → Add marketplace →
   Add from a repository (GitHub URL)**:
   `https://github.com/Substrate-memory/Substrate-memory-plugins`. **Browse**,
   install `substrate-claude`.
2. **Start a new session** (plugin MCP servers register at session start; if
   tools still do not appear, quit and reopen the app). There, paste:

   ```text
   Verify my Substrate connection with memory_search and report Connected to Substrate.
   ```
3. Cowork cloud sessions have no local history, so the import offer is skipped.

## Rules

- Only the user approves, in the browser. **Connection approved** is not
  **Connected to Substrate.**; only a successful `memory_search` is.
- Never request or accept API keys, tokens, or client secrets. Never show the
  import ticket in a reply.
- Use the plugin or the claude.ai Substrate connector, not both.
