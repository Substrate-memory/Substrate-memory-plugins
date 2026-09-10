# Compatibility

## Current review candidate

| Release | Hermes | Cowork | Status |
|---|---|---|---|
| v0.6.0 | Hermes 0.21.0 exactly; golden `substrate` runtime from v0.5.0 | Thin remote HTTP MCP plugin (`substrate-mcp`); requires backend MCP/browser OAuth and authenticated client verification | Review-stage only |

Hermes uses `plugins/substrate` and retains its in-process hooks, durable spool,
and memory tools. Cowork uses `plugins/substrate-mcp`, which contains only a
remote HTTP MCP manifest, generic usage skill, and docs. It has no local server,
hooks, or automatic full-transcript guarantee.

## Installation contract

The exact request is:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

Unknown hosts require a clarifying question. Supported setup must use browser
OAuth consent and an authenticated memory smoke test before success. No manual
API keys, tokens, or client secrets are accepted.

## Historical releases

The immutable v0.3.0, v0.4.0, and v0.5.0 releases remain available for rollback.
Their historical host adapters are not part of v0.6.0 and are not rebuilt.
