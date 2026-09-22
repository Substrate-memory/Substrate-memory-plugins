# Substrate memory for Hermes

Long-term memory for your Hermes agent. The agent recalls facts from your
knowledge base during each turn, saves completed turns automatically, and
lets you store or correct facts on demand. The server owns storage,
ranking, and evidence. The plugin caches no retrieved memory.

What you get:

- Turn context: relevant facts appear in `<memory-context>` lines each
  turn. Use them naturally.
- Durable capture: every completed turn and session boundary is saved
  through a profile-local write-ahead spool. Nothing is fire-and-forget:
  if the network is down, turns wait in the spool and send later.
- Memory tools: `memory_search`, `memory_expand`, `memory_evidence`,
  `memory_remember`, and `memory_forget`.
- Private sign-in: one browser approval per profile. No pasted keys.

## Install

Install this directory, not the repository root:

```sh
substrate_ref="$(git ls-remote https://github.com/Substrate-memory/Substrate-memory-plugins.git refs/tags/v0.7.0 | awk '{print $1}')"
printf '%s\n' "$substrate_ref" | grep -Eq '^[0-9a-f]{40}$'
hermes plugins install \
  Substrate-memory/Substrate-memory-plugins/plugins/substrate-hermes \
  --ref "$substrate_ref" --no-enable
```

The install flow is: **Install → Sign in → Review → Approve connection
→ Connected to Substrate.** Connect the active profile with the bundled
login CLI (standard library only, no `PYTHONPATH` needed):

```sh
python <plugin-dir>/onboard.py start --json
```

When a `verification_uri_complete` link appears (from the login CLI, from
`memory_search`, or on the first turn), open that exact link in a browser,
sign in, and approve the connection. The agent must never approve it for
you or ask for a pasted key. Then wait for approval:

```sh
python <plugin-dir>/onboard.py poll --json
python <plugin-dir>/onboard.py status --json
```

The plugin stores the tenant-scoped key privately in the active profile
only (`<profile>/.env` plus `<profile>/substrate/credentials/access-token`,
both owner-only). If `memory_search` reports `authorization_required`, run
the commands above and retry. **Connected to Substrate.** is confirmed by
an authenticated `memory_search` call, even with an empty result.

TLS verification stays enabled with the host trust store. Never disable
TLS verification, install a private certificate, or inspect another Hermes
profile.

After setup succeeds, disable `substrate_wiki` (if an old provider is
installed), enable `substrate`, restart the gateway, and verify
`memory_search` in a new turn. Do not change `memory.provider` before a
memory call succeeds.

## Import past conversations

After **Connected to Substrate.**, the agent asks once whether to import
earlier sessions from this host. On yes, it shows the local session list
and imports only the sessions you confirm. Turns already saved are never
stored twice. If a turn ever reports earlier turns as not saved yet, the
agent syncs the missing turns on request.

## Privacy and redaction

Secrets never leave your machine in readable form. Before anything is
sent, the plugin redacts API keys, tokens, passwords, and similar values
from text and tool traffic, drops system messages, and bounds every
field. Captured turns include bounded redacted tool traffic (tool call
arguments up to 4096 bytes, tool result excerpts up to 8192 bytes plus a
digest). The server redacts again on receipt. The shared redaction rules
are checked against `contract/redaction-fixtures.json`.

## Durability

Capture and boundary events enqueue into a profile-local write-ahead spool
(`<profile>/substrate/spool`, owner-only) at live priority, and explicit
writes (`memory_remember`, `memory_forget`) enqueue at explicit priority.
The spool survives restarts, retries with backoff, and delivers in order.
The plugin fails open: a slow or failed memory call never blocks your turn
(turn context gives up after 500 ms and injects nothing).

## Runtime

The plugin registers:

- `pre_llm_call` for validated turn context;
- `post_llm_call`, fired once per turn by the host with the finalized
  conversation (the sole live-capture entry point);
- `on_session_end`, which fires at the end of every turn and intentionally
  emits nothing;
- `on_session_finalize` for the true content-free session-end boundary;
- `on_session_reset`, fired with the new session id after rotation;
- `subagent_start` / `subagent_stop` for parent-routed capture;
- `memory_search`, `memory_expand`, and `memory_evidence` for retrieval;
- `memory_remember` (`text` plus `durability` in `durable`,
  `time_bounded`, `transient`), which returns the server's `m:` handle;
- `memory_forget` (exactly one handle plus a required non-empty reason),
  which marks one memory atom as no longer true without deleting its
  record or evidence.

Optional overrides are `SUBSTRATE_API_URL`, `SUBSTRATE_API_KEY`, and
`SUBSTRATE_AGENT_NAME`. The agent name is what you and your agents see in
the Substrate agent pane; the approval page lets you edit it before
approving, and it can be renamed there later.

All server calls use the MCP memory contract v2 over `POST {server}/mcp`
(see [`CONTRACT.md`](CONTRACT.md) section 13 and
[`docs/mcp-contract.md`](../../docs/mcp-contract.md)). Wire schemas and
limits are defined in [`CONTRACT.md`](CONTRACT.md). Runtime code uses only
the Python standard library. Supported host: Hermes 0.21.0 exactly (the
tested version); do not install on an unverified host version and do not
upgrade Hermes automatically.
