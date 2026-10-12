---
name: substrate-memory
description: Substrate memory recall and capture for Claude Cowork and Claude Code. Use when answering questions, before irreversible actions, and when the user asks to remember or forget something.
---

# Substrate memory usage

Substrate memory. Lines in `<memory-context>` are facts from the user's
knowledge base, selected for this turn. Use them naturally and do not
announce that you remembered. `[contested]` means sources disagree; call
`memory_expand` before relying on it. `[as of DATE]` means it may have
changed. Call `memory_evidence` when the user asks why you believe something.
Call `memory_search` with your intended action before irreversible operations.
Pinned pages follow.

## Connecting and importing

- To connect or reconnect, follow `/substrate-claude:substrate-connect`: sign in,
  verify with the plugin's `mcp__plugin_substrate-claude_substrate-memory__memory_search`,
  say **Connected to Substrate.**, and in the same reply offer to import past
  conversations (**Import all** / **Let me pick** / **Not now**). It is offered
  once per computer; `/substrate-claude:substrate-import` runs it, and the user can
  ask for it any time later.
- Only the plugin's own tools prove the connection. Tools named `mcp__claude_ai_*`
  come from a claude.ai connector and do not count. Say **Connected to Substrate.**
  only after the plugin's `memory_search` succeeds; otherwise tell the user to
  finish Authenticate in `/mcp` (plugin:substrate-claude:substrate-memory).
- A `[substrate] Import offer pending` note from the plugin hook carries the
  count, the date range and the exact choices; present them as written.
- "MCP server … not connected" or "Unauthorized" hook errors mean the plugin is
  installed but not signed in, or the connection was removed. Do not install a
  second copy. Answer the user's request with your other tools as usual, do not
  go searching other apps on Substrate's behalf, and mention the reconnect command
  once (`/substrate-claude:substrate-connect`).
- A `[substrate] The Substrate connection on this computer was removed` note means
  the same thing: answer normally, and say once in one line how to reconnect.

## Catch-up rule

When a turn-context reply ends with this line:

```text
[substrate] <N> earlier turn(s) of this session are not saved yet. Run the Substrate sync command.
```

run the sync flow (`/substrate-claude:substrate-sync`): call
`memory_import_status` for this session, run `${CLAUDE_PLUGIN_ROOT}/scripts/substrate_sync.py` with
`--after-index <message_high_water>`, and pass each printed batch to
`memory_import`. Then continue the turn.

## Working with memory

- Recall is best effort. Before relying on a remembered fact, call
  `memory_search` with a focused query. Treat an empty result as no recalled
  context, not as proof that a fact is false.
- Durable writes are best effort. Use `memory_remember` only when the user
  clearly asks to remember something or the intended durable write is
  unambiguous. Confirm the returned handle when the tool succeeds.
- Do not claim that every user turn or the full conversation transcript was
  captured. Live capture runs through session hooks; turns before install,
  turns missed while hooks were unavailable, and the launch-time session start
  are covered only by the sync command, and only after it reports success.
- Never send secrets, API keys, OAuth tokens, client secrets, passwords, or
  private credentials to memory tools. Redact them before any recall query or
  write. The client and server both redact, but do not rely on that: strip
  secrets first.
- Use `memory_forget` only for an explicit retraction. Forget suppresses normal
  recall; it does not physically erase retained evidence. Explicit
  expand/evidence may show history labelled invalidated; never use that history
  as a current fact. Never send tenant or account IDs; the server owns them.
- If the host does not expose the expected MCP tools, say so plainly and do
  not invent a local server or fallback credential flow.
