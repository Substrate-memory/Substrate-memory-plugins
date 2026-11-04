# Security

Do not put API keys, OAuth tokens, client secrets, passwords, or private credentials in this repository, plugin configuration, prompts, or chat. Authentication uses browser consent owned by the backend. The agent shows the exact consent URL (**Connect your agent to Substrate**) and never asks the user to paste a secret. The product does not issue API keys to users.

## What each package does

- **Hermes** (`plugins/substrate-hermes`) keeps its fail-closed transport, TLS verification, profile-private credential custody, redaction, and durable write-ahead spool boundaries.
- **Claude and Codex** (`plugins/substrate-claude`, `plugins/substrate-codex`) authenticate with MCP OAuth (authorization code + PKCE, scopes `retrieve capture`) and approve on the same **Connect your agent to Substrate** screen. Secrets are redacted client-side by the sync path and always re-redacted server-side on ingest.
- **Fallback** (`plugins/substrate-mcp`) is only a remote HTTP MCP manifest, a usage skill, and a self-check command. It has no local server or hooks and does not guarantee full-transcript capture. Its endpoint `https://app.trysubstrate.co/mcp` and the browser OAuth backend (dynamic client registration, PKCE) are deployed.

## Verification and import rules

Do not report success without an authenticated smoke call: only a passed `memory_search` means **Connected to Substrate.** History import runs only after the user chooses **Import all** or picks sessions, sends redacted raw turns (redacted again server-side), and never sends the current session. Claude Code and Codex upload with a short-lived import-only ticket from `memory_import_ticket` (60 minutes, `memory_import`/`memory_import_status` only, revoked with its parent connection); the script gets it from the Substrate server with a single-use claim (or reads it from a temporary file the agent wrote and deletes it at once), never from argv or the environment, and agents never show it in chat.

## Local code, credentials and network (Claude and Codex)

- **One local script.** The only code the Claude and Codex plugins run outside the declared MCP server (`https://app.trysubstrate.co/mcp`) is the bundled, standard-library `scripts/substrate_sync.py`, from the plugin's hooks and from its connect, import and sync commands. The Claude commands pre-approve exactly that script at `${CLAUDE_PLUGIN_ROOT}` with `--host claude`, nothing broader.
- **Credentials.** The script handles only Substrate's own import ticket. It never reads the host's sign-in files (Claude Code's `.credentials.json`, Codex's auth store), the keychain, SSH or cloud keys, and it never prints a ticket.
- **Network.** It talks only to Substrate over HTTPS (`*.trysubstrate.co`; loopback for tests); any other URL is refused before a request is made. It downloads no code and runs nothing it downloaded. Updates come only through the plugin marketplace. See [docs/threat-model.md](docs/threat-model.md) and [docs/installation.md](docs/installation.md).
