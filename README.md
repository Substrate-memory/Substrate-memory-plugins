# Substrate Memory Plugins

**Give your agents a memory that follows you across Claude, ChatGPT, Codex and Hermes.**

Substrate remembers your decisions, preferences, people and projects. Connect once, and every supported agent recalls what matters before each turn and saves what matters after it. One connection screen, one review step, one approval. No API keys. No tokens to paste. No secrets to manage.

## What you get

- **Recall before every turn.** Relevant memories are injected as context before the agent answers.
- **Automatic capture of every completed turn.** User messages, assistant answers and bounded redacted tool calls and results are saved.
- **Session boundaries.** Starts, resumes, resets, compacts and ends are recorded, so memory stays scoped to the right session.
- **Subagent capture.** Work done by subagents is routed to the parent session.
- **Durable delivery.** Nothing is stored twice. Hermes uses a profile-local write-ahead spool. Claude and Codex treat the host transcript as the spool: the server reports missing turns and the plugin sync re-imports them with deterministic ids.
- **Explicit remember and forget.** Say "remember this" to keep a fact. Retract one fact without deleting its evidence trail.
- **Evidence.** Check the source behind a recalled fact before you rely on it.
- **Import past conversations.** Asked once, after connection. Only with your confirmation.

## How it works

```text
Claude (Cowork, Claude Code) --> plugins/substrate-claude --\
ChatGPT (Work, Codex app, Codex CLI) --> plugins/substrate-codex --\
Hermes --> plugins/substrate-hermes --------------------------------+--> one server-side
Other MCP-capable agent --> plugins/substrate-mcp (fallback only) --/    MCP contract (v2)
                                                                              |
                                                                              v
                                                              recall + capture + memory store
```

Each package is a thin client. The server owns storage, ranking, redaction checks and the memory contract. Every host speaks the same contract (`docs/mcp-contract.md`, version 2): `memory_turn_context` opens the turn and returns recall, `memory_capture_tool` records tool use, `memory_capture_turn` closes the turn, `memory_session_boundary` marks lifecycle events, `memory_import` replays missed or past turns, and `memory_search`, `memory_expand`, `memory_evidence`, `memory_remember`, `memory_forget` read and write memory. The legacy Hermes `/api/v1` wire stays supported on the server during the rollout.

## Install

Paste this request into the agent you want to connect:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

The agent identifies its host and installs the right package. The flow is the same everywhere: **Install → Sign in → Review → Approve connection → Connected to Substrate.** Only report **Connected to Substrate.** after an authenticated memory smoke test (`memory_search`) succeeds. The browser's **Connection approved** state is consent only; it is not proof that the agent finished connecting. Never paste or request API keys, manual tokens, or client secrets.

| Your host | Package the agent installs | Guide |
|---|---|---|
| Claude Cowork, Claude Code | `plugins/substrate-claude` | [substrate-claude README](plugins/substrate-claude/README.md) |
| ChatGPT Work, Codex app, Codex CLI | `plugins/substrate-codex` | [substrate-codex README](plugins/substrate-codex/README.md) |
| Hermes | `plugins/substrate-hermes` | [substrate-hermes README](plugins/substrate-hermes/README.md) |
| Any other MCP-capable agent (only when no package above fits) | `plugins/substrate-mcp` (thin fallback) | [substrate-mcp README](plugins/substrate-mcp/README.md) |

**Hermes in one command** (the repository root is a Hermes plugin that loads `plugins/substrate-hermes`; it passes the Hermes install scan with no override):

```sh
hermes plugins install https://github.com/Substrate-memory/Substrate-memory-plugins --enable
```

Then follow the printed steps: the agent shows a one-time approval link and code, you approve in the browser, and the agent confirms **Connected to Substrate as you@example.com.** (your account)

If Cowork needs manual installation, use **Cowork tab → Customize → Plugins → Personal plugins → + → Add marketplace → Add from a repository (GitHub URL)**, then browse and install the package. Client-required permission dialogs still need the user. Follow the single [installation and recovery guide](docs/installation.md).

## Security and privacy

- **Browser approval only.** You sign in and choose **Connect your agent to Substrate** in the browser. The agent shows the exact link and never approves for you.
- **No keys to manage.** The product never issues API keys to users and never asks for pasted tokens or client secrets.
- **Redaction on both sides.** Secrets are redacted client-side before sending and again server-side on ingest. Captured tool arguments are bounded (4096 bytes) and tool results are stored as excerpts (8192 bytes plus a digest).
- **Tenant isolation.** Every credential is tenant-scoped. The server owns account identity; clients never send tenant or account ids.
- **What is stored.** Redacted turn content, session boundaries, explicit memories you confirm, and retraction records with their evidence.
- **What is not stored.** Passwords, API keys, tokens, and other secrets are redacted before storage. During history import nothing is written before you confirm.

See [SECURITY.md](SECURITY.md) and [docs/threat-model.md](docs/threat-model.md).

## Import past conversations

After **Connected to Substrate.**, the agent asks once whether to import past conversations. It lists the sessions it can read on that host, shows the list, and imports only what you confirm. Turns are raw, redacted replays from that host's own transcripts. Nothing is written before confirmation. Secrets are redacted client- and server-side. If you say no, the agent does not ask again.

## Supported hosts and versions

| Release | Hermes | Claude Code | Cowork | Codex / ChatGPT Work | Other MCP clients | Status |
|---|---|---|---|---|---|---|
| v0.8.0 | `plugins/substrate-hermes` (tested on Hermes 0.21.0-0.21.x) | `plugins/substrate-claude` | `plugins/substrate-claude` | `plugins/substrate-codex` | `plugins/substrate-mcp` fallback | Released |

Every package installs on any host version. Outside the tested range the agent (and the Hermes plugin itself) shows a friendly note instead of refusing: *Tested on Hermes 0.21.0-0.21.x; you are on X. It should work; tell us if not.* See [COMPATIBILITY.md](COMPATIBILITY.md#version-policy).

Known limits, stated honestly:

- Codex has no session-end hook, so the server seals idle sessions after 30 minutes.
- Claude's SessionStart hook at launch runs before MCP connects, so the first turn implies the boundary.
- If a Cowork cloud session ends while Substrate is unreachable and is never reopened, its last turn is not recovered.
- Plugin hooks in Codex must be trusted once via `/hooks`.
- Cowork needs a new session after install before tools appear.

See [COMPATIBILITY.md](COMPATIBILITY.md) for the full matrix and history.

## FAQ

**Do I need an API key?**
No. Sign in in the browser and approve. There is nothing to copy or paste.

**Which package does my agent install?**
Claude Cowork and Claude Code use `plugins/substrate-claude`. ChatGPT Work, the Codex app and Codex CLI use `plugins/substrate-codex`. Hermes uses `plugins/substrate-hermes`. Any other MCP-capable agent uses the thin `plugins/substrate-mcp` fallback, only when no package fits.

**How do I know it is connected?**
The agent runs an authenticated memory smoke test. Only a passed `memory_search` means **Connected to Substrate.**

**What exactly is sent to Substrate?**
Each completed turn: your message, the agent's answer, and bounded tool calls and results, all redacted before sending and again on the server. History import sends the same redacted turn content, and only for the sessions you confirm.

**What about my old August 2026 API-key plugin?**
Those plugins (`substrate_capture`, local spool paths, `SUBSTRATE_API_KEY` setups) are deprecated. Uninstall them and connect with the browser flow. Their local spools are not migrated.

**Can I roll back?**
Yes. The immutable `v0.5.0` and `v0.6.0` tags remain available for rollback.

## Development and releases

```bash
uv sync --frozen --extra dev
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
python3 scripts/check_public_hygiene.py --root .
.venv/bin/python scripts/build_release.py
.venv/bin/python scripts/build_release.py --check
```

Releases are staged and gated; no tag or publication happens from a review branch. See [docs/releasing.md](docs/releasing.md).

## Links

- [Installation and recovery guide](docs/installation.md)
- [Architecture](docs/architecture.md)
- [Source-of-truth boundary](docs/source-of-truth.md)
- [Compatibility](COMPATIBILITY.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [MCP contract v2](docs/mcp-contract.md) (single source of truth for the memory tools)
