# Threat model

| Threat | Primary controls |
|---|---|
| Credential disclosure | Profile-private `.env` custody; no config/log/argument/chat secrets; pre-network redaction |
| Device-code disclosure | Owner-private profile file; deleted on terminal states; never in onboarding state |
| Origin substitution | Configured HTTPS origin allowlist; unsafe override rejection; redirect rejection |
| TLS interception | System trust plus pinned public ISRG roots; verification never disabled |
| Tenant crossover | Tenant-scoped revocable server keys; per-request agent resolution |
| Replay duplication or loss | Stable event IDs; idempotency keys; durable capture queue |
