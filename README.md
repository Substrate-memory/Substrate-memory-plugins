# Substrate Memory Plugins

This is the official public repository for the Substrate memory plugin for
Hermes 0.21.0. The current release is
[`v0.5.0`](https://github.com/Substrate-memory/Substrate-memory-plugins/releases/tag/v0.5.0):
durable v5 Hermes `substrate` content at `0.4.0` plus the five host adapters
at `0.4.0` (byte-identical to the `v0.4.0` release). Older releases
[`v0.3.0`](https://github.com/Substrate-memory/Substrate-memory-plugins/releases/tag/v0.3.0)
and [`v0.4.0`](https://github.com/Substrate-memory/Substrate-memory-plugins/releases/tag/v0.4.0)
keep working and are preserved for rollback. New installs pin `v0.5.0`.

## Install with a Hermes agent

The user only needs to say:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

### Required behavior for the installing agent

Treat the sentence above as approval to install and configure the current Substrate plugin
for the active Hermes profile. Do not ask the user for repository paths, commands, commit
hashes, API keys, or TLS workarounds.

1. Confirm `hermes --version` is exactly 0.21.0 (the tested version). Stop on any other version. Do not upgrade Hermes automatically.
2. Install the plugin subdirectory at the released tag, not the repository root:

   ```sh
   substrate_ref="$(git ls-remote https://github.com/Substrate-memory/Substrate-memory-plugins.git refs/tags/v0.5.0 | awk '{print $1}')"
   printf '%s\n' "$substrate_ref" | grep -Eq '^[0-9a-f]{40}$'
   hermes plugins install \
     Substrate-memory/Substrate-memory-plugins/plugins/substrate \
     --ref "$substrate_ref" --no-enable
   ```

3. Run `hermes plugins doctor substrate --ci`. Stop without changing the legacy provider
   if installation or doctor fails.
4. Enable the plugin and connect the active profile with the bundled login CLI
   (or let first use start it automatically). When a
   `verification_uri_complete` link appears (from `onboard.py start --json`,
   from `memory_search`, or on the first turn), present that exact clickable
   URL to the user:

   ```sh
   python <plugin-dir>/onboard.py start --json
   python <plugin-dir>/onboard.py poll --json
   ```

   The user approves in their browser; the agent must never approve it for
   them or ask for a pasted key. No API key is ever requested, shown, or
   pasted into chat.
5. The plugin stores the tenant-scoped key privately in the active profile
   (`<profile>/.env` plus `<profile>/substrate/credentials/access-token`,
   both owner-only) and retries automatically. No manual credential migration
   is required. Login connects recall and capture only; it never uploads
   prior conversation history.
6. Only after a memory call succeeds, disable `substrate_wiki` (if an old provider is
   installed), clear an old `memory.provider: substrate_wiki` selection with
   `hermes memory off` (disables the external provider only; built-in local
   memory stays), and restart the gateway. This prevents duplicate capture while
   preserving built-in local memory.
7. The `setup.py` compat entry point can pre-connect during installation
   (`python <plugin-dir>/setup.py --hermes-home <home>`), but it is not required.
8. Verify the new `memory_search` tool in a new turn. `wiki_search` is legacy and is not a
   valid verification of the current plugin.
9. Report success only after `memory_search` reaches the authenticated Substrate API.
   Fail closed and report the exact safe error class otherwise.

TLS verification must remain enabled with the host trust store. The plugin
never trusts a replacement leaf certificate or a private certificate, never
disables certificate or hostname checks, and never inspects another Hermes
profile.

## Install on other hosts

Every supported host installs from this same repository. The user only needs to say:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

to an agent running on that host. The installing agent follows the linked runbook for
its host: it installs the plugin subdirectory (never the repository root), lets device
onboarding run on first use, and verifies with `memory_search` in a new turn.

| Host | Install (one line) | Runbook |
|---|---|---|
| Hermes 0.21.0 (tested) | Say the sentence above to a Hermes agent | `plugins/substrate/README.md` |
| Claude Code | Say the sentence above in Claude Code (`claude plugin install substrate-memory@substrate-marketplace`) | [plugins/claude-code/INSTALL.md](plugins/claude-code/INSTALL.md) |
| Claude Cowork | Say the sentence above in Cowork (`claude plugin install substrate-cowork`) | [plugins/claude-cowork/INSTALL.md](plugins/claude-cowork/INSTALL.md) |
| Codex CLI | `sh plugins/codex/install.sh` from a sparse checkout, then `codex plugin add substrate@substrate-memory` | [plugins/codex/INSTALL.md](plugins/codex/INSTALL.md) |
| Grok | Say the sentence above to a Grok agent, or run `sh plugins/grok-bot/install.sh` | [plugins/grok-bot/INSTALL.md](plugins/grok-bot/INSTALL.md) |
| OpenClaw | `openclaw plugins install --link ./Substrate-memory-plugins/plugins/openclaw --force`, then `openclaw plugins enable substrate` | [plugins/openclaw/INSTALL.md](plugins/openclaw/INSTALL.md) |

Behavior is the same on every host: first use starts RFC 8628 device authorization
and the exact `verification_uri_complete` approval URL is shown through the agent
(never paste a key into chat), the tenant-scoped key is stored privately in the
active profile, and every failure is fail-closed with a bounded error class. TLS
verification stays enabled everywhere.

## Current plugin

The `substrate` plugin (content `0.4.0`) is a durable, standard-library-only
adapter. It provides:

- bounded next-turn memory context through `pre_llm_call`;
- durable completed-turn capture through `post_llm_call` into a write-ahead
  spool (live priority; nothing is fire-and-forget);
- true session completion markers through `on_session_finalize`, session
  rotation through `on_session_reset`, and parent-routed subagent capture
  through `subagent_start`/`subagent_stop`, so ended sessions are
  materialized into the extraction pipeline automatically;
- `memory_search`, `memory_expand`, and `memory_evidence`;
- `memory_remember` (explicit durable writes) and `memory_forget`
  (single-handle retraction with reason, record and evidence retained);
- passwordless device authorization through the bundled `onboard.py` login
  CLI (`start`/`status`/`poll --json`), with pre-login capture held pending
  (never quarantined) until approval;
- agent display names, selectable on the approval page and visible in the agent pane.

The Substrate server remains the source of truth for storage, ranking, the associative
editor, and evidence.

## Privacy boundary

Visible prompts and assistant output may be sent to the configured Substrate server after
redaction. Completed turns also carry bounded redacted tool traffic: tool call
arguments (canonical JSON, at most 4096 bytes, or a truncated digest form) and
tool result excerpts (at most 8192 bytes plus a SHA-256 digest of the full
redacted result). System messages are never captured or uploaded. Retrieved
memory is never cached locally; the plugin returns empty context on any
retrieval failure. Unsent capture waits only in the profile-local durable
spool (`<profile>/substrate/spool`, owner-only files) until delivery or
bounded eviction. Login connects recall and capture only; prior conversation
history is never uploaded by installation or login.

Read [SECURITY.md](SECURITY.md) and [the threat model](docs/threat-model.md) before
deployment.

## Development

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev python -m pytest -q
python3 scripts/check_public_hygiene.py --root .
uv run --frozen --extra dev python scripts/build_release.py --check
```

## Repository map

- `plugins/substrate/` — the Hermes 0.21.0 retrieval plugin and setup flow.
- `plugins/claude-code/` — the Claude Code host adapter (MCP server plus hooks).
- `plugins/claude-cowork/` — the Claude Cowork host adapter (MCP server plus hooks).
- `plugins/codex/` — the Codex CLI host adapter (MCP server, hooks, skill).
- `plugins/grok-bot/` — the Grok host adapter (MCP server plus turn bridge).
- `plugins/openclaw/` — the OpenClaw adapter (zero-dependency JS entry plus Python core).
- `scripts/` — deterministic release builder and public-hygiene check.
- `tests/` — contract, behavior, transport, onboarding, and packaging tests.
- `docs/` — architecture, ownership, and threat-model contracts.

## License

MIT © 2026 Sightline Technologies Inc. See [LICENSE](LICENSE).
