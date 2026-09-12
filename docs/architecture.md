# Architecture

```text
Hermes 0.21.0 lifecycle / tools
        |
        v
plugins/substrate (golden Hermes runtime)
  - bounded recall and durable capture hooks
  - memory search/expand/evidence/write tools
  - profile-local browser device onboarding
        |
        v
versioned Substrate HTTP API

Cowork host
        |
        v
plugins/substrate-mcp (thin package)
  - remote HTTP MCP manifest at app.trysubstrate.co/mcp
  - generic best-effort usage skill
  - no local server, hooks, or transcript bridge
```

Both integrations use the same Substrate browser connection screen and consent
model. Account, entitlement, credential registration and revocation are backend
services shared by both. Device polling and MCP authorization-code/PKCE remain
protocol adapters underneath that common user-facing journey.

The backend owns the remote MCP endpoint and browser connection service; this
repository configures clients. See [the single installation guide](installation.md).
The Cowork package must not imply automatic full-transcript capture or claim a
working connection before an authenticated memory request succeeds.
