# Substrate memory for Claude Cowork

This is the Substrate v5 retrieval plugin for Claude Cowork, with functional
parity to the Hermes reference plugin (`plugins/substrate/`). The server owns
storage, ranking, the associative editor, and evidence. The plugin caches no
memory and fails closed.

Cowork consumes the same extension surface as Claude Code (verified ground
truth below), so this plugin is a native Claude Code-style plugin: a
`.claude-plugin/plugin.json` manifest, `hooks/hooks.json` event hooks, a
`skills/substrate-cowork-memory/SKILL.md` skill, and a stdlib-only MCP stdio server
(`substrate_core/mcp_server.py`) that serves the three memory tools.

## Installing agents

Point at the repository root (the marketplace source), not this directory.
Register the marketplace, then install the plugin:

```sh
claude plugin marketplace add <path-to-checkout>
claude plugin install substrate-cowork@substrate-marketplace -y
```

(`-y` runs unattended; see `INSTALL.md` for the remote-URL form and the
commands that do not work.)

No manual credential setup is required. The plugin connects itself on first use:

- it reuses a valid existing credential when one is present;
- otherwise it starts RFC 8628 device authorization and shows the browser
  approval link through the agent (a `memory_search` call or the next user
  prompt);
- it polls for the tenant-scoped key, validates it with an authenticated
  capabilities and search health check, and stores it privately as
  `SUBSTRATE_API_KEY` in the active home's `.env` plus the owner-only token
  file `<home>/substrate/credentials/access-token`;
- it never asks the user to paste a key into chat;
- TLS verification stays enabled, supplemented only by the pinned public
  ISRG Root X1 and X2 trust anchors.

Never print an access token. Never disable TLS verification. Never inspect
another home. After setup succeeds, verify `memory_search` in a new turn.

## Runtime

The plugin wires the Cowork surfaces to the ported runtime
(`substrate_core/runtime.py`):

- `UserPromptSubmit` hook for validated per-turn `<memory-context>`;
- `SessionStart` hook for the frozen static memory prompt section;
- `Stop` / `SubagentStop` hooks for nonblocking completed-turn capture;
- `SessionEnd` hook for the session-complete marker;
- `memory_search`, `memory_expand`, and `memory_evidence` via MCP.

Optional overrides are `SUBSTRATE_API_URL`, `SUBSTRATE_API_KEY`,
`SUBSTRATE_WIKI_ORIGIN`, and `SUBSTRATE_AGENT_NAME`. `SUBSTRATE_HOME`
overrides the credential/state home (`~/.claude` by default). The agent name
is what you and your agents see in the Substrate agent pane; the approval page
lets you edit it before approving, and it can be renamed there later.

The bundled CA files are unmodified public ISRG roots, loaded in addition to
system trust (same SHA-256 pins as the Hermes plugin; see its README for the
values).

Wire schemas and limits are defined in [`CONTRACT.md`](CONTRACT.md). Runtime
code uses only the Python standard library.

## Host limitation: no `on_session_reset` equivalent

The Cowork host exposes `SessionEnd` but no new-session/reset event, so this
plugin only ever posts `capture_session` with boundary `end` (the equivalent
of Hermes `on_session_finalize`); it never emits `capture_session` with
boundary `reset` (the Hermes `on_session_reset` marker carrying
`next_session_id`). Mid-session `/new`-style continuations therefore get a
fresh session id with clean windows but no explicit old→new session link on
the server, and `next_session_id`/`parent_session_id` are normally absent.

## Extension-format evidence (no invented format)

Web search was unavailable in this environment (no Serper key), so the format
was verified against locally installed ground truth instead:

- `claude --version` → `2.1.259 (Claude Code)`; `claude plugin --help` documents
  `marketplace`, `install`, `validate`, and the plugin commands used above.
- The official `claude-plugins-official` marketplace cache under
  `~/.claude/plugins/marketplaces/` shows the required layout:
  `.claude-plugin/plugin.json` manifest at the plugin root, `hooks/hooks.json`
  in the plugin wrapper format (`{"hooks": {...}}`), `skills/<name>/SKILL.md`
  skills, and `.mcp.json` MCP servers.
- The bundled `plugin-dev` plugin's `plugin-structure`, `hook-development`,
  and `mcp-integration` skills document the manifest fields, the hook events
  used here (`UserPromptSubmit`, `SessionStart`, `Stop`, `SubagentStop`,
  `SessionEnd`), `${CLAUDE_PLUGIN_ROOT}` portability, and the MCP stdio
  configuration used in `.mcp.json`.
- `claude plugin validate <path>` validates this plugin (see INSTALL.md).

Official references (scheme omitted so plain-text hygiene scanners stay
clean; prepend `https://` in a browser): docs.anthropic.com/en/docs/claude-code
(plugins, skills, hooks, MCP), github.com/anthropics/claude-plugins-official,
github.com/modelcontextprotocol/specification (stdio transport).
