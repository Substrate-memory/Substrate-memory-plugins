# Substrate memory for other MCP agents (fallback)

This is the thin `plugins/substrate-mcp` fallback for MCP-capable agents that have **no native Substrate package**. If you use Claude Cowork or Claude Code, install `plugins/substrate-claude`. If you use ChatGPT Work, the Codex app, or Codex CLI, install `plugins/substrate-codex`. If you use Hermes, install `plugins/substrate-hermes`. Use this fallback only when none of those packages fits your host.

It registers one remote HTTP MCP server and one usage skill. It contains no local MCP server, hooks, transcript bridge, credentials, or host-specific runtime.

The endpoint is `https://app.trysubstrate.co/mcp` over remote **Streamable HTTP**. The remote service and browser connection flow are backend-owned. Every host uses the same sign-in, review and approval screen. Do not report an installation as successful until an authenticated `memory_search` smoke test passes.

## Install and host mechanics

Start with:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

Use **Install → Sign in → Review → Approve connection → Finish in the agent → Verify**. The agent can prepare supported settings. The user must click host permission controls and the exact browser link, then **Approve connection**.

| Host | Agent can automate | User must click | New session or restart required before tools appear? |
|---|---|---|---|
| Other MCP clients (fallback) | Configure the remote Streamable HTTP server through the host's documented interface. | Host controls and browser approval. | Follow the host's documented lifecycle; do not assume a restart. |

See [INSTALL.md](INSTALL.md) and the shared [installation and recovery guide](https://github.com/Substrate-memory/Substrate-memory-plugins/blob/main/docs/installation.md) for the host-specific flow. Never invent a host command or request a manual token.

## Self-check before installing

- **Tools present:** run an authenticated `memory_search` smoke test. Empty is a valid result. On success report **Connected to Substrate.**
- **Configured but tools absent:** say it is not wired into this session. Follow the host's documented session or reload action, then paste:

  ```text
  Verify my Substrate connection with memory_search and report Connected to Substrate.
  ```
- **Nothing configured:** install this package through the host's documented interface, then complete the common flow.
- **Authorization required:** show the exact browser URL. The user signs in, reviews, and chooses **Approve connection**. Finish in the agent and rerun the smoke test. **Connection approved** is not proof of a working client.

## Sign-in and verification

Open the exact browser link returned by the client and review the connection name, permissions and proof details on **Connect your agent to Substrate**. Choose **Approve connection** or deny. Only the user can approve. Only after the authenticated request succeeds report **Connected to Substrate.** Never request API keys, manual tokens, or client secrets. The product does not issue API keys to users.

## Memory tools (contract v2)

The server exposes one memory tool surface, defined in [`docs/mcp-contract.md`](../../docs/mcp-contract.md):

- **Recall:** `memory_search` (the connection smoke test; an empty authenticated result is valid), `memory_expand`, `memory_evidence`, `memory_shares`.
- **Explicit writes:** `memory_remember` (one item per call, one new `operation_id` per item, at most 4096 bytes of text) and `memory_forget` (exactly one `m:`/`p:` handle plus a reason; it suppresses normal recall without deleting evidence).
- **Turn lifecycle** (used by hook-wired packages; the agent may call them directly where the host permits): `memory_turn_context`, `memory_capture_tool`, `memory_capture_turn`, `memory_session_boundary`.
- **History import:** `memory_import` replays redacted turns in batches (at most 64 items per call) for agent-driven history import where the host can read its own transcripts; items sent without an `event_id` get a deterministic id, so re-sending never stores twice. `memory_import_status` reports what the server holds.

## Import past conversations (asked once)

After **Connected to Substrate.**, the agent asks exactly once:

```text
Do you want to import past conversations into Substrate? I will show you the sessions I can read on this host, and import only what you confirm. Nothing is written before you confirm.
```

If the user says no, do not ask again. If yes: list the sessions this agent can read on this host (its own transcripts, or an export file the user provides), show the list, and import only the confirmed sessions as raw, redacted turns via `memory_import` (or confirmed facts via `memory_remember` where the host cannot replay turns). Never import secrets. If no history is accessible, say so and stop.

## Legacy plugins

The API-key based Claude Code, Codex, and Cowork plugins shipped in August 2026 are deprecated. This includes `substrate_capture`, paths such as `~/.substrate/*/spool` (including `~/.substrate/claude_code_memory/spool`), and setup using `SUBSTRATE_API_URL` or `SUBSTRATE_API_KEY`.

Uninstall them. Do not set `SUBSTRATE_API_KEY` by hand; the current product does not issue API keys to users. Their local spools are not migrated; pending events do not become Substrate memory. Install the matching v0.7.0 package and use browser authorization instead.

## Contents and guarantees

- `.mcp.json` — remote HTTP MCP at `https://app.trysubstrate.co/mcp`.
- `skills/substrate-memory/SKILL.md` — fallback usage guidance for the contract v2 tools.
- `commands/substrate-connect.md` — self-check slash command (`/substrate-mcp:substrate-connect`).
- `INSTALL.md` — installation and verification runbook.

This fallback has no hooks and does not guarantee automatic full-conversation capture. Recall and writes are best effort: call `memory_search` with a focused query before relying on a fact, and do not claim that every turn was captured. Do not place credentials in plugin files, prompts, config, or chat.
