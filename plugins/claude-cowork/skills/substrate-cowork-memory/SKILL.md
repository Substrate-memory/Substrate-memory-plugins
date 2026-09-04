---
name: substrate-cowork-memory
description: This skill should be used when the user asks to recall saved knowledge, search memory, expand a memory handle, show evidence for a belief, connect Substrate memory, or before any irreversible operation (delete, deploy, send, overwrite). Provides the Substrate memory prompt and the memory_search, memory_expand, memory_evidence tools.
version: 0.3.0
---

# Substrate Memory

Substrate memory. Lines in `<memory-context>` are facts from the user's knowledge base, selected for this turn. Use them naturally and do not announce that you remembered. `[contested]` means sources disagree; call `memory_expand` before relying on it. `[as of DATE]` means it may have changed. Call `memory_evidence` when the user asks why you believe something. Call `memory_search` with your intended action before irreversible operations. Pinned pages follow.

(The paragraph above is the frozen static memory prompt. It is also delivered
by the plugin's SessionStart hook, so it holds even when this skill is idle.)

## Tools (MCP server `substrate-memory`, stdlib-only stdio)

- `memory_search` — Search Substrate memory. Use the intended action in the
  query before irreversible operations. Input: `{"query": string,
  "kinds"?: string[≤16], "limit"?: 1..20 (default 8)}`.
- `memory_expand` — Expand a Substrate memory or page handle into its bounded
  detail. Input: `{"handle": "m:<hex>" | "p:<hex>"}`.
- `memory_evidence` — Get evidence excerpts for a Substrate memory handle.
  Input: `{"handle": ..., "raw"?: boolean (default false), "limit"?: 1..20}`.

Results are bounded JSON strings. `{"error": "<class>"}` is fail-closed:
`invalid_request` (fix the input), `invalid_response`, `timeout`,
`transport_error`, `invalid_config` (not connected yet).

## First-use connection

When a memory call returns `authorization_required`, Substrate memory is not
connected yet. Show the exact `verification_uri_complete` link to the user and
ask them to open it, sign in by email, and approve the connection. Never ask
the user to paste a key into chat, and never approve it for them. After
approval, call `memory_search` again; the stored key is reused automatically.

## Capture

Completed turns are captured in the background by the Stop hook and session
markers by the SessionEnd hook. Capture never blocks a turn and never uploads
tool results or system messages beyond the redacted, bounded envelope.
