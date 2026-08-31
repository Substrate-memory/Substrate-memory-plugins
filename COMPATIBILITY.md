# Compatibility

Compatibility has three independent axes: Hermes host, hosted Substrate capability contract, and plugin version.

## Certified matrix

| Plugin | Hermes | Required hosted capabilities | Status |
|---|---|---|---|
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
