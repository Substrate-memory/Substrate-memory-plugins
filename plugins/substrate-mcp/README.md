# Substrate memory for MCP hosts

This is the thin `plugins/substrate-mcp` client for Cowork, Claude Code, Codex,
and other compatible MCP hosts. It registers one remote HTTP MCP server and one
generic usage skill. It contains no local MCP server, hooks, transcript bridge,
credentials, or host-specific runtime.

The endpoint is `https://app.trysubstrate.co/mcp` over remote **Streamable HTTP**.
The remote service and browser connection flow are backend-owned. Hermes and MCP
use the same sign-in, review and approval screen. Do not report an installation
as successful until an authenticated `memory_search` smoke test passes.

## Install and host mechanics

Start with:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

Use **Install → Sign in → Review → Approve connection → Finish in the agent →
Verify**. The agent can prepare supported settings. The user must click host
permission controls and the exact browser link, then **Approve connection**.

| Host | Agent can automate | User must click | New session or restart required before tools appear? |
|---|---|---|---|
| Claude Code | Run the confirmed `claude mcp add` and authenticate with `/mcp` or `claude mcp login`. | Host prompts and browser approval. | No documented restart requirement; use `/reload-plugins` for plugin changes. |
| Cowork | Install this marketplace package when permitted. | Marketplace/permission dialogs and browser approval. | **Yes in the current build (observed). Start a new session.** If tools still do not appear, quit and reopen the app. |
| Codex | Run the confirmed `codex mcp add` and `codex mcp login` commands. | Host prompts and browser approval. | No documented restart requirement; use the host's session action if tools are absent. |
| Other MCP clients | Use the host's documented remote MCP interface. | Host controls and browser approval. | Follow the host's documented lifecycle; do not assume a restart. |

See [INSTALL.md](INSTALL.md) and the shared [installation and recovery
guide](https://github.com/Substrate-memory/Substrate-memory-plugins/blob/main/docs/installation.md)
for the host-specific flow. Never invent a host command or request a manual token.

## Self-check before installing

- **Tools present:** run an authenticated `memory_search` smoke test. Empty is a
  valid result. On success report **Connected to Substrate.**
- **Configured but tools absent:** say it is not wired into this session. Start a
  new session (required for Cowork), then paste:

  ```text
  Verify my Substrate connection with memory_search and report Connected to Substrate.
  ```
- **Nothing configured:** install this package through the host's documented
  interface, then complete the common flow.
- **Authorization required:** show the exact browser URL. The user signs in,
  reviews, and chooses **Approve connection**. Finish in the agent and rerun the
  smoke test. **Connection approved** is not proof of a working client.

## Claude Code and Codex

Claude Code direct setup and sign-in:

```text
claude mcp add --transport http substrate-memory https://app.trysubstrate.co/mcp
claude mcp login substrate-memory
```

Codex direct setup and sign-in:

```text
codex mcp add substrate-memory --url https://app.trysubstrate.co/mcp
codex mcp login substrate-memory
```

Claude Code also supports this plugin flow:

```text
/plugin marketplace add Substrate-memory/Substrate-memory-plugins
/plugin install substrate-mcp@substrate-marketplace
/reload-plugins
```

Complete browser authorization and run the self-check.

## Cowork

1. Open **Cowork → Customize → Plugins → Personal plugins → + → Add marketplace**.
2. Choose **Add from a repository (GitHub URL)** and enter the repository URL above.
3. Choose **Browse**, then install `substrate-mcp`.
4. Confirm `https://app.trysubstrate.co/mcp` and **Streamable HTTP** transport.
5. **Start a new Cowork session.** The plugin MCP server is registered only at
   session start (observed in the current Cowork build). No browser tab opens in
   the old session. If tools still do not appear, quit and reopen the app.
6. In the new session, paste the self-check prompt above. The first memory call
   opens **Connect your agent to Substrate**. Review the request and choose
   **Approve connection**, then verify.

The GitHub-URL marketplace procedure above is observed in the current Cowork build;
its public official documentation is not yet confirmed. For another MCP host, use
its documented configuration interface. Do not invent a command or bypass client
permissions.

## Sign-in and verification

Both integrations use **Connect your agent to Substrate**. Open the exact browser
link returned by the client, review the connection name, permissions and proof
details, then choose **Approve connection** or deny. Only the user can approve.
Only after the authenticated request succeeds report **Connected to Substrate.**
Never request API keys, manual tokens, or client secrets.

## Import past conversations (asked once)

After **Connected to Substrate.**, the agent asks exactly once:

```text
Do you want to import past conversations into Substrate? I will only use
conversations I can access on this host, show you what I extracted, and write
nothing until you confirm.
```

If the user says no, do not ask again. If yes:

1. Use only history this agent can read on this host: its own local session
   transcripts, or an export file the user places where the agent can read it.
   The agent has no access to another app's chat history. If nothing is
   accessible, say so plainly and stop.
2. Extract durable facts, decisions, preferences, and people/projects. Skip
   small talk. Never extract secrets, API keys, tokens, passwords, or private
   credentials.
3. Show the list to the user before writing. Let the user remove items.
4. After confirmation, write each item with the host's `memory_remember` tool:
   one item per call, one new `operation_id` per item, at most 4096 bytes of
   text, `durability` set to `durable` unless the item is clearly time-bound.
   Report the count written and any failures.

Raw transcripts are not uploaded. This step is the same for every host; only
the readable history differs.

## Legacy plugins

The API-key based Claude Code, Codex, and Cowork plugins shipped in August 2026
are deprecated. This includes `substrate_capture`, paths such as `~/.substrate/*/spool` (including
`~/.substrate/claude_code_memory/spool`), and setup using `SUBSTRATE_API_URL` or
`SUBSTRATE_API_KEY`.

Uninstall them. Do not set `SUBSTRATE_API_KEY` by hand; the current product
does not issue API keys to users. Their local spools are not migrated; pending
events do not become Substrate memory. Install this package and
use browser authorization instead.

## Contents and guarantees

- `.mcp.json` — remote HTTP MCP at `https://app.trysubstrate.co/mcp`.
- `skills/substrate-memory/SKILL.md` — generic, best-effort recall/write guidance.
- `commands/substrate-connect.md` — self-check slash command (`/substrate-mcp:substrate-connect`).
- `INSTALL.md` — installation and verification runbook.

The server may expose tools such as `memory_search` and a durable write tool. The
skill does not assume every host exposes the same tool names or transcript
lifecycle. Recall and writes are best effort. This package has no hooks and does
not guarantee automatic full-conversation capture. Do not place credentials in
plugin files, prompts, config, or chat.
