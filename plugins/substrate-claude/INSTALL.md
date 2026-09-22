# Install Substrate memory for Claude (agent runbook)

Use this six-step experience:
**Install → Sign in → Review → Approve connection → Finish in the agent → Verify.**

## Before you start

The agent can prepare supported settings. The user must click host permission
controls and the exact browser link, then **Approve connection**.

## Self-check before installing

1. If `memory_search` is present, run an authenticated smoke test. An empty
   result is valid. On success report **Connected to Substrate.** and do not
   reinstall.
2. If the package/server is configured but tools are absent, say that it is not
   wired into this session. Start a new session (Cowork registers plugin MCP
   servers at session start) or run `/reload-plugins` (Claude Code), then paste:

   ```text
   Verify my Substrate connection with memory_search and report Connected to Substrate.
   ```
3. If nothing is configured, use the host section below and then continue the
   common flow.
4. If tools are present but authorization is required, show the exact browser
   URL. The user signs in, reviews it, and chooses **Approve connection**.
   Finish in the agent and rerun `memory_search` before reporting success.

## Cowork

1. Open **Cowork → Customize → Plugins → Personal plugins → + → Add
   marketplace**.
2. Choose **Add from a repository (GitHub URL)** and enter
   `https://github.com/Substrate-memory/Substrate-memory-plugins`.
3. Choose **Browse**, then install `substrate-claude`.
4. **Start a new Cowork session.** The plugin MCP server registers only at
   session start. If tools still do not appear, quit and reopen the app.
5. In the new session, run the self-check above.

## Claude Code

```text
/plugin marketplace add Substrate-memory/Substrate-memory-plugins
/plugin install substrate-claude@substrate-marketplace
/reload-plugins
```

Complete browser authorization and run the self-check. Plugins enabled on
claude.ai also sync to Claude Code automatically.

## Sign-in and verification

Open the exact browser link returned by the client. Review the connection name,
permissions, and proof details, then choose **Approve connection** or deny.
Only the user can approve. Only after the authenticated request succeeds report
**Connected to Substrate.** Never request API keys, manual tokens, or client
secrets.

## Import past conversations (asked once)

After **Connected to Substrate.**, ask exactly once whether the user wants to
import past conversations, then follow
`/substrate-claude:substrate-import`. If the user says no, do not ask again.
