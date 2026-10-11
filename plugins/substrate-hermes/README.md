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
- Past conversations: after connecting, one question imports this
  profile's earlier Hermes conversations (tool `substrate_import`).
- Private sign-in: one browser approval per profile. No pasted keys.

## Install

One command, from the repository URL (no scanner override needed):

```sh
hermes plugins install https://github.com/Substrate-memory/Substrate-memory-plugins --enable
```

The repository root is a Hermes plugin that loads this directory.
Installing only this directory also works:
`hermes plugins install Substrate-memory/Substrate-memory-plugins/plugins/substrate-hermes --enable`.
To pin a release, add `--ref <40-character commit SHA of the v0.9.0 tag>`.

## Connect (one browser approval)

The flow is: **Install → Sign in → Review → Approve connection →
Connected to Substrate. → Import past conversations?** The agent chains
the steps itself and only waits for you where you decide.

```sh
python "$(dirname "$(hermes config path)")/plugins/substrate/onboard.py" start
python "$(dirname "$(hermes config path)")/plugins/substrate/onboard.py" poll
```

`start` prints the approval link and a code. The agent shows them and runs
`poll` right away (in the background with `notify_on_complete`, or in the
foreground), so your approval is picked up by itself: you never have to type
"approved". Open the link, sign in, check the code, and choose **Approve
connection**. `poll` ends with **Connected to Substrate as
you@example.com.** (your account) and, when this profile has earlier
conversations, the import question for the agent to ask in the same reply
(add `--json` for machine-readable output). The agent must never approve for
you or ask for a pasted key.

Once the plugin is loaded (after `hermes gateway restart`), you do not need
the CLI: when the profile is not connected, the agent shows the link and
code in chat on your next message, the plugin finishes by itself after you
approve, and the next reply says **Connected to Substrate as you@example.com.**
(your account) and asks about importing past conversations.
Every failure comes with a plain message and a next step.

The plugin stores the tenant-scoped key privately in the active profile
only (`<profile>/.env` plus `<profile>/substrate/credentials/access-token`,
both owner-only). **Connected to Substrate.** is confirmed by an
authenticated `memory_search` call, even with an empty result.

Use the default address `https://app.trysubstrate.co`. If the plugin
reaches Substrate through another address (an old `SUBSTRATE_API_URL`, a
proxy or tailnet name), sign-in still works and the plugin tells you which
setting to remove. Plain `http` is only accepted for a server on the same
machine.

TLS verification stays enabled with the host trust store. Never disable
TLS verification, install a private certificate, or inspect another Hermes
profile.

If an old `substrate_wiki` provider is installed, see `after-install.md`
for the cutover after the first successful memory call.

## Supported Hermes versions

Tested on Hermes 0.21.0 or newer (0.21.4 verified end to end). The plugin
installs and runs on any version. Newer versions get no note; on an older
Hermes it tells you once: *Tested on Hermes 0.21.0 or newer; you are on X.
It should work; if something does not, tell us.* It never upgrades Hermes. See
[COMPATIBILITY.md](../../COMPATIBILITY.md#version-policy).

## Import past conversations

Right after **Connected to Substrate.**, the agent asks once, with real
numbers: *I can also add your 42 earlier Hermes conversations (1,234
messages, March 3, 2026 to October 6, 2026) to your Substrate memory, so I
remember them too. Import all, let me pick, or not now?*

- **Import all** imports every earlier conversation of this profile in the
  background. Live messages keep priority; the agent tells you when it is
  done (*Imported 42 past Hermes conversations (310 turns): 300 new, 10
  already in memory.*).
- **Let me pick** lists recent conversations and imports the ones you choose.
- **Not now** is remembered and never asked again. Ask the agent to import
  your past conversations any time.

What is left out: the current conversation (it is saved live), automated
runs (cron, batch, subagents, webhooks, tests), group chats, hidden and
empty sessions. Text is redacted exactly like live turns, and again by the
server. The source is this profile's `state.db`, read-only; any Hermes
schema from 4 to 30 works. Imports resume after a restart, and a second
import never stores anything twice. The decision lives in
`<profile>/substrate/import-offer.json`, progress in
`<profile>/substrate/import.json`.

Without chat, the same from a terminal:
`onboard.py import` (preview), `--list`, `--yes [--session <id> ...]`,
`--no`, `--status`.

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
writes (`memory_remember`, `memory_forget`) enqueue at explicit priority,
and imported history at replay priority (sent last, a bounded window at a
time).
The spool survives restarts, retries with backoff, and delivers in order.
The plugin fails open: a slow or failed memory call never blocks your turn
(the plugin stops waiting for turn context after 500 ms and injects nothing;
the server's own turn-context deadline is 1500 ms).

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
  record or evidence;
- `substrate_import` (`preview`, `start`, `status`, `decline`) for the
  past-conversation import.

Optional overrides are `SUBSTRATE_API_URL`, `SUBSTRATE_API_KEY`, and
`SUBSTRATE_AGENT_NAME`. The agent name is what you and your agents see in
the Substrate agent pane; the approval page lets you edit it before
approving, and it can be renamed there later.

All server calls use the MCP memory contract v2 over `POST {server}/mcp`
(see [`CONTRACT.md`](CONTRACT.md) section 13 and
[`docs/mcp-contract.md`](../../docs/mcp-contract.md)). Wire schemas and
limits are defined in [`CONTRACT.md`](CONTRACT.md). Runtime code uses only
the Python standard library. Host versions: see *Supported Hermes versions*
above (installs on any version; never upgrades Hermes automatically).
