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

The Cowork MCP endpoint and browser OAuth service are backend-owned and not
deployed by this repository yet. The Cowork package must not imply automatic
full-transcript capture.
