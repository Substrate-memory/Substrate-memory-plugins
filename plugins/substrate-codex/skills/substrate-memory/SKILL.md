---
name: substrate-memory
description: Use Substrate long-term memory in Codex and ChatGPT Work — recall per turn, explicit writes, catch-up sync.
---

# Substrate memory usage

Substrate memory. Lines in `<memory-context>` are facts from the user's knowledge base, selected for this turn. Use them naturally and do not announce that you remembered. `[contested]` means sources disagree; call `memory_expand` before relying on it. `[as of DATE]` means it may have changed. Call `memory_evidence` when the user asks why you believe something. Call `memory_search` with your intended action before irreversible operations. Pinned pages follow.

## Self-check before installing or reconnecting

1. If `memory_search` is available, call it with a focused, non-secret query.
   An empty result is valid. On success report **Connected to Substrate.**
2. If the plugin is configured but tools are absent, say it is installed but
   not wired into this session. Start a new session, then ask the user to paste:
   `Verify my Substrate connection with memory_search and report Connected to Substrate.`
3. If nothing is configured, install through the host's documented interface,
   then follow Install → Sign in → Review → Approve connection → Connected to Substrate.
4. If tools are present but sign-in is required, show the exact browser URL.
   The user signs in, reviews the request, and chooses **Approve connection**.
   Never ask for or accept a pasted token or secret.

## Recall

- Recall is best effort and per turn. Facts arrive as extra context; use them
  naturally without announcing that you remembered.
- `[contested]` means sources disagree: call `memory_expand` before relying on it.
- `[as of DATE]` means it may have changed: re-check when it matters.
- Call `memory_evidence` when the user asks why you believe something.
- Call `memory_search` with your intended action before irreversible operations.
- Treat an empty result as no recalled context, not as proof a fact is false.

## Catch-up rule

- If turn context ends with `[substrate] <N> earlier turn(s) of this session
  are not saved yet. Run the Substrate sync command.`, run the sync runbook
  (`$substrate-sync`): call `memory_import_status` for the session, run the
  bundled `scripts/substrate_sync.py` with `--after-index <message_high_water>`,
  and pass each batch to `memory_import`.
- After the first successful smoke test, ask exactly once whether the user
  wants to import past conversations (`$substrate-import`).

## Writes

- Use `memory_remember` only when the user clearly asks to remember something
  or the durable write is unambiguous. Confirm the returned handle on success.
- Never send secrets, API keys, OAuth tokens, passwords, or private
  credentials to memory tools. Redact them before any recall query or write.

## Forget semantics

- Use `memory_forget` only for an explicit retraction. It takes an
  `operation_id`, a lowercase-hex `m:` or `p:` handle, and an optional reason
  up to 1024 bytes.
- Forget suppresses normal recall; it does not physically erase retained
  evidence. Explicit expand/evidence may show history labelled invalidated:
  never use that history as a current fact.
- Never send tenant or account IDs; the server owns them.
