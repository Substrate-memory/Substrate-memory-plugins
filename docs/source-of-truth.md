# Source-of-truth boundary

## Decision

`Substrate-memory/Substrate-memory-plugins` is the sole editable source for the Hermes
`substrate` retrieval plugin. The protected default branch and the immutable
releases (currently `v0.3.0`, `v0.4.0`, pending `v0.5.0`: tag plus attested
archives and `SHA256SUMS`) are the only published artifacts.

The server repository owns only the Substrate API, persistence, and deployment; it must
not vendor or modify plugin source.

## Ownership

### This repository

- Hermes hook and tool registration.
- Client transport and response validation.
- Device onboarding and credential custody.
- Session-completion capture.
- Build, tests, documentation, and releases.
