# Threat model

| Threat | Primary controls |
|---|---|
| Credential disclosure | Native/profile-private custody; no config/log/argument secrets; pre-persistence redaction |
| Origin substitution | Fixed hosted HTTPS origin; unsafe environment override rejection; redirect rejection |
| Private-history disclosure | Explicit historical consent; source eligibility filters; redaction before spool/network |
| Local spool exposure | Profile-rooted owner-private files; bounded bytes; explicit deletion |
| Tenant crossover | Tenant-scoped revocable server keys; live account/tenant checks; tenant-bound API resolution |
| Replay duplication or loss | Stable batch/event IDs; idempotency keys; durable acknowledgements and exact resume |
| Resource exhaustion | Bounded bodies/events/responses/spool; poll throttling; capped retry backoff |
| Build or release tampering | Allowlisted deterministic ZIP; commit and file hashes; immutable checksums |
| Cross-profile leakage | Profile-derived credential slots and state; no shared writable profile state |
| PID reuse during import | Content-free runtime nonce and process-identity verification |
| Prompt injection in recall | Bounded cited memory cards treated as untrusted context; no authority from memory |

## Trust assumptions

- Hosted account authentication and tenant isolation are operated at `app.trysubstrate.co`.
- Hermes 0.20.x invokes current user-plugin discovery and provider lifecycle hooks.
- The operating-system user and `$HERMES_HOME` permissions protect local state.
- Published checksums and release custody are verified before installation.

## Explicit limitations

- Pattern redaction cannot prove arbitrary sensitive prose is absent.
- Compromise of the hosted service is outside the plugin's local security boundary.
- Memory content does not grant send, action, or authorization authority.
- Portable workers are restarted when Hermes next initializes the provider; systemd units can restart independently.
