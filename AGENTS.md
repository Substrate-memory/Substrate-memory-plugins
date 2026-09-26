# Agent installation contract

For the request:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

Follow [the single installation and recovery guide](docs/installation.md).

## 1. Identify the host you are running in

Pick the package for the agent that is handling this request right now:

- Claude Cowork or Claude Code → `plugins/substrate-claude`.
- ChatGPT Work, Codex app, or Codex CLI → `plugins/substrate-codex`.
- Hermes → `plugins/substrate-hermes`.
- Any other MCP-capable agent → `plugins/substrate-mcp`, and only as a fallback when no package above fits.

Never pick a host because its binary happens to exist on the machine. Do not select Hermes merely because a Hermes executable exists elsewhere. Target the agent currently handling the installation request. Ask the user which host this is only when it cannot be determined safely.

Check the host version against the tested range in [COMPATIBILITY.md](COMPATIBILITY.md#version-policy). If it is outside the range, install anyway and tell the user once: *Tested on <host> <range>; you are on <version>. It should work; if something does not, tell us at https://github.com/Substrate-memory/Substrate-memory-plugins/issues.* Never refuse and never change the host version.

For Hermes, one command installs from the repository URL (the root is a Hermes plugin that loads `plugins/substrate-hermes`): `hermes plugins install https://github.com/Substrate-memory/Substrate-memory-plugins --enable`. Then follow the printed steps.

## 2. Six-step flow

Keep the user-facing sequence the same everywhere:

1. **Install.** Configure the supported integration for the identified host.
2. **Sign in.** Open the exact browser link returned by Substrate or the client authorization flow. Both arrive at **Connect your agent to Substrate**. Reuse an existing Substrate session; otherwise sign in or create an account normally.
3. **Review.** Use the same connection-name field and requested-access list. Check the connection details against the agent being installed. A displayed name is not proof of identity. Do not hide the verification code or registered callback host when those details are supplied.
4. **Approve connection.** The user approves or denies in the browser. Never approve on the user's behalf.
5. **Finish in the agent.** The browser shows **Connection approved**, not a claim that installation is already complete. The client resumes automatically where supported; otherwise return to the agent. It completes authentication privately and runs an authenticated memory smoke test, normally `memory_search`.
6. **Verify.** An empty authenticated result is valid. Only after a successful request report exactly **Connected to Substrate.** If a restart is required (Cowork needs a new session after install; Codex hooks must be trusted once via `/hooks`), perform or explain it first and verify in the new session before reporting success.

**Connection approved** is consent only. It is not **Connected to Substrate.**

## 3. Self-check before installing

1. **Tools are present.** Run an authenticated `memory_search` smoke test with a focused, non-secret query. An empty result is valid. If it succeeds, report **Connected to Substrate.** and do not reinstall.
2. **The plugin/server is configured, but tools are absent.** Say plainly that the integration is installed but is not wired into this session. For Cowork, start a new session because registration happens only at session start. Otherwise follow the host's documented restart/session action. Then ask the user to paste this exact prompt in the new session:

   ```text
   Verify my Substrate connection with memory_search and report Connected to Substrate.
   ```
3. **Nothing is configured.** Follow the host installation path in the guide, then return to the six-step flow.
4. **Tools are present but authentication is required.** Do not call this connected. Show the exact browser URL returned by the tool or client and ask the user to open it, sign in, review, and choose **Approve connection**. After approval, finish in the agent and rerun the smoke test.

Never infer authentication from a plugin file, marketplace entry, or browser state. Automate client setup where supported. For client-owned actions you cannot perform, give precise instructions from the shared guide, then resume the same flow. Never invent a local MCP server, bypass permissions, or use a pasted-token fallback. Preserve the Hermes runtime and active-profile credential isolation.

## 4. Import past conversations (asked once)

After **Connected to Substrate.**, ask exactly once:

```text
Do you want to import past conversations into Substrate? I will show you the sessions I can read on this host, and import only what you confirm. Nothing is written before you confirm.
```

If the user says no, do not ask again. If yes:

1. List the sessions this agent can read on this host (its own local transcripts, via the package sync command where one exists). Show the list first. The agent has no access to another app's chat history. If nothing is accessible, say so plainly and stop.
2. Import only the confirmed sessions as raw, redacted turns (`memory_import` with `--origin history_replay` and a fresh `batch_id` where the package supports it; otherwise confirmed facts via `memory_remember`). Nothing is written before confirmation.
3. Never import secrets, API keys, tokens, passwords, or private credentials. They are redacted client- and server-side.
4. Report what was imported and any failures.

## 5. Rules

- Never request API keys, manual tokens, or client secrets. Never ask the user to paste a secret. Authentication uses browser consent owned by the backend.
- Report the exact success phrase **Connected to Substrate.** only after the authenticated smoke test succeeds.
- Keep the endpoint `https://app.trysubstrate.co/mcp` over remote Streamable HTTP. Do not substitute another server or a user-provided secret endpoint.
