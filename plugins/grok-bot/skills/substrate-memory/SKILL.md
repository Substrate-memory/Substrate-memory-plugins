---
name: substrate-memory
description: Search and capture Substrate long-term memory around Grok turns
when-to-use: every turn that should recall stored facts, before irreversible operations, when the user asks why you believe something
allowed-tools: substrate-memory__memory_search, substrate-memory__memory_expand, substrate-memory__memory_evidence
---

# Substrate memory

Lines in `<memory-context>` are facts from the user's knowledge base,
selected for this turn. Use them naturally and do not announce that you
remembered. `[contested]` means sources disagree; call `memory_expand`
before relying on it. `[as of DATE]` means it may have changed. Call
`memory_evidence` when the user asks why you believe something. Call
`memory_search` with your intended action in the query before irreversible
operations. Pinned pages follow.

Turn wiring: fetch pre-turn context with `bridge.py pre-turn`, capture
completed turns with `bridge.py capture-turn`, and close sessions with
`bridge.py session-end`. If a tool result reports `authorization_required`,
show the exact `verification_uri_complete` URL to the user and ask them to
approve it in a browser; never ask for a pasted key. Never print an access
token. Never disable TLS verification.
