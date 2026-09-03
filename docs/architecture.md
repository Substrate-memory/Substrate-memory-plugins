# Architecture

```text
Hermes 0.21.x lifecycle / tools
        |
        v
substrate plugin (plugins/substrate, stdlib only)
  - pre_llm_call: bounded turn-context recall injection
  - post_llm_call: nonblocking completed-turn capture
  - on_session_reset / on_session_finalize: session-completion markers
  - memory_search / memory_expand / memory_evidence tools
  - onboarding.py: RFC 8628 device flow, key stored in profile .env
  - verified HTTPS client with pinned public ISRG roots
        |
        v
versioned Substrate HTTP API (capabilities, ledger, memory, agents)
```

## Recall

Retrieval is ranked server-side over projected fact units. The plugin caches no memory;
every failure injects nothing and every tool result is bounded and redacted.

## Capture

Completed turns are posted as live ledger events. Session boundaries post
`capture_session` envelopes so the server can materialize ended sessions into the
extraction pipeline. Credential-shaped values are redacted before admission to the
network path.
