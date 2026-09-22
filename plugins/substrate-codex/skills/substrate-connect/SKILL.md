---
name: substrate-connect
description: Check Substrate wiring and sign-in before installing or reconnecting.
---

# Substrate connect

1. If `memory_search` is available, call it with a focused, non-secret query.
   An empty result is valid. On success report exactly:
   **Connected to Substrate.**
2. If the plugin is configured but tools are absent, say it is installed but
   not wired into this session. Start a new session and give the user this
   prompt for the next session:
   `Verify my Substrate connection with memory_search and report Connected to Substrate.`
3. If nothing is configured, install through the host's documented interface:
   ChatGPT/Codex app via Plugins → marketplace from the GitHub repo, or
   Codex CLI via `/plugins` browser or
   `codex plugin marketplace add Substrate-memory/Substrate-memory-plugins`
   plus `codex plugin add substrate-codex@substrate-marketplace`.
   Direct MCP fallback: `codex mcp add substrate-memory --url https://app.trysubstrate.co/mcp`
   then `codex mcp login substrate-memory`.
4. If sign-in is required, show the exact browser URL. The user signs in,
   reviews the request, and chooses **Approve connection**. Finish in the agent
   and rerun the smoke test. **Connection approved** is not proof of a working
   client. Never ask for a pasted token or secret.
5. After the first successful smoke test, ask exactly once whether the user
   wants to import past conversations (`$substrate-import`).
