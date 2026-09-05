# Architecture

```text
Hermes 0.21.0 lifecycle / tools
        |
        v
substrate plugin (plugins/substrate 0.4.0, stdlib only)
  - pre_llm_call: bounded turn-context recall injection
  - post_llm_call: durable completed-turn capture into a write-ahead spool
  - on_session_finalize: true session-end boundary; on_session_reset: rotation
  - subagent_start / subagent_stop: parent-routed subagent capture
  - memory_search / memory_expand / memory_evidence tools
  - memory_remember / memory_forget explicit write tools
  - onboard.py: RFC 8628 device flow, key stored in profile .env
  - verified HTTPS client with the host system trust store (no bundled roots)
        |
        v
versioned Substrate HTTP API (capabilities, ledger, memory, agents)
```

## Recall

Retrieval is ranked server-side over projected fact units. The plugin caches no
retrieved memory; every failure injects nothing and every tool result is bounded
and redacted.

## Capture

Completed turns (user/assistant text plus bounded redacted tool traffic: tool
call arguments up to 4096 bytes, tool result excerpts up to 8192 bytes plus
digest; system messages never captured) enqueue as live ledger events into a
profile-local write-ahead spool and are delivered with strict ACK retirement.
Session boundaries post `capture_session` envelopes so the server can materialize
ended sessions into the extraction pipeline. Credential-shaped values are redacted
before admission to the network path.
