# Substrate MCP memory contract (v2)

`mcp_contract_version` **2**. This document is the single source of truth for the
Substrate memory MCP surface used by every Substrate plugin: `substrate-hermes`,
`substrate-claude`, `substrate-codex`, and the thin `substrate-mcp` fallback. The
server implementation lives in the Substrate server repository
(`agent-api/mcp-api.mjs`); the clients live in this repository. Both sides ship
exactly one version of this contract.

Conventions: all string sizes are UTF-8 byte counts; "≤ N" means at most N bytes;
timestamps are RFC 3339 UTC; handles match `^[mp]:[0-9a-f]{8,64}$`; input schemas
are **closed** (an unknown argument is `invalid_request`). Tool results carry the
same JSON in `structuredContent` and, unless a tool says otherwise, in
`content[0].text`. The hook and import tools (4.3-4.8) answer with `"contract_version": 2`; the memory tools (`memory_search`, `memory_expand`, `memory_evidence`, `memory_shares`, `memory_remember`, `memory_forget`) answer with `"contract_version": 1` on the current server, and clients accept 1 or 2 for them. `memory_shares` is offered only when the server has sharing wired; clients must not require it.

The legacy Hermes wire (`/api/v1/*`, `CONTRACT.md` contract_version 1) stays
supported on the server during the v0.7.0 rollout. Its **envelope schema is reused
unchanged** by this contract (section 5), so extraction downstream is identical for
every host.

## 1. Endpoint, transport, authentication

- Endpoint: `https://app.trysubstrate.co/mcp`, MCP Streamable HTTP, stateless
  (no `MCP-Session-Id`). Clients must accept both `application/json` and
  `text/event-stream` responses.
- Server identity: `initialize` → `serverInfo.name == "substrate-memory"`,
  `serverInfo.version` is the server release; `instructions` starts with
  `substrate-mcp-contract/2`. A client refuses to run against any other value.
- Authentication, one of:
  - **MCP OAuth** (authorization code + PKCE, dynamic client registration, resource
    metadata at `/.well-known/oauth-protected-resource/mcp`). Used by Claude,
    Cowork, Codex, ChatGPT and every other MCP host. Scopes: `retrieve capture`.
  - **Device-grant key** `sk_sub_…` obtained through `POST /oauth/device_authorization`
    (RFC 8628, `client_id=substrate-hermes`). Used by Hermes, whose plugin holds its
    own credential for the offline spool. Sent as `Authorization: Bearer sk_sub_…`.
  Both credentials are tenant-scoped agent credentials with the same scopes and the
  same **Connect your agent to Substrate** approval screen.
- Request body limit on `/mcp`: **262144 bytes** (was 32 KiB; raised for import
  batches). Larger → HTTP 413.
- Scopes per tool: `retrieve` for `memory_search`, `memory_expand`,
  `memory_evidence`, `memory_shares`, `memory_turn_context`, `memory_import_status`;
  `capture` for `memory_remember`, `memory_forget`, `memory_capture_tool`,
  `memory_capture_turn`, `memory_session_boundary`, `memory_import`,
  `memory_import_ticket`.
- **Import ticket** `sk_imp_…` (from `memory_import_ticket`, 4.9): a short-lived
  bearer for a local, non-model import script on `/mcp`, limited to
  `memory_import` and `memory_import_status`.

## 2. Error shape

A failed tool call returns `isError: true` with
`{"contract_version": 2, "error": <category>, "hint"?: <≤200>}`. Categories:
`unauthorized`, `forbidden`, `invalid_request`, `payload_too_large`, `not_found`,
`conflict`, `rate_limited`, `result_too_large`, `internal`. Transport-level
failures (HTTP 401/403/413/429/5xx) keep their existing JSON bodies. Clients never
retry `invalid_request`, `not_found`, `forbidden`; they may retry the rest with
backoff. Hooks always **fail open**: a failed memory call never blocks the host.

## 3. Platforms and sessions

- `platform` (≤64): `hermes`, `claude` (Claude Code, Cowork), `codex` (Codex CLI,
  Codex app, ChatGPT Work), `other`. The agent's display name comes from its
  credential (OAuth client name or Hermes agent name), never from tool arguments.
- `session_id` (1..512): the host's session id, passed through unchanged.
- `turn_id` (≤128, optional): host turn id when the host provides one (Codex).
  When absent the server numbers turns per session.
- `agent_context`: `main` or `subagent`. Subagent captures are routed to the
  parent session (`parent_session_id`), as in Hermes.
- The server keeps one **pending turn** per `(agent, session_id)`: opened by
  `memory_turn_context`, extended by `memory_capture_tool`, closed by
  `memory_capture_turn`. Closing writes one canonical `capture_turn` ledger
  envelope (section 5). A pending turn older than 6 hours is closed as-is.
- A session with no activity for **30 minutes** is sealed by the server
  (`capture_session` with `boundary: "end"`, `session_complete: true`) unless a
  boundary already sealed it. Hosts that expose no session-end event therefore
  still get sealed extraction windows.

## 4. Tools

### 4.1 Read tools (unchanged from the current server)

`memory_search {query: 1..4096, kinds?: [≤16 ≤32], limit?: 1..20, share?}`,
`memory_expand {handle, share?}`, `memory_evidence {handle, raw?, limit?, share?}`,
`memory_shares {}` keep their current schemas and results. `memory_search` is the
connection smoke test: an authenticated call, even with an empty result, proves
**Connected to Substrate.**

### 4.2 Write tools (unchanged)

`memory_remember {operation_id, text: 1..4096, about?: ≤256, durability?}` and
`memory_forget {operation_id, handle, reason?: ≤1024}` keep their current schemas
and results.

### 4.3 `memory_turn_context` (scope `retrieve`) — hook: prompt submitted

Input:

```
{session_id, prompt: ≤16384, platform, turn_id?, agent_context?: "main"|"subagent",
 parent_session_id?: ≤512}
```

The server redacts `prompt`, opens the pending turn with it as the `user`
message, runs recall for this turn, and returns:

```
{contract_version: 2, session_id, turn: int ≥ 0, block: ≤8192 (≤40 lines),
 handles: [≤64 handle], missing_turns: int ≥ 0, brief_version: int ≥ 0,
 latency_ms: number, empty_reason: ""|"no_candidates"|"gated"|"not_implemented"}
```

**Text content rule.** `content[0].text` is the text a host injects as
additional context, so it is NOT the JSON above. It is:

1. the `block` verbatim (may be empty), then
2. if `missing_turns > 0`, one line:
   `[substrate] <N> earlier turn(s) of this session are not saved yet. Run the Substrate sync command.`

An empty `block` with `missing_turns == 0` yields an empty string (nothing is
injected). `missing_turns` is the count of server-numbered turns in this session
that were opened but never closed, or that the client's own numbering reveals as
absent; the server never blocks on it. Server deadline 500 ms; a slow recall
returns `block: ""`, `empty_reason: "gated"`.

### 4.4 `memory_capture_tool` (scope `capture`) — hook: tool used

Input:

```
{session_id, tool_use_id: 1..128, tool_name: 1..128, tool_input: any JSON,
 tool_response: any JSON, platform, turn_id?, agent_context?, parent_session_id?}
```

`tool_input` and `tool_response` may arrive as a JSON value or as a string
containing JSON (hosts differ in placeholder typing); the server accepts both.
The server redacts and bounds them exactly as section 6 describes (tool call
arguments ≤4096 bytes canonical, result excerpt ≤8192 bytes plus SHA-256 digest of
the full redacted result) and appends an assistant `tool_calls` entry plus a
`tool` message to the pending turn. Calls to Substrate's own `memory_*` tools are
ignored by the server (`action: "ignored"`), so hooks may match every tool.
Output: `{contract_version: 2, session_id, turn, tool_index: int, action: "recorded"|"ignored"}`.
Text content: `""`.

### 4.5 `memory_capture_turn` (scope `capture`) — hook: agent finished the turn

Input:

```
{session_id, assistant_message: ≤16384, platform, turn_id?,
 agent_context?: "main"|"subagent", agent_id?: ≤128, parent_session_id?: ≤512}
```

Closes the pending turn: the redacted `assistant_message` becomes the final
`assistant` message; the server builds the `capture_turn` envelope (section 5) with
`capture_origin: "live"`, `event_id` = UUID v4, and writes it through the same
ledger path as Hermes. If no pending turn exists (for example the prompt hook did
not fire), the server creates a turn holding only the assistant message. For
`agent_context: "subagent"` the turn is written under `parent_session_id` with
`speaker.role: "agent"` and `speaker.id: agent_id`.
Output: `{contract_version: 2, event_id, action: "stored"|"duplicate"|"sealed"|"queued", session_id, turn}`.
Text content: `""`.

### 4.6 `memory_session_boundary` (scope `capture`) — hook: session lifecycle

Input:

```
{session_id, boundary: "startup"|"resume"|"clear"|"compact"|"end"|
                       "switch"|"reset"|"compress"|"rewound",
 platform, next_session_id?: ≤512, parent_session_id?: ≤512, reason?: ≤64}
```

Host values map to canonical boundaries: `startup|resume → switch`,
`clear → reset`, `compact → compress`, `end → end` (with
`session_complete: true`). Canonical values pass through. The server writes a
content-free `capture_session` envelope (section 5.3) with the server's
`message_high_water`. Output: `{contract_version: 2, event_id, action, boundary}`.
Text content: `""`.

### 4.7 `memory_import` (scope `capture`) — spool drain, catch-up, history

Input: `{items: [1..64 item], batch_id?: 8..64 hex}`.

An `item` is a **ledger envelope from section 5 without `schema_version`,
`contract_version`, and (optionally) `event_id`**:

```
{event_id?: uuid, kind, session_id, offset: {start, end}, capture_origin:
 "live"|"history_replay"|"catchup", batch_id: ""|hex, speaker, created_at, payload}
```

- Allowed kinds: `capture_turn`, `capture_session`, `memory_write`,
  `memory_forget`, `consent`.
- `event_id` present (Hermes live spool): used as is; the server answers
  `duplicate` for a replay.
- `event_id` absent (catch-up, history): the server derives it deterministically
  as `uuid5(6f3a2b1c-9d8e-4f70-a1b2-c3d4e5f60718, canonical_json({kind, session_id, offset, payload}))`
  (CONTRACT.md §5.10), so re-sending the same turn never stores twice.
- Every payload is validated by the same rules as `/api/v1/ledger/events`
  (CONTRACT.md §5.12 steps 3–9) and redacted again server-side.
- Items are processed in order; one invalid item does not reject the others.
- Whole request ≤ 262144 bytes.

Output:

```
{contract_version: 2, batch_id, accepted: int, rejected: int,
 results: [{index, event_id?, action: "stored"|"duplicate"|"sealed"|"queued"|"rejected", error?}]}
```

Text content: a one-line summary, e.g. `Imported 12 items (10 stored, 2 duplicate).`

### 4.8 `memory_import_status` (scope `retrieve`)

Input: `{session_id?: ≤512, batch_id?: hex}` (at least one).
Output for a session: `{contract_version: 2, session: {session_id, turns_stored,
message_high_water, complete: bool, last_event_at}}`. Output for a batch: the
`/api/v1/import-status` fields (`events_received, sessions_seen, sessions_completed,
extracted, pending, complete, last_event_at`). Text content: JSON.

Clients use `message_high_water` to send only the turns the server does not hold.

### 4.9 `memory_import_ticket` (scope `capture`) — bulk history without the model

Input (closed): `{platform: ≤64, batch_id?: 8..64 hex}` (bookkeeping only).

Output (`structuredContent`; text is the same JSON):

```
{contract_version: 2, ticket: "sk_imp_…", mcp_url: "<origin>/mcp", expires_at, expires_in: 3600,
 max_items: 64, max_batch_bytes: 245760}
```

- The ticket is a bearer for `POST mcp_url` (same Streamable HTTP JSON-RPC) for a
  local script the agent runs. Agents pass it through the environment
  (`SUBSTRATE_IMPORT_TICKET`, `SUBSTRATE_MCP_URL`), never argv, never chat text.
- Bound to the minting tenant, agent and credential; expires after 60 minutes;
  every use re-checks the parent credential, so disconnecting the agent or
  suspending the tenant ends it (`401` / `403`).
- Allowed: `initialize`, `ping`, notifications, `tools/list` (exactly
  `memory_import`, `memory_import_status`), and `tools/call` of those two. Any
  other tool is `forbidden`; any other method is HTTP 403.
- Through a ticket, `memory_import` accepts only `capture_turn` /
  `capture_session` items with `capture_origin` `history_replay` or `catchup`;
  any other item is rejected on its own (`error: "forbidden"`).
- Limits are `memory_import`'s (64 items, 262144-byte body). Calling the tool
  again mints a fresh ticket. On `401` the script stops and asks the agent for a
  new one; re-sending is safe (4.7 dedupe).

## 5. Ledger envelope (shared with CONTRACT.md §5, unchanged)

The server stores every capture, from every host, as the schema_version 3
envelope defined in `plugins/substrate-hermes/CONTRACT.md` §5:
`capture_turn` payload `{turn_id, messages: [{index, role: user|assistant|tool,
content, timestamp?, speaker?, tool_calls?, tool_call_id?, tool_name?,
result_digest, result_bytes, result_truncated?}]}`, `capture_session` payload
`{boundary, session_complete, next_session_id?, parent_session_id?,
message_high_water, platform, chat_type, participants?}`, plus `memory_write`,
`memory_forget`, `consent`. Limits: event ≤ 262144 bytes, tool call args ≤ 4096,
tool result excerpt ≤ 8192, ≤ 4096 messages per turn, ≤ 64 tool calls per message.
`chat_type` for MCP hosts is `direct`.

## 6. Redaction (identical on every side)

Applied client-side by `substrate-hermes` and by the sync script, and **always**
server-side on ingest for every tool in section 4. Both implementations must pass
the shared fixture `contract/redaction-fixtures.json` in this repository.

1. Text: `(?i)(\b(?:authorization|api[_-]?key|access[_-]?token|token|password|secret)\b\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+`
   → keep group 1, replace the value with `[REDACTED]`.
2. Text: `\bsk_[A-Za-z0-9_-]{8,}\b` → `[REDACTED]`.
3. Objects: any key matching
   `(?i)(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|private[_-]?key)`
   has its value replaced by `[REDACTED]` at any depth (max depth 8, max 128 keys
   or items per level; deeper or longer → `[TRUNCATED]`).
4. Strings inside objects are bounded to 8192 bytes, other scalars to 1024;
   non-finite numbers → `[NONFINITE]`; integers outside ±2^53−1 → strings.
5. Invalid UTF-8 is replaced, never rejected. Clipping is on UTF-8 byte boundaries.

## 7. Host hook wiring (reference)

Both Claude Code/Cowork and Codex/ChatGPT Work use `type: "mcp_tool"` hooks
against the plugin's bundled remote server, with `${field}` substitution from the
hook event. Reference mapping (the plugin packages ship the exact files):

| Host event | Tool | Arguments |
|---|---|---|
| `UserPromptSubmit` | `memory_turn_context` | `session_id, prompt, platform` (+ `turn_id` on Codex) |
| `PostToolUse` (all tools) | `memory_capture_tool` | `session_id, tool_use_id, tool_name, tool_input, tool_response, platform` |
| `Stop` | `memory_capture_turn` | `session_id, assistant_message=last_assistant_message, agent_context: main, platform` |
| `SubagentStop` (Codex only) | `memory_capture_turn` | `… agent_context: subagent, agent_id, parent_session_id=session_id` |
| `SessionStart` | — (Codex only; Claude Code rejects `mcp_tool` hooks on this event) | |
| `PreCompact` | `memory_session_boundary` | `boundary: compact` |
| `SessionEnd` | — (Claude Code and Codex both refuse MCP tool hooks here) | |

Timeouts: 5 s for every hook. Every hook fails open. Both packages add one `type: "command"` hook on `UserPromptSubmit`: `substrate_sync.py --offer-check` (section 8), local and silent once the import offer is answered. Known host limits,
documented to users (verified on Claude Code 2.1.289): Claude Code refuses
`mcp_tool` hooks on `SessionStart` and `SessionEnd` (no MCP client context), so
the first turn implies the start and the server seals a session after 30
minutes idle. The Claude plugin declares no `SubagentStop` hook: Claude Code
fires it for its own helpers too (the compaction summary was stored as a
turn), and a real subagent's result already arrives as the main agent's Agent
tool result through `PostToolUse`.

## 8. Sync and history import (client side)

`scripts/substrate_sync.py` (standard library only, byte-identical in
`substrate-claude` and `substrate-codex`; transcripts at
`$CLAUDE_CONFIG_DIR|~/.claude/projects/**/*.jsonl` without `subagents/`, and
`$CODEX_HOME|~/.codex/sessions/**/*.jsonl`; state in
`<that config dir>/substrate-memory/` or `$SUBSTRATE_DATA_DIR`):

- `--list` / `--preview` — local sessions (`--preview` prints one JSON line
  `{sessions, turns, first_at, last_at, bytes_estimate, excluded, decision}`).
- `--session <id> [--after-index N] --origin catchup|history_replay` — prints
  `memory_import` batches (≤ 64 items, ≤ 240 KiB) for catch-up; the agent passes
  each to `memory_import`.
- `--upload --all|--session <id>… [--exclude-session <id>] [--background]` —
  history import without the model: reads `SUBSTRATE_IMPORT_TICKET` and
  `SUBSTRATE_MCP_URL` (4.9), runs `initialize` (sends `Mcp-Session-Id` back if
  the server sets one; accepts JSON or SSE bodies), then `tools/call
  memory_import` with redacted `history_replay` items and one fresh `batch_id`.
  After a session's last turn it sends one `capture_session` seal (`boundary:
  "end"`, `session_complete: true`, offset and `message_high_water` = the
  session's final message index) so the server materializes and extracts it;
  a rerun is a duplicate. Catch-up output (`--session`) never seals: that
  session is still live.
  Turns over the limits are reduced (tool calls capped at 64 per message,
  shorter excerpts) or split into consecutive parts with exact offsets. Retries
  429/5xx/timeouts with backoff; a refused batch is bisected; `401`/`403` stops
  cleanly. Progress goes to `import-status.json`; `--status [--wait S]` reads it.
  Finished sessions are remembered locally and skipped on a rerun. The current
  session (named, or last seen by the offer hook, or the transcript written in
  the last 10 minutes) is never imported. Final line:
  `Imported N sessions (T turns): S stored, D duplicate, R rejected.`
- `--offer-check` — `UserPromptSubmit` command hook printing hook JSON
  (`hookSpecificOutput.additionalContext`). Until `--record-connected` it tells
  the agent to finish setup now (sign in, `memory_search`, **Connected to
  Substrate.**, then the offer); until a decision is recorded it tells it to
  offer the import; with no other local sessions it records `none`. Silent
  once connected and decided; always exit 0.
- `--record-connected`, `--record-decision yes|no|picked|none`, `--pause S` (≤ 60 s wait, used while
  the user approves sign-in).
- Claude subagent transcripts are skipped: `subagents/` folders, top-level
  `agent-*.jsonl` files, and `isSidechain: true` records (they repeat the
  parent's session id; the parent already holds their result).

Catch-up: when a turn-context text ends with the `[substrate] … not saved yet`
line, or on the sync command, the agent runs `memory_import_status` for the
session, then `--session … --after-index <message_high_water>`, then
`memory_import` per batch. History import: after **Connected to Substrate.**,
in the same reply, the agent runs `--preview` and offers **Import all** / **Let
me pick** / **Not now** once; on a yes it calls `memory_import_ticket`, starts
`--upload --background`, and polls `--status` until done.

## 9. Versioning

- Adding an optional input field or a result field is backward compatible and
  keeps `mcp_contract_version: 2`.
- Removing a tool, renaming a field, or changing a limit is a new contract version
  and a new `instructions` prefix.
- The server keeps the Hermes `/api/v1` wire until the `substrate-hermes` v0.5.0 pin
  is retired.
