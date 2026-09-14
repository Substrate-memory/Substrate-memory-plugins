# Install Substrate memory

Use this six-step experience:
**Install → Sign in → Review → Approve connection → Finish in the agent → Verify.**

Start with:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

The shared [installation and recovery guide](https://github.com/Substrate-memory/Substrate-memory-plugins/blob/main/docs/installation.md)
is the source of truth. This package configures
`https://app.trysubstrate.co/mcp` using remote **Streamable HTTP**; it does not
install another server.

## Before you start

The agent can prepare supported settings. The user must click host permission
controls and the exact browser link, then **Approve connection**. For Cowork in
the current build (observed behavior), **start a new session after installation**:
the plugin MCP server is registered at session start. If tools still do not appear,
quit and reopen the app. Other hosts use their documented session or reload
lifecycle when tools are absent.

## Self-check before installing

1. If `memory_search` is present, run an authenticated smoke test. An empty result
   is valid. On success report **Connected to Substrate.** and do not reinstall.
2. If the package/server is configured but tools are absent, say that it is not
   wired into this session. Start a new session (required for Cowork), then paste:

   ```text
   Verify my Substrate connection with memory_search and report Connected to Substrate.
   ```
3. If nothing is configured, use the host section below and then continue the
   common flow.
4. If tools are present but authorization is required, show the exact browser URL.
   The user signs in, reviews it, and chooses **Approve connection**. Finish in
   the agent and rerun `memory_search` before reporting success.

## Claude Code

Direct remote MCP setup:

```text
claude mcp add --transport http substrate-memory https://app.trysubstrate.co/mcp
```

Authenticate with `/mcp`, or run:

```text
claude mcp login substrate-memory
```

A plugin installation can use:

```text
/plugin marketplace add Substrate-memory/Substrate-memory-plugins
/plugin install substrate-mcp@substrate-marketplace
/reload-plugins
```

Complete browser authorization and run the self-check. Do not use the deprecated
API-key plugin described below.

## Cowork

The menu flow below was observed working in the current Cowork build; its public
official documentation is not yet confirmed.

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

## Codex

Configure and authenticate the remote Streamable HTTP server:

```text
codex mcp add substrate-memory --url https://app.trysubstrate.co/mcp
codex mcp login substrate-memory
```

Complete browser authorization and run the self-check. Do not use the deprecated
API-key plugin described below.

The package also provides the self-check slash command
`/substrate-mcp:substrate-connect` on hosts that support plugin commands.

For another MCP host, use its documented configuration interface. Do not invent a
command or bypass client permissions.

## Sign-in and verification

Open the exact browser link returned by the client. Both integrations use
**Connect your agent to Substrate**. Sign in if needed, review the connection
name, permissions and proof details, then choose **Approve connection** or deny.
Only the user can approve. **Connection approved** means consent was accepted;
the client must still finish its handoff and make an authenticated memory request.
Only then report **Connected to Substrate.** Never request API keys, manual tokens,
or client secrets.

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

This package has no hooks and provides no full-transcript capture guarantee. It is
a thin remote MCP client with best-effort memory tools. Do not place API keys,
OAuth tokens, client secrets, or other credentials in plugin files, prompts,
config, or chat.
