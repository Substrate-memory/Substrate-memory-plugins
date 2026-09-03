# Source-of-truth boundary

## Decision

`Substrate-memory/Substrate-memory-plugins` is the sole editable source for the Hermes
`substrate` retrieval plugin. The protected default branch and immutable `v0.3.0` release
(tag plus attested `substrate.zip` and `SHA256SUMS`) are the only published artifacts.

The server repository owns only the Substrate API, persistence, and deployment; it must
not vendor or modify plugin source.

## Ownership

### This repository

- Hermes hook and tool registration.
- Client transport and response validation.
- Device onboarding and credential custody.
- Session-completion capture.
- Build, tests, documentation, and releases.
