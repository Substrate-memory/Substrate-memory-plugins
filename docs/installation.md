# One Substrate connection experience

Start with this request in the agent you want to connect:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

## Before you start: host mechanics

The install path depends on the host. The agent can prepare supported settings, but
it cannot click a browser consent button or bypass host permission dialogs.

| Host | Agent can automate | User must click | New session or restart required before tools appear? |
|---|---|---|---|
| Hermes | Install the `substrate` plugin, start device login, poll approval, and run the smoke test. | The exact browser link, then **Approve connection**. | No. Verify in the active agent after approval. |
| Claude Code | Run the confirmed `claude mcp add` command, authenticate with `/mcp` or `claude mcp login`, and run the smoke test. | Host permission prompts and the exact browser link, then **Approve connection**. | No documented restart requirement. For plugin changes use `/reload-plugins`; if a shell-added server is absent, use the host's session/reload action. |
| Claude Cowork | Install the marketplace plugin when permitted and configure its remote MCP entry. | Marketplace/plugin permission dialogs and the exact browser link, then **Approve connection**. | **Yes in the current Cowork build (observed). Start a new session.** If tools still do not appear, quit and reopen the app. |
| Codex | Run the confirmed `codex mcp add` and `codex mcp login` commands, then run the smoke test. | Host permission prompts and the exact browser link, then **Approve connection**. | No documented restart requirement; if tools are absent, use the host's documented session action. |
| Other MCP clients | Use the host's documented remote Streamable HTTP and browser-authorization interface. | Any host setup/permission controls and the exact browser link, then **Approve connection**. | Follow the host's documented lifecycle; do not assume a restart. |

The exact host commands are listed only where this repository has confirmed them.
Do not substitute a manual-token setup.

## User-facing flow (Hermes and MCP)

Use this same six-step sequence for every supported integration:

1. **Install.** The agent identifies its current host and configures the supported
   integration. Ask the user only if that host cannot be determined safely.
2. **Sign in.** Open the exact browser link returned by Substrate or the client's
   authorization flow. Both arrive at **Connect your agent to Substrate**. Reuse
   an existing Substrate session; otherwise sign in or create an account normally.
3. **Review.** Use the same connection-name field and requested-access list. Check
   the connection details against the agent being installed. A displayed name is
   not proof of identity. Do not hide the verification code or registered callback
   host when those details are supplied.
4. **Approve connection.** The user approves or denies in the browser. Never
   approve on the user's behalf. No API keys, manual tokens, or client secrets are
   requested, displayed, copied, or pasted.
5. **Finish in the agent.** The browser shows **Connection approved**, not a claim
   that installation is already complete. The client resumes automatically where
   supported; otherwise return to the agent. It completes authentication privately
   and runs an authenticated memory smoke test, normally `memory_search`.
6. **Verify.** An empty authenticated result is valid. Only after a successful
   request report **Connected to Substrate.** If a restart is required, perform or
   explain it first and verify in the new session before reporting success.

## Self-check before installing

Before changing anything, the agent checks the current host and the available MCP
or memory tools. Use this decision protocol:

1. **Tools are present.** Run an authenticated `memory_search` smoke test with a
   focused, non-secret query. An empty result is valid. If it succeeds, report
   **Connected to Substrate.** and do not reinstall.
2. **The plugin/server is configured, but tools are absent.** Say plainly that the
   integration is installed but is not wired into this session. For Cowork, start a
   new session because registration happens only at session start. Otherwise follow
   the host's documented restart/session action. Then ask the user to paste this
   exact prompt in the new session:

   ```text
   Verify my Substrate connection with memory_search and report Connected to Substrate.
   ```
3. **Nothing is configured.** Follow the host installation path below, then return
   to the six-step flow.
4. **Tools are present but authentication is required.** Do not call this connected.
   Show the exact browser URL returned by the tool or client and ask the user to
   open it, sign in, review, and choose **Approve connection**. After approval,
   finish in the agent and rerun the smoke test.

Never infer authentication from a plugin file, marketplace entry, or browser state.
**Connection approved** is consent only. It is not **Connected to Substrate.**

## Installation mechanics (not separate onboarding)

These are client-specific preparation steps, not different Substrate sign-in flows.
Do not select Hermes merely because a Hermes executable exists elsewhere on the
machine. Target the agent currently handling the installation request.

### Hermes

Use the golden `plugins/substrate` plugin and its published v0.5.0 install pin.
Follow its README for the standard-library login CLI and supported host version.
Do not change its runtime, inspect another profile, upgrade Hermes automatically,
or replace its working device authorization with a custom OAuth client. Wait for
browser approval, let the plugin obtain/store credentials privately, then verify.
Keep existing memory configuration unchanged until a memory request succeeds.
Then run the shared **Import past conversations** step below; it is identical for
Hermes and MCP hosts.

### Claude Code

The confirmed direct MCP setup is:

```text
claude mcp add --transport http substrate-memory https://app.trysubstrate.co/mcp
```

Authenticate with `/mcp` in Claude Code, or run:

```text
claude mcp login substrate-memory
```

For a user-scoped server, add `--scope user` to the `claude mcp add` command. A
plugin installation may instead use these confirmed Claude Code commands:

```text
/plugin marketplace add Substrate-memory/Substrate-memory-plugins
/plugin install substrate-mcp@substrate-marketplace
/reload-plugins
```

Complete the common browser flow and run the self-check. Do not use the deprecated
API-key plugin described below.

### Cowork

Use the thin `plugins/substrate-mcp` package. Configure the remote Streamable HTTP
endpoint `https://app.trysubstrate.co/mcp` through the client's supported interface.
Do not install another server or add capture hooks.

Marketplace setup below was observed working in the current Cowork build; its
public official documentation is not yet confirmed. Automate it when the host
permits it. Otherwise give these exact instructions, without asking for credentials:

1. Open the **Cowork** tab.
2. Choose **Customize → Plugins → Personal plugins → + → Add marketplace**.
3. Choose **Add from a repository (GitHub URL)** and enter the repository URL above.
4. Choose **Browse**, then install `substrate-mcp`.
5. Confirm the server URL above and **Streamable HTTP** transport.
6. **Start a new Cowork session.** In the current Cowork build, a plugin MCP
   server is registered only at session start (observed behavior). Nothing
   opens a browser tab in the old session. If tools still do not appear in the
   new session, quit and reopen the app.
7. In the new session, paste the self-check prompt. The first memory call opens
   the browser link. Complete the same Substrate sign-in, review, approval and
   verification sequence.

Required client installation/permission dialogs cannot be bypassed by a repo prompt.
The user must perform steps the client does not let the agent automate.

### Codex

The confirmed setup is:

```text
codex mcp add substrate-memory --url https://app.trysubstrate.co/mcp
codex mcp login substrate-memory
```

The first command configures the remote Streamable HTTP server. The second starts
the browser authorization flow. Complete the common browser flow and run the
self-check after the host reports the server is available. Do not use the
deprecated API-key plugin described below.

### Other MCP-capable agents

Use the same endpoint and the host's native remote Streamable HTTP/browser OAuth
support. Discover supported configuration commands instead of inventing them.
If the host cannot edit its own configuration, provide precise instructions for
that host and then return to the same Substrate connection flow. A client with
only local stdio or incompatible OAuth is not automatically supported; never work
around that limitation by asking the user for a token.

## Import past conversations (asked once, same for Hermes and MCP)

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
are deprecated. This includes `substrate_capture`, local paths such as `~/.substrate/*/spool` (including
`~/.substrate/claude_code_memory/spool`), and any setup using `SUBSTRATE_API_URL`
or `SUBSTRATE_API_KEY`.

Uninstall those legacy plugins. Do not set `SUBSTRATE_API_KEY` by hand on any
host; the current product does not issue API keys to users. (The Hermes plugin
writes its own private credential after browser approval. Leave that alone.)
Their local spools are not migrated; pending spool events do not become
Substrate memory. Install `plugins/substrate-mcp` and complete browser
authorization instead.

## Recovery and implementation boundary

For an expired or unavailable link, return to the agent and start connecting again.
For a denied request, say that access was not granted. For a transient service
error, preserve entered fields and retry safely; do not substitute a manual-token
flow. A repository checkout or release label is not evidence that the live
connection works.

The Substrate browser screen, consent model and account/entitlement checks are
shared. Hermes device polling and MCP authorization-code/PKCE callbacks remain
protocol adapters underneath. Their proof details and client-controlled handoff
can differ, but there is no separate product onboarding journey.

This common setup does not claim identical memory automation: Hermes retains its
durable automatic capture. MCP clients use explicit/best-effort tool invocation.
