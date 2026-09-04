# Substrate memory — agent instructions for Grok Bot

Paste this file (or append it) to your Grok agent instructions / system
prompt. It carries the frozen static memory prompt plus the per-turn
wiring that MCP alone cannot express.

## Static memory prompt (frozen per session)

Substrate memory. Lines in `<memory-context>` are facts from the user's
knowledge base, selected for this turn. Use them naturally and do not
announce that you remembered. `[contested]` means sources disagree; call
`memory_expand` before relying on it. `[as of DATE]` means it may have
changed. Call `memory_evidence` when the user asks why you believe something.
Call `memory_search` with your intended action before irreversible operations.
Pinned pages follow.

## Per-turn wiring (harness behavior)

1. Before each model request, call `pre_turn_context(session_id,
   user_message, conversation_history, ...)` from `bridge.py` and prepend
   the returned block to the request. An empty result means "no memory for
   this turn" — proceed normally.
2. After each completed turn, call `capture_turn(user_message,
   assistant_response, session_id=..., conversation_history=...)`. It queues
   in the background and never blocks.
3. On conversation reset call `session_reset(...)`; on shutdown or session
   expiry call `session_end(...)`.
4. If any tool result has `status: authorization_required`, show the exact
   `verification_uri_complete` URL to the user and ask them to open it in a
   browser, sign in by email, and approve. Never ask for a pasted key.
   After approval, retry the tool — the key is stored privately and memory
   works automatically from then on.
5. Never print an access token. Never disable TLS verification.
