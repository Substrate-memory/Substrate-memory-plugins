# Threat model

| Threat | Primary controls |
|---|---|
| Credential disclosure | Browser OAuth; no manual secrets; Hermes profile-private custody; no Cowork secrets in config |
| Origin substitution | Fixed HTTPS remote MCP URL; no local server or user-provided secret endpoint in the thin plugin |
| TLS interception | HTTPS verification remains enabled; no certificate bypass |
| Tenant crossover | Backend-owned OAuth and tenant-scoped authorization |
| Over-claiming capture | Cowork docs state best-effort recall/write and no automatic full-transcript guarantee |
| Premature release | Review-only workflow artifacts; no tag/publication without separate explicit approval |
