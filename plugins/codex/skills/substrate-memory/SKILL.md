---
name: substrate-memory
description: Substrate memory for this session. Recall project facts before acting and capture what you learn.
---

# Substrate memory

Substrate is the source of truth for cross-session memory. It is available
through the `substrate` MCP server (`memory_search`, `memory_expand`,
`memory_evidence`) and through the per-turn `<memory-context>` block the
`UserPromptSubmit` hook injects.

## Static memory prompt (frozen for this session)

Substrate memory. Lines in `<memory-context>` are facts from the user's knowledge base, selected for this turn. Use them naturally and do not announce that you remembered. `[contested]` means sources disagree; call `memory_expand` before relying on it. `[as of DATE]` means it may have changed. Call `memory_evidence` when the user asks why you believe something. Call `memory_search` with your intended action before irreversible operations. Pinned pages follow.

## Rules

- Use `<memory-context>` lines naturally; never announce that you remembered.
- `[contested]` means sources disagree: call `memory_expand` before relying
  on it. `[as of DATE]` means it may have changed.
- Call `memory_search` with your intended action before irreversible
  operations (writes, deletes, deploys, network calls).
- Call `memory_evidence` when the user asks why you believe something.
- If a tool result reports `authorization_required`, show the user the exact
  `verification_uri_complete` link and ask them to approve it in a browser.
  Never approve it for them and never ask for a pasted key.
- Completed turns are captured automatically by the `PostToolUse` hook.
  Capture failures never block a turn; retrieval failures inject nothing.
- TLS verification stays enabled. Never print an access token.
