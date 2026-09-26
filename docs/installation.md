# One Substrate connection experience

Start with this request in the agent you want to connect:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

The agent identifies its host and installs the right package: Claude Cowork and Claude Code use `plugins/substrate-claude`; ChatGPT Work, the Codex app and Codex CLI use `plugins/substrate-codex`; Hermes uses `plugins/substrate-hermes`; any other MCP-capable agent uses the thin `plugins/substrate-mcp` fallback, only when no package fits.

## Before you start: host mechanics

The install path depends on the host. The agent can prepare supported settings, but it cannot click a browser consent button or bypass host permission dialogs.

| Host | Package | Agent can automate | User must click | New session or restart required before tools appear? |
|---|---|---|---|---|
| Claude Code | `plugins/substrate-claude` | Install the plugin, wire `memory_*` hooks, run the sync command setup, authenticate, and run the smoke test. | Host permission prompts and the exact browser link, then **Approve connection**. Trust plugin hooks once via `/hooks`. | No documented restart requirement; use the host's session/reload action if tools are absent. |
| Claude Cowork | `plugins/substrate-claude` | Install the marketplace plugin when permitted and configure its remote MCP entry and hooks. | Marketplace/plugin permission dialogs and the exact browser link, then **Approve connection**. | **Yes in the current Cowork build (observed). Start a new session.** If tools still do not appear, quit and reopen the app. |
| Codex (CLI, app) and ChatGPT Work | `plugins/substrate-codex` | Install the plugin, wire `memory_*` hooks, run the sync command setup, authenticate, and run the smoke test. | Host permission prompts and the exact browser link, then **Approve connection**. Trust plugin hooks once via `/hooks`. | No documented restart requirement; if tools are absent, use the host's documented session action. |
| Hermes | `plugins/substrate-hermes` | Install the plugin, start device login, poll approval, and run the smoke test. | The exact browser link, then **Approve connection**. | No. Verify in the active agent after approval. |
| Other MCP clients | `plugins/substrate-mcp` (fallback) | Use the host's documented remote Streamable HTTP and browser-authorization interface. | Any host setup/permission controls and the exact browser link, then **Approve connection**. | Follow the host's documented lifecycle; do not assume a restart. |

The exact host commands are listed only where this repository has confirmed them. See each package README for the confirmed commands. Do not substitute a manual-token setup. Do not select Hermes merely because a Hermes executable exists elsewhere on the machine. Target the agent currently handling the installation request.

## User-facing flow (every host)

Use this same six-step sequence for every supported integration:

1. **Install.** The agent identifies its current host and configures the supported integration. Ask the user only if that host cannot be determined safely.
2. **Sign in.** Open the exact browser link returned by Substrate or the client's authorization flow. Both arrive at **Connect your agent to Substrate**. Reuse an existing Substrate session; otherwise sign in or create an account normally.
3. **Review.** Use the same connection-name field and requested-access list. Check the connection details against the agent being installed. A displayed name is not proof of identity. Do not hide the verification code or registered callback host when those details are supplied.
4. **Approve connection.** The user approves or denies in the browser. Never approve on the user's behalf. No API keys, manual tokens, or client secrets are requested, displayed, copied, or pasted.
5. **Finish in the agent.** The browser shows **Connection approved**, not a claim that installation is already complete. The client resumes automatically where supported; otherwise return to the agent. It completes authentication privately and runs an authenticated memory smoke test, normally `memory_search`.
6. **Verification.** An empty authenticated result is valid. Only after a successful request report **Connected to Substrate.** If a restart is required, perform or explain it first and verify in the new session before reporting success.

**Connection approved** is consent only; it is not proof of a working client. Never infer authentication from a plugin file, marketplace entry, or browser state. It is not **Connected to Substrate.**

## Self-check before installing

Before changing anything, the agent checks the current host and the available MCP or memory tools. Use this decision protocol:

1. **Tools are present.** Run an authenticated `memory_search` smoke test with a focused, non-secret query. An empty result is valid. If it succeeds, report **Connected to Substrate.** and do not reinstall.
2. **The plugin/server is configured, but tools are absent.** Say plainly that the integration is installed but is not wired into this session. For Cowork, start a new session because registration happens only at session start. Otherwise follow the host's documented restart/session action. Then ask the user to paste this exact prompt in the new session:

   ```text
   Verify my Substrate connection with memory_search and report Connected to Substrate.
   ```
3. **Nothing is configured.** Follow the host installation path below, then return to the six-step flow.
4. **Tools are present but authentication is required.** Do not call this connected. Show the exact browser URL returned by the tool or client and ask the user to open it, sign in, review, and choose **Approve connection**. After approval, finish in the agent and rerun the smoke test.

## Installation mechanics (not separate onboarding)

These are client-specific preparation steps, not different Substrate sign-in flows.

### Claude Code (`plugins/substrate-claude`)

Install the `substrate-claude` package per its README, then complete the common browser flow and run the self-check. The package wires `memory_*` hooks so recall is injected before every turn and every completed turn is captured automatically, including bounded redacted tool calls and results. The host transcript is the spool: if the server reports missing turns, the agent runs the package sync command to re-import them with deterministic ids.

> TODO (docs worker): confirm the exact install commands against `plugins/substrate-claude/README.md` once that package lands, and replace this pointer with the confirmed commands.

Do not use the deprecated API-key plugin described below. Plugin hooks must be trusted once via `/hooks`.

### Cowork (`plugins/substrate-claude`)

Install the `substrate-claude` package through the client's supported interface. Automate it when the host permits it. Otherwise give the exact instructions from the package README, without asking for credentials. The menu flow below was observed working in the current Cowork build; its public official documentation is not yet confirmed:

1. Open the **Cowork** tab.
2. Choose **Customize → Plugins → Personal plugins → + → Add marketplace**.
3. Choose **Add from a repository (GitHub URL)** and enter the repository URL above.
4. Choose **Browse**, then install the Substrate Claude package.
5. Confirm the server URL `https://app.trysubstrate.co/mcp` and **Streamable HTTP** transport.
6. **Start a new Cowork session.** In the current Cowork build, a plugin MCP server is registered only at session start (observed behavior). Nothing opens a browser tab in the old session. If tools still do not appear in the new session, quit and reopen the app.
7. In the new session, paste the self-check prompt. The first memory call opens the browser link. Complete the same Substrate sign-in, review, approval and verification sequence. Trust plugin hooks once via `/hooks` if the host asks.

Required client installation/permission dialogs cannot be bypassed by a repo prompt. The user must perform steps the client does not let the agent automate. Note the launch limit: Claude's SessionStart hook at launch runs before MCP connects, so the first turn implies the boundary. If a Cowork cloud session ends while Substrate is unreachable and is never reopened, its last turn is not recovered.

> TODO (docs worker): confirm the exact Cowork steps against `plugins/substrate-claude/README.md` once that package lands.

### Codex and ChatGPT Work (`plugins/substrate-codex`)

One package serves ChatGPT Work, Codex in the ChatGPT desktop app, and Codex CLI (see `plugins/substrate-codex/README.md`). The package wires `memory_*` hooks so recall is injected before every turn and every completed turn is captured automatically, including bounded redacted tool calls and results. The host transcript is the spool: if the server reports missing turns, the agent runs the package sync skill (`$substrate-sync`) to re-import them with deterministic ids.

ChatGPT / Codex app: open Plugins, add the marketplace from the GitHub repo `Substrate-memory/Substrate-memory-plugins`, install **Substrate Memory**, and start a new chat. Codex CLI: use the `/plugins` browser, or run:

```text
codex plugin marketplace add Substrate-memory/Substrate-memory-plugins
codex plugin add substrate-codex@substrate-marketplace
```

Then start a new session. Direct MCP fallback (no plugin):

```text
codex mcp add substrate-memory --url https://app.trysubstrate.co/mcp
codex mcp login substrate-memory
```

Then complete the common browser flow and run the self-check. Do not use the deprecated API-key plugin described below. Plugin hooks must be trusted once via `/hooks` (until trusted, Codex skips automatic capture). Codex has no session-end hook, so the server seals idle sessions after 30 minutes. On the web, plugins do not deploy scripts, so the sync and import skills need the desktop app or the CLI.

### Hermes (`plugins/substrate-hermes`)

Install with one command from the repository URL: `hermes plugins install https://github.com/Substrate-memory/Substrate-memory-plugins --enable` (the root is a Hermes plugin that loads `plugins/substrate-hermes`; installing the `plugins/substrate-hermes` directory also works). It installs on any Hermes version; outside the tested range it says so once (see [COMPATIBILITY.md](../COMPATIBILITY.md#version-policy)). Run `onboard.py start` from the installed plugin directory, show the user the printed link and code, then `onboard.py poll`; it ends with **Connected to Substrate as you@example.com.** (your account) After a gateway restart the plugin also shows the link and code in chat by itself. Do not inspect another profile or replace its device authorization with a custom OAuth client. Keep existing memory configuration unchanged until a memory request succeeds. Then run the shared **Import past conversations** step below; it is identical for every host.

### Other MCP-capable agents (`plugins/substrate-mcp`, fallback)

Use the thin `plugins/substrate-mcp` package only when no native package fits. Configure the remote Streamable HTTP endpoint `https://app.trysubstrate.co/mcp` through the client's supported interface, per `plugins/substrate-mcp/INSTALL.md`. Do not install another server. The fallback has no hooks and no automatic full-transcript capture guarantee: recall and writes are best effort through the contract v2 `memory_*` tools, and history import works where the host can read its own transcripts.

For another MCP host, use its documented configuration interface. Do not invent a command or bypass client permissions. A client with only local stdio or incompatible OAuth is not automatically supported; never work around that limitation by asking the user for a token.

## Import past conversations (asked once, same for every host)

After **Connected to Substrate.**, the agent asks exactly once:

```text
Do you want to import past conversations into Substrate? I will show you the sessions I can read on this host, and import only what you confirm. Nothing is written before you confirm.
```

If the user says no, do not ask again. If yes:

1. List the sessions this agent can read on this host: its own local session transcripts (Claude and Codex packages provide a sync command that lists them; Hermes and fallback hosts use their own readable history or an export file the user places where the agent can read it). Show the list to the user first. The agent has no access to another app's chat history. If nothing is accessible, say so plainly and stop.
2. After the user confirms the list of sessions, import the confirmed sessions as raw, redacted turns with `--origin history_replay` and a fresh `batch_id` (Claude/Codex sync), so the server stores each turn once under a deterministic id. On hosts without a sync command, write confirmed durable facts with `memory_remember` (one item per call, one new `operation_id` per item, at most 4096 bytes of text). Nothing is written before confirmation.
3. Never import secrets, API keys, tokens, passwords, or private credentials. They are redacted client- and server-side.
4. Report what was imported (for example the `memory_import_status` result) and any failures.

Raw unconfirmed transcripts are never uploaded. This step is the same for every host; only the readable history differs.

## Legacy plugins

The API-key based Claude Code, Codex, and Cowork plugins shipped in August 2026 are deprecated. This includes `substrate_capture`, local paths such as `~/.substrate/*/spool` (including `~/.substrate/claude_code_memory/spool`), and any setup using `SUBSTRATE_API_URL` or `SUBSTRATE_API_KEY`.

Uninstall those legacy plugins. Do not set `SUBSTRATE_API_KEY` by hand on any host; the current product does not issue API keys to users. (The Hermes plugin writes its own private credential after browser approval. Leave that alone.) Their local spools are not migrated; pending spool events do not become Substrate memory. Install the matching v0.8.0 package and complete browser authorization instead.

## Recovery and implementation boundary

For an expired or unavailable link, return to the agent and start connecting again. For a denied request, say that access was not granted. For a transient service error, preserve entered fields and retry safely; do not substitute a manual-token flow. A repository checkout or release label is not evidence that the live connection works.

The Substrate browser screen, consent model and account/entitlement checks are shared. Hermes device polling and MCP authorization-code/PKCE callbacks remain protocol adapters underneath. Their proof details and client-controlled handoff can differ, but there is no separate product onboarding journey.

This common setup does not claim identical delivery mechanics: Hermes uses its profile-local write-ahead spool, while Claude and Codex treat the host transcript as the spool and re-import missing turns with deterministic ids. All hosts speak the same server-side MCP contract (recall before every turn, automatic capture of every completed turn including bounded redacted tool calls and results, session boundaries, subagent capture, explicit remember/forget, evidence).
