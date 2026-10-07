# Security

Do not put API keys, OAuth tokens, client secrets, passwords, or private credentials in this repository, plugin configuration, prompts, or chat. Authentication uses browser consent owned by the backend. The agent shows the exact consent URL (**Connect your agent to Substrate**) and never asks the user to paste a secret. The product does not issue API keys to users.

## What each package does

- **Hermes** (`plugins/substrate-hermes`) keeps its fail-closed transport, TLS verification, profile-private credential custody, redaction, and durable write-ahead spool boundaries.
- **Claude and Codex** (`plugins/substrate-claude`, `plugins/substrate-codex`) authenticate with MCP OAuth (authorization code + PKCE, scopes `retrieve capture`) and approve on the same **Connect your agent to Substrate** screen. Secrets are redacted client-side by the sync path and always re-redacted server-side on ingest.
- **Fallback** (`plugins/substrate-mcp`) is only a remote HTTP MCP manifest, a usage skill, and a self-check command. It has no local server or hooks and does not guarantee full-transcript capture. Its endpoint `https://app.trysubstrate.co/mcp` and the browser OAuth backend (dynamic client registration, PKCE) are deployed.

## Verification and import rules

Do not report success without an authenticated smoke call: only a passed `memory_search` means **Connected to Substrate.** History import runs only after the user chooses **Import all** or picks sessions, sends redacted raw turns (redacted again server-side), and never sends the current session. Claude Code and Codex upload with a short-lived import-only ticket from `memory_import_ticket` (60 minutes, `memory_import`/`memory_import_status` only, revoked with its parent connection); the script reads it from the environment, never argv, and agents never show it in chat. See [docs/threat-model.md](docs/threat-model.md) and [docs/installation.md](docs/installation.md).
