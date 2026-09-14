# Security

Do not put API keys, OAuth tokens, client secrets, passwords, or private
credentials in this repository, plugin configuration, prompts, or chat.
Authentication uses browser OAuth consent owned by the backend. The agent shows
the exact consent URL and never asks the user to paste a secret.

The Hermes plugin keeps its existing fail-closed transport, TLS verification,
profile-private credential custody, redaction, and durable spool boundaries.
Those runtime files are golden from v0.5.0.

The `substrate-mcp` package is only a remote HTTP MCP manifest, a generic skill,
and a self-check command. It has no local server or hooks and does not guarantee
full-transcript capture. Its endpoint `https://app.trysubstrate.co/mcp` and the
browser OAuth backend (dynamic client registration, PKCE) are deployed. Do not
report success without an authenticated smoke call.
