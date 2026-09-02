# Source-of-truth boundary

## Decision

`Substrate-memory/Substrate-memory-plugins` is the sole editable source for the Hermes `substrate_wiki` plugin. The protected default branch and immutable releases `v1.5.0`, `v2.0.0`, `v2.0.1`, `v2.0.3`, and `v2.0.4` have been read back successfully.

Substrate-v2 owns only the server and pinned public release references; it must not vendor or modify plugin source.

## Ownership

### This repository

- Hermes provider lifecycle and tool registration.
- Client transport and response validation.
- Client-side redaction.
- Profile-local spool and checkpoints.
- Durable history replay and import service.
- Build, installer, tests, documentation, and releases.

### Substrate-v2

- HTTP endpoints and authentication.
- Capability negotiation and versioned request/response behavior.
- Idempotency, request limits, persistence, queues, deletion, triage, projection, indexing, and search.
- Infrastructure, deployment, and server-side producer tests.

The repositories meet at immutable plugin release assets and the versioned HTTP capability contract. Neither repository imports the other's runtime source.

## Extraction provenance

- Source repository: `Substrate-memory/Substrate-v2`.
- Extraction base: `39e7bd8f650401c4a53004dc34e06c4b47e77c28`.
- Current imported plugin release: 1.4.1.
- Original 1.4.1 source commit: `a3953b0512bbb84fb62b48a75bab04cbcb845c78`.
- Archive SHA-256: `877ccf9b0212792b699d9c98912a26980675a6050df3bd319e927639e3d901f1`.
- Installer SHA-256: `7600b2681c3aebcb1b1492b0a04be38bbbec637089cbbcfb1cc26e8c10865b8d`.

The imported 1.4.1 artifacts remain immutable. Repository-native releases use new versions and provenance commits from this repository.
