# Compatibility

Compatibility has three independent axes: Hermes host, Substrate server capability contract, and plugin version.

## Certified matrix

| Plugin | Hermes | Required server capabilities | Status |
|---|---|---|---|
| 1.5.0 | 0.18.2 | `stream-v2`, `entity-wiki-v1`, `entity-quality-v2` | Standalone contract-tested; host-lifecycle certification pending |
| 1.5.0 | 0.19.0 | Not evaluated completely | Unsupported |
| 1.4.1 | 0.18.2 | Imported immutable historical release | Historical compatibility record |

Hermes 0.19.0 requires provider discovery, plugin lifecycle, tool-schema, profile-isolation, import-service, configuration, replay, and rollback verification before support can be claimed.

## Failure behavior

The plugin fails closed when required capabilities or response contracts are absent. It does not silently fall back to legacy history transmission or an alternate memory provider.

## Versioning

- Plugin behavior follows semantic versioning.
- Breaking server behavior requires a new protocol/schema identifier and concurrent old-version support during migration.
- No silent plugin auto-update.
- No automatic configuration or state rewrite.
- Rollback preserves profile-local configuration, spool, and checkpoints.
