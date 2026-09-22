# Compatibility

## Current release

| Release | Hermes | Claude Code | Cowork | Codex / ChatGPT Work | Other MCP clients | Status |
|---|---|---|---|---|---|---|
| v0.7.0 | `plugins/substrate-hermes` (Hermes 0.21.0) | `plugins/substrate-claude` | `plugins/substrate-claude`; new session required in the current build (observed) for server registration | `plugins/substrate-codex` | `plugins/substrate-mcp` fallback | Candidate |

All four packages speak one server-side MCP contract (v2): recall before every turn, automatic capture of every completed turn including bounded redacted tool calls and results, session boundaries, subagent capture, explicit remember/forget, and evidence. Hermes delivers through its profile-local write-ahead spool; Claude and Codex treat the host transcript as the spool and re-import missing turns with deterministic ids. The fallback package has no hooks and offers best-effort tools only.

Known limits: Codex has no session-end hook (the server seals idle sessions after 30 minutes); Claude's SessionStart hook at launch runs before MCP connects; if a Cowork cloud session ends while Substrate is unreachable and is never reopened, its last turn is not recovered; plugin hooks in Codex must be trusted once via `/hooks`; Cowork needs a new session after install.

## Installation contract

The exact request is:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

The agent identifies its host (Cowork/Claude Code → `substrate-claude`; ChatGPT Work/Codex → `substrate-codex`; Hermes → `substrate-hermes`; else `substrate-mcp` fallback) and never picks a host merely because its binary exists. Supported setup must use browser OAuth consent and an authenticated memory smoke test before success. No manual API keys, tokens, or client secrets are accepted. If the package is configured but tools are absent, the host is not wired into the current session; start a new session or restart as documented before retrying. In the current Cowork build, start a new session after installation (observed behavior).

The August 2026 API-key plugins (`substrate_capture`, `~/.substrate/*/spool`, `SUBSTRATE_API_URL`/`SUBSTRATE_API_KEY`) are deprecated. Their local spool is not migrated. Do not set `SUBSTRATE_API_KEY` by hand; the product does not issue API keys.

## Historical releases

| Release | Hermes | Claude Code | Cowork | Codex / other MCP clients | Status |
|---|---|---|---|---|---|
| v0.6.0 | Hermes 0.21.0 exactly; golden `substrate` runtime from v0.5.0 | Thin remote HTTP MCP plugin (`substrate-mcp`); use the host's confirmed MCP interface | Thin remote HTTP MCP plugin (`substrate-mcp`); new session required in the current build (observed) for server registration | Thin remote HTTP MCP plugin (`substrate-mcp`); use the host's confirmed MCP interface | Released |
| v0.5.0 | Durable v5 Hermes `substrate` plugin (write-ahead spool, session boundaries, subagent capture, device login) | Five host adapters at manifest 0.4.0 lineage (deprecated Aug 2026 API-key set) | Same as Claude Code | Same as Claude Code | Released |

The immutable v0.3.0, v0.4.0, v0.5.0, and v0.6.0 releases remain available for rollback. Their historical host adapters are not part of v0.7.0 and are not rebuilt.
