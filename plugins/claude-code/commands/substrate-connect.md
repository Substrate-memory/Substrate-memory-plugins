---
description: Connect this Claude Code profile to Substrate memory (one-time browser approval)
---

# Connect to Substrate memory

Substrate memory connects itself with RFC 8628 device authorization. No key is
ever pasted into chat.

1. Run the pre-turn memory path once (ask anything, or call `memory_search`
   with `{"query": "hello"}`).
2. If the turn injects a `<substrate-connect>` block, show that exact
   `verification_uri_complete` URL to the user as a clickable link and ask
   them to open it, sign in by email, and approve the connection. Never
   approve it for them. Never ask for a pasted key.
3. After approval the tenant-scoped key is stored privately under
   `~/.claude/substrate/` (and `SUBSTRATE_API_KEY` in the environment for the
   session) and memory works automatically. Call `memory_search` again to
   verify; it should return results or `{"contract_version":1,"results":[]}`,
   never an `authorization_required` status.
4. TLS verification stays enabled at all times. Never print an access token.
