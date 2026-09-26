# Threat model

| Threat | Primary controls |
|---|---|
| Credential disclosure | Browser OAuth consent only; no API keys, manual tokens, or client secrets requested, displayed, copied, or pasted; Hermes profile-private custody; no secrets in plugin config |
| Origin substitution | Fixed HTTPS remote MCP URL (`https://app.trysubstrate.co/mcp`); no local server or user-provided secret endpoint in any package |
| Spoofed approval link (Hermes device login) | The link from the server must be HTTPS (plain HTTP only for a loopback host), use path `/oauth/device`, and carry this grant's user code; it is rebuilt from those parts. It may be on the server's public origin even when the plugin reached the server by another address; the plugin then names both addresses. The user checks the code on the approval page |
| TLS interception | HTTPS verification remains enabled; no certificate bypass |
| Tenant crossover | Backend-owned OAuth and tenant-scoped authorization; clients never send tenant or account ids |
| Secret capture in turns or tool traffic | Client-side redaction plus server-side re-redaction on ingest (shared fixture `contract/redaction-fixtures.json`); bounded tool args (4096 bytes) and result excerpts (8192 bytes plus digest); history import only after user confirms the session list |
| Over-claiming capture | Docs state the honest limits: Codex has no session-end hook (server seals idle sessions after 30 minutes); Claude SessionStart at launch fires before MCP connects; an unreopened Cowork cloud session's last turn is not recovered; the fallback package claims best effort only, never automatic full-transcript capture |
| Premature success claim | **Connection approved** is consent only; **Connected to Substrate.** requires an authenticated `memory_search` smoke test |
| Premature release | Review-only workflow artifacts; no tag/publication without separate explicit approval |
