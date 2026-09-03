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
