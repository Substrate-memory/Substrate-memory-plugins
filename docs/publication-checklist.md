# Publication checklist

Run against the exact candidate and release assets.

## Source boundary

- [ ] Only plugin-owned source, tooling, tests, docs, and immutable imported assets are present.
- [ ] No Substrate server implementation, infrastructure, production configuration, evidence, or customer records.
- [ ] Runtime imports only the Python standard library and Hermes host interfaces.
- [ ] `Substrate-v2` does not remain an editable plugin source.

## Secrets and privacy

- [ ] Publication scanner passes with content-free output.
- [ ] GitHub secret scanning is enabled.
- [ ] No real credentials, private endpoints, history, spool contents, or production evidence.
- [ ] Synthetic credential fixtures are clearly non-routable.
- [ ] README and security policy explain capture and retention boundaries.

## Compatibility and behavior

- [ ] Provider, entity-memory, replay, hardening, redaction, packaging, and importer tests pass.
- [ ] Hermes compatibility claim names an exact version.
- [ ] Server capability requirements and fail-closed behavior are documented.
- [ ] Rollback preserves configuration, spool, and checkpoints.

## Artifact custody

- [ ] Version is not reused.
- [ ] Source is committed before building.
- [ ] Archive provenance names the exact source commit and every file digest.
- [ ] Archive and installer checksums are verified independently.
- [ ] Adversarial review covers source and exact artifacts.
- [ ] GitHub release assets are read back and match local bytes.

Version 1.4.1 is an imported immutable exception: its source provenance correctly points to the historical Substrate-v2 commit. It must never be rebuilt or replaced.
