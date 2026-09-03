# Compatibility

Compatibility has three independent axes: Hermes host, hosted Substrate capability contract, and plugin version.

## Current install target

The current Hermes plugin is [`plugins/substrate`](plugins/substrate) (`substrate`, version
0.2.x). It is the only path an installing agent should select from this repository, and it is
certified for Hermes **0.21.x**. It installs with
`hermes plugins install Substrate-memory/Substrate-memory-plugins/plugins/substrate --no-enable`,
runs RFC 8628 device onboarding by itself, and registers `memory_search`, `memory_expand`,
and `memory_evidence`. It never requires a pre-existing API key.

The table below is the **legacy** `substrate_wiki` provider record. It does not apply to the
current `substrate` plugin, and it must not be used to reject a Hermes 0.21.x install: the
legacy rows exist only for existing 0.20.x installations and migration testing.

## Legacy certified matrix (substrate_wiki)

| Plugin | Hermes | Required hosted capabilities | Status |
|---|---|---|---|
| 2.0.5 | 0.20.x | Same as 2.0.4; explicit post-approval history consent and v5 handshake | Contract and lifecycle integration tested |
| 2.0.4 | 0.20.x | Same as 2.0.3; compact user/assistant capture and batched history upload | Contract and lifecycle integration tested |
| 2.0.3 | 0.20.x | Same as 2.0.2; resilient approved-device polling across hosted stalls | Contract and lifecycle integration tested |
| 2.0.2 | 0.20.x | Same as 2.0.1; bounded retry for hosted tenant cold starts | Contract and lifecycle integration tested |
| 2.0.1 | 0.20.x | Same as 2.0.0; complete email authorization URL in agent/headless prompts | Contract and lifecycle integration tested |
| 2.0.0 | 0.20.x | `stream-v2`, `entity-wiki-v1`, `entity-quality-v2`, hosted device authorization | Contract and lifecycle integration tested |
| 1.4.1 | 0.18.2 | Imported immutable historical release | Historical compatibility record only |

Plugin 2.0.x uses the current 0.20 user-plugin discovery path and native `post_setup` hook.
Older Hermes lifecycle conventions are not supported by this release.

## Failure behavior

The plugin fails closed when the hosted origin, capabilities, credential custody, or response
contracts are invalid. It does not fall back to a local/self-hosted server, legacy history
transmission, or another memory provider. Expired or revoked credentials start repairable
hosted onboarding while durable events remain queued.

## Versioning

- Plugin behavior follows semantic versioning.
- The hosted origin is fixed at `https://app.trysubstrate.co`.
- Breaking server behavior requires a new protocol/schema identifier.
- Installation activates `memory.provider: substrate_wiki`; there is no silent auto-update.
- Rollback preserves profile-local spool, checkpoints, consent state, and credential custody.
