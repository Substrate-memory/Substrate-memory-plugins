# Compatibility

## Current release

| Release | Hermes | Claude Code | Cowork | Codex / ChatGPT Work | Other MCP clients | Status |
|---|---|---|---|---|---|---|
| v0.8.0 | `plugins/substrate-hermes` (tested on Hermes 0.21.0-0.21.x; install by repository URL) | `plugins/substrate-claude` | `plugins/substrate-claude`; new session required in the current build (observed) for server registration | `plugins/substrate-codex` | `plugins/substrate-mcp` fallback | Released |

## Version policy

One rule for every package in this repository:

- **Install regardless of the host version.** No package pins or refuses a host version (no `requires_hermes`, no engine ranges, no "exact version only" instructions). Never upgrade or downgrade the host automatically.
- **Tested range, soft notice.** Each package has a tested range (table below). When the host is outside it, tell the user once, then continue:

  ```text
  Tested on <host> <tested range>; you are on <version>. It should work; if something does not, tell us at https://github.com/Substrate-memory/Substrate-memory-plugins/issues.
  ```

- **Who shows it.** The Hermes plugin shows it by itself on the first turn (it reads the running Hermes version). For Claude, Cowork, Codex, and other MCP hosts the installing agent compares the host version with the table and shows the same sentence (see `AGENTS.md`).
- Failures after install are reported with a plain message and a next step, never a bare error code.

| Package | Host | Tested range |
|---|---|---|
| `plugins/substrate-hermes` | Hermes | 0.21.0-0.21.x (0.21.4 verified end to end for v0.8.0) |
| `plugins/substrate-claude` | Claude Code, Cowork | Claude Code 2.1.x; current Cowork build |
| `plugins/substrate-codex` | Codex CLI, Codex app, ChatGPT Work | Codex CLI 0.144.x; current app builds |
| `plugins/substrate-mcp` | Other MCP clients | Any client with remote Streamable HTTP MCP and OAuth |

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
| v0.7.0 | `plugins/substrate-hermes` (Hermes 0.21.0) | `plugins/substrate-claude` | `plugins/substrate-claude` | `plugins/substrate-codex`, `plugins/substrate-mcp` fallback | Released (superseded: sign-in failed when the server was reached by another address) |
| v0.6.0 | Hermes 0.21.0 exactly; golden `substrate` runtime from v0.5.0 | Thin remote HTTP MCP plugin (`substrate-mcp`); use the host's confirmed MCP interface | Thin remote HTTP MCP plugin (`substrate-mcp`); new session required in the current build (observed) for server registration | Thin remote HTTP MCP plugin (`substrate-mcp`); use the host's confirmed MCP interface | Released |
| v0.5.0 | Durable v5 Hermes `substrate` plugin (write-ahead spool, session boundaries, subagent capture, device login) | Five host adapters at manifest 0.4.0 lineage (deprecated Aug 2026 API-key set) | Same as Claude Code | Same as Claude Code | Released |

The immutable v0.3.0, v0.4.0, v0.5.0, and v0.6.0 releases remain available for rollback. Their historical host adapters are not part of v0.7.0 or v0.8.0 and are not rebuilt.
