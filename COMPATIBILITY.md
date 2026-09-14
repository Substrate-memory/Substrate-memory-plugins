# Compatibility

## Current review candidate

| Release | Hermes | Claude Code | Cowork | Codex / other MCP clients | Status |
|---|---|---|---|---|---|
| v0.6.0 | Hermes 0.21.0 exactly; golden `substrate` runtime from v0.5.0 | Thin remote HTTP MCP plugin (`substrate-mcp`); use the host's confirmed MCP interface | Thin remote HTTP MCP plugin (`substrate-mcp`); new session required in the current build (observed) for server registration | Thin remote HTTP MCP plugin (`substrate-mcp`); use the host's confirmed MCP interface | Review-stage only |

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
API keys, tokens, or client secrets are accepted. If the package is configured but
tools are absent, the host is not wired into the current session; start a new
session or restart as documented before retrying. In the current Cowork build, start a
new session after installation (observed behavior).

The August 2026 API-key plugins (`substrate_capture`, `~/.substrate/*/spool`,
`SUBSTRATE_API_URL`/`SUBSTRATE_API_KEY`) are deprecated. Their local spool is not
migrated. Do not set `SUBSTRATE_API_KEY` by hand; the product does not issue API keys.

## Historical releases

The immutable v0.3.0, v0.4.0, and v0.5.0 releases remain available for rollback.
Their historical host adapters are not part of v0.6.0 and are not rebuilt.
