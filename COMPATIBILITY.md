# Compatibility

Compatibility has three independent axes: Hermes host, hosted Substrate capability contract, and plugin version.

## Certified matrix

| Release | Plugin content | Host | Required hosted capabilities | Status |
|---|---|---|---|---|
| Release | Plugin content | Host | Required hosted capabilities | Status |
|---|---|---|---|---|
| v0.3.0 | Hermes `substrate` 0.3.0 | Hermes 0.21.x | contract v1 capabilities, device authorization, memory turn-context/search/expand/evidence, session completion | Released |
| v0.4.0 | Hermes `substrate` 0.3.0 (bytes unchanged) + five host adapters at 0.4.0 | Hermes 0.21.x plus the multi-host table below | contract v1 capabilities, device authorization, memory turn-context/search/expand/evidence, session completion | Released |
| v0.5.0 | Hermes `substrate` 0.4.0 (durable v5: spool, write tools, login CLI) + five host adapters at 0.4.0 (bytes unchanged) | Hermes 0.21.x plus the multi-host table below | contract v1 capabilities, device authorization, memory turn-context/search/expand/evidence, memory write/forget ledger, session completion | Pending tag (branch `release/hermes-durability-v5`) |

The `substrate` plugin installs with
`hermes plugins install Substrate-memory/Substrate-memory-plugins/plugins/substrate --ref v0.5.0 --no-enable`.
Older installs pinned to `v0.3.0` or `v0.4.0` keep working and are preserved
for rollback.
It runs RFC 8628 device onboarding by itself (bundled `onboard.py`, or
automatically on first use), and registers `memory_search`, `memory_expand`,
`memory_evidence`, `memory_remember`, and `memory_forget`. It never requires
a pre-existing API key.

## Failure behavior

The plugin fails closed when the configured origin, capabilities, credential custody, or response
contracts are invalid. It does not fall back to a local/self-hosted server or another memory
provider. Expired or revoked credentials restart repairable device onboarding while durable
capture events remain queued.

## Versioning

- Plugin behavior follows semantic versioning.
- The repo-level release version lives in the root `VERSION` file (currently `0.5.0`)
  and covers the whole plugin set; the Hermes plugin content is at `0.4.0`,
  the host adapters at `0.4.0` (byte-identical to `v0.4.0`).
- Releases are immutable Git tags (`v0.3.0` with attested `substrate.zip` and
  `SHA256SUMS`; `v0.4.0` and `v0.5.0` with all six attested archives plus
  `SHA256SUMS`).
- Breaking server behavior requires a new protocol/schema identifier.
- Rollback preserves profile-local state and credential custody.

## Multi-host plugins (0.4.0)

Each host adapter below vendors the same stdlib-only core and exposes identical
`memory_search`, `memory_expand`, and `memory_evidence` tools, bounded pre-turn
`<memory-context>` recall, nonblocking completed-turn capture, session boundary
markers, and first-use RFC 8628 device onboarding. All are fail-closed with the
same bounded error classes, and all keep TLS verification enabled with only the
bundled public ISRG roots.

| Plugin directory | Host | Host version | Plugin version | Status |
|---|---|---|---|---|
| `plugins/substrate/` | Hermes | 0.21.x | 0.4.0 (durable v5) | Supported |
| `plugins/claude-code/` | Claude Code | 2.x (verified on 2.1.259) | 0.4.0 | Supported |
| `plugins/claude-cowork/` | Claude Cowork | Shares the Claude Code plugin surface | 0.4.0 | Supported |
| `plugins/codex/` | Codex CLI | 0.144.x (verified on 0.144.5) | 0.4.0 | Supported |
| `plugins/grok-bot/` | Grok Build CLI / MCP-capable harness | Best-effort on grok.com web and X bots (no installable tool surface there) | 0.4.0 | Supported where MCP tools can be registered |
| `plugins/openclaw/` | OpenClaw | >= 2026.4.24, Node 22+, python3 3.11+ | 0.4.0 | Supported |
