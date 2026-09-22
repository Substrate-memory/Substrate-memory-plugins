# Architecture

```text
Claude Cowork / Claude Code --> plugins/substrate-claude --\
ChatGPT Work / Codex app / Codex CLI --> plugins/substrate-codex --\
Hermes --> plugins/substrate-hermes ----------------------------------+--> one server-side
Other MCP-capable agent --> plugins/substrate-mcp (fallback only) ---/    MCP contract (v2)
                                                                              |
                                                                              v
                                              recall + capture + memory store (Substrate backend)
```

## One contract, thin clients

Every host speaks the same server-side MCP contract v2 (`docs/mcp-contract.md`).
Each plugin package is a thin client: hook wiring, a sync command (Claude/Codex),
or a bare remote manifest (fallback). The server owns storage, ranking, recall
assembly, redaction checks, session sealing, and deterministic replay.

The per-turn lifecycle:

- `memory_turn_context` (prompt submitted) opens the pending turn and returns the recall block (at most 8192 bytes, 40 lines) plus a missing-turn count.
- `memory_capture_tool` (tool used) appends bounded redacted tool calls (arguments at most 4096 bytes) and result excerpts (at most 8192 bytes plus a SHA-256 digest of the full redacted result) to the pending turn. Calls to Substrate's own `memory_*` tools are ignored.
- `memory_capture_turn` (agent finished) closes the pending turn and writes one canonical `capture_turn` ledger envelope.
- `memory_session_boundary` (lifecycle) writes a content-free `capture_session` envelope.
- `memory_import` replays missed or past turns; items without an `event_id` get a deterministic UUID so re-sending never stores twice. `memory_import_status` reports what the server holds.

Read and write tools (`memory_search`, `memory_expand`, `memory_evidence`, `memory_shares`, `memory_remember`, `memory_forget`) work the same on every host. All hooks fail open: a failed memory call never blocks the host.

## Durable delivery

- **Hermes** keeps its profile-local write-ahead spool. Nothing is fire-and-forget.
- **Claude and Codex** treat the host transcript as the spool. The server reports missing turns in the turn-context text; the plugin's sync command (`scripts/substrate_sync.py`, standard library only) re-imports them with deterministic ids, so nothing is stored twice. A session with no activity for 30 minutes is sealed by the server.
- **Fallback hosts** (`substrate-mcp`) have no hooks and no automatic capture guarantee; the agent drives the `memory_*` tools directly, including `memory_import` where the host can read its own transcripts.

## Client and server boundary

The backend owns the remote MCP endpoint (`https://app.trysubstrate.co/mcp`), browser connection service, OAuth consent, persistence, ranking, and deployment; this repository configures clients. See [the single installation guide](installation.md). No package may imply a working connection before an authenticated memory request succeeds. The server keeps the Hermes `/api/v1` wire during the rollout; its envelope schema is reused unchanged by contract v2.
