# Install Substrate memory (fallback package)

Use this six-step experience:
**Install → Sign in → Review → Approve connection → Finish in the agent → Verify.**

Start with:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

> Use this package only when no native package fits your host. Claude Cowork and Claude Code use `plugins/substrate-claude`; ChatGPT Work, the Codex app and Codex CLI use `plugins/substrate-codex`; Hermes uses `plugins/substrate-hermes`.

The shared [installation and recovery guide](https://github.com/Substrate-memory/Substrate-memory-plugins/blob/main/docs/installation.md)
is the source of truth. This package configures
`https://app.trysubstrate.co/mcp` using remote **Streamable HTTP**; it does not
install another server.

## Before you start

The agent can prepare supported settings. The user must click host permission controls and the exact browser link, then **Approve connection**. Follow the host's documented session or reload lifecycle when tools are absent.

## Self-check before installing

1. If `memory_search` is present, run an authenticated smoke test. An empty result is valid. On success report **Connected to Substrate.** and do not reinstall.
2. If the package/server is configured but tools are absent, say that it is not wired into this session. Follow the host's documented restart or session action, then paste:

   ```text
   Verify my Substrate connection with memory_search and report Connected to Substrate.
   ```
3. If nothing is configured, use the host section below and then continue the common flow.
4. If tools are present but authorization is required, show the exact browser URL. The user signs in, reviews it, and chooses **Approve connection**. Finish in the agent and rerun `memory_search` before reporting success.

## Other MCP hosts

Use the host's documented remote Streamable HTTP and browser-authorization interface to configure `https://app.trysubstrate.co/mcp`. Discover supported configuration commands instead of inventing them. If the host cannot edit its own configuration, provide precise instructions for that host and then return to the same Substrate connection flow. A client with only local stdio or incompatible OAuth is not automatically supported; never work around that limitation by asking the user for a token. Do not use the deprecated API-key plugin described below.

The package also provides the self-check slash command `/substrate-mcp:substrate-connect` on hosts that support plugin commands.

## Sign-in and verification

Open the exact browser link returned by the client. Every host uses **Connect your agent to Substrate**. Sign in if needed, review the connection name, permissions and proof details, then choose **Approve connection** or deny. Only the user can approve. **Connection approved** means consent was accepted; the client must still finish its handoff and make an authenticated memory request. Only then report **Connected to Substrate.** Never request API keys, manual tokens, or client secrets.

## What the agent already knows (asked once)

After **Connected to Substrate.**, the agent asks exactly once:

```text
I can save what I already know about you to Substrate. Want me to?
```

On yes, the agent searches its own memory and past chats (only what this host lets it read) for people, projects, decisions, preferences and open items, shows the list, and imports it only after the user confirms. The import is one `memory_import` document item (`kind: "document"`, a title, markdown text, and a source such as "Claude memory"). If the server rejects document items, the agent saves the confirmed facts with `memory_remember` instead. If the user says no, it does not ask again. Never import secrets, API keys, tokens, passwords, or private credentials.

## Legacy plugins

The API-key based Claude Code, Codex, and Cowork plugins shipped in August 2026 are deprecated. This includes `substrate_capture`, paths such as `~/.substrate/*/spool` (including `~/.substrate/claude_code_memory/spool`), and setup using `SUBSTRATE_API_URL` or `SUBSTRATE_API_KEY`.

Uninstall them. Do not set `SUBSTRATE_API_KEY` by hand; the current product does not issue API keys to users. Their local spools are not migrated; pending events do not become Substrate memory. Install the matching v0.10.0 package and use browser authorization instead.

This package has no hooks and provides no full-transcript capture guarantee. It is a thin remote MCP client with best-effort memory tools. Do not place API keys, OAuth tokens, client secrets, or other credentials in plugin files, prompts, config, or chat.
