# Threat model

| Threat | Primary controls |
|---|---|
| Credential disclosure | Environment-only secrets; redaction before persistence/network; content-free errors; release scanning |
| Private-history disclosure | Explicit capture/replay; configured-server trust decision; no history in logs or receipts |
| Local spool exposure | Profile-rooted owner-private files; bounded bytes/age; explicit deletion |
| Server impersonation or redirect | Configured HTTPS origin; redirect rejection; capability/provider validation |
| Replay duplication or loss | Deterministic IDs; idempotency keys; durable acknowledgements; exact resume |
| Resource exhaustion | Event/response/spool bounds; backpressure; capped retries; importer memory tests |
| Build or release tampering | Release-clean allowlist; deterministic ZIP; source commit and per-file hashes; immutable checksums |
| Cross-profile leakage | State rooted in active Hermes home; no shared writable profile state |
| Prompt/content injection in recalled memory | Bounded cited memory cards treated as untrusted context; no tool authority granted by memory |

## Trust assumptions

- The operator chooses and authenticates a trusted Substrate server.
- Hermes 0.18.2 invokes the documented provider lifecycle correctly.
- The operating-system user and `$HERMES_HOME` permissions protect local state.
- Published checksums and GitHub release custody are verified before installation.

## Explicit limitations

- Pattern-based redaction cannot prove arbitrary sensitive prose is absent.
- The plugin cannot make a malicious configured server safe.
- Memory content does not grant send, action, or authorization authority by itself.
- Hermes versions outside the contract-tested matrix may change lifecycle behavior; full host-lifecycle certification is pending even for 0.18.2.
