---
name: substrate-memory
description: Search, expand, and cite the user's Substrate knowledge base; memory capture runs automatically.
---

# Substrate memory

Substrate is the user's long-term knowledge base. The server owns storage,
ranking, and evidence; this plugin only retrieves and captures.

## Static rules (always apply)

Substrate memory. Lines in `<memory-context>` are facts from the user's
knowledge base, selected for this turn. Use them naturally and do not announce
that you remembered. `[contested]` means sources disagree; call `memory_expand`
before relying on it. `[as of DATE]` means it may have changed. Call
`memory_evidence` when the user asks why you believe something. Call
`memory_search` with your intended action before irreversible operations.
Pinned pages follow.

## Tools (provided by the `substrate-memory` MCP server)

- `memory_search {"query": string, "kinds"?: string[<=16], "limit"?: 1..20}`:
  recall or pre-action check. Returns `{contract_version, results:
  [{handle, text?, score?, kind?, markers?}]}`.
- `memory_expand {"handle": "m:...|p:..."}`: bounded detail for one handle.
- `memory_evidence {"handle": ..., "raw"?: bool, "limit"?: 1..20}`: excerpts
  when the user asks why you believe something.

If a tool returns `{"status":"authorization_required", ...}`, show the exact
`verification_uri_complete` URL to the user and ask them to approve it in a
browser, then retry. Never ask for a pasted key. Never print an access token.

## Capture

Completed turns and session boundaries are captured automatically in the
background. Nothing to do here.
