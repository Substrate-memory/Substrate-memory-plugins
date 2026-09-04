# Compatibility

Compatibility has three independent axes: Hermes host, hosted Substrate capability contract, and plugin version.

## Certified matrix

| Plugin | Hermes | Required hosted capabilities | Status |
|---|---|---|---|
| 0.3.x | 0.21.x | contract v1 capabilities, device authorization, memory turn-context/search/expand/evidence, session completion | Released |

The `substrate` plugin installs with
`hermes plugins install Substrate-memory/Substrate-memory-plugins/plugins/substrate --ref v0.3.0 --no-enable`,
runs RFC 8628 device onboarding by itself, and registers `memory_search`, `memory_expand`,
and `memory_evidence`. It never requires a pre-existing API key.

## Failure behavior

The plugin fails closed when the configured origin, capabilities, credential custody, or response
contracts are invalid. It does not fall back to a local/self-hosted server or another memory
provider. Expired or revoked credentials restart repairable device onboarding while durable
capture events remain queued.

## Versioning

- Plugin behavior follows semantic versioning.
- Releases are immutable Git tags (`v0.3.0`) with attested `substrate.zip` and `SHA256SUMS` assets.
- Breaking server behavior requires a new protocol/schema identifier.
- Rollback preserves profile-local state and credential custody.

## Multi-host plugins (0.3.0)

Each host adapter below vendors the same stdlib-only core and exposes identical
`memory_search`, `memory_expand`, and `memory_evidence` tools, bounded pre-turn
`<memory-context>` recall, nonblocking completed-turn capture, session boundary
markers, and first-use RFC 8628 device onboarding. All are fail-closed with the
same bounded error classes, and all keep TLS verification enabled with only the
bundled public ISRG roots.

| Plugin directory | Host | Host version | Status |
|---|---|---|---|
| `plugins/substrate/` | Hermes | 0.21.x | Released |
| `plugins/claude-code/` | Claude Code | 2.x (verified on 2.1.259) | Supported |
| `plugins/claude-cowork/` | Claude Cowork | Shares the Claude Code plugin surface | Supported |
| `plugins/codex/` | Codex CLI | 0.144.x (verified on 0.144.5) | Supported |
| `plugins/grok-bot/` | Grok Build CLI / MCP-capable harness | Best-effort on grok.com web and X bots (no installable tool surface there) | Supported where MCP tools can be registered |
| `plugins/openclaw/` | OpenClaw | >= 2026.4.24, Node 22+, python3 3.11+ | Supported |
