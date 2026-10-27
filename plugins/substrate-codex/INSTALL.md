# Install runbook: substrate-codex (agent)

## Package layout

- `plugin.json` — portable manifest (`$schema` agent-plugins 1.0.0,
  `extensions.com.openai` with `interface` and `hooks: ./hooks/hooks.json`).
- `.codex-plugin/plugin.json` — Codex compatibility manifest. No `hooks`
  field: the bundled validator rejects it; Codex discovers `hooks/hooks.json`
  by default.
- `mcp.json` — portable MCP config (`substrate-memory`,
  `streamable-http`, the url in this plugin's mcp.json).
- `.mcp.json` — legacy MCP config, same server.
- `hooks/hooks.json` — the plugin's hook table: `mcp_tool` hooks against server
  `substrate-memory` (bare key from `mcpServers`), platform `codex`, `turn_id`
  passed through, plus one `command` hook on `UserPromptSubmit` running
  `scripts/substrate_sync.py --host codex --offer-check` (local, no network,
  silent once the import offer was answered; always exits 0). Codex 0.154 does
  not load this file for a local install (see below), so the connect skill
  copies the same table into the user hook file. No `SessionEnd` entry: Codex
  does not support MCP hooks there.
- `scripts/codex_user_hooks.py` — installs, checks and removes the Substrate
  entries in the user hook file (standard library only).
- `skills/` — `substrate-memory`, `substrate-connect`, `substrate-sync`,
  `substrate-import` (invoke as `$skill-name` in Codex).
- `scripts/substrate_sync.py` — byte-identical copy of the Claude script;
  stdlib only. Reads local rollouts; `--upload` sends them straight to the
  `mcp_url` from `memory_import_ticket`. The agent passes a claim (`--claim`,
  made by `--new-claim`) and the script exchanges it for the ticket; an older
  server's ticket comes from a file the script deletes (`--ticket-file`). The ticket never
  goes on a command line; a claim works once and expires after 10 minutes.

## Contract mapping (docs/mcp-contract.md section 7)

| Host event | Tool | Key args |
|---|---|---|
| `UserPromptSubmit` | `memory_turn_context` | `session_id, prompt, platform, turn_id` |
| `PostToolUse` (all tools) | `memory_capture_tool` | `session_id, tool_use_id, tool_name, tool_input, tool_response, platform, turn_id` |
| `Stop` | `memory_capture_turn` | `session_id, assistant_message=last_assistant_message, agent_context main, platform, turn_id` |
| `SubagentStop` | `memory_capture_turn` | `agent_context subagent, agent_id, parent_session_id=session_id, platform, turn_id` |
| `SessionStart` (`startup\|resume\|clear\|compact`) | `memory_session_boundary` | `session_id, boundary=source, platform` |
| `PreCompact` | `memory_session_boundary` | `boundary compact` |
| `UserPromptSubmit` (command) | — | `substrate_sync.py --host codex --offer-check` |

Timeouts 5 s. Every hook fails open. `${field}` placeholders filling a whole
value keep JSON type.

## Verify

```text
cd Substrate-memory-plugins  # your checkout
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/substrate-codex
.venv/bin/python -m pytest -q tests/test_substrate_codex_plugin.py tests/test_substrate_codex_user_hooks.py
CODEX_HOME=/tmp/sb-codex codex plugin marketplace add "$PWD"
CODEX_HOME=/tmp/sb-codex codex plugin add substrate-codex@substrate-marketplace
CODEX_HOME=/tmp/sb-codex codex plugin list --json
```

## User hook file (CDX-04)

Codex 0.154 shows `0 installed` for every event when a plugin's hooks are
loaded from a local install (`/hooks`). Plugin skills load; plugin hooks do
not. Codex does load user hooks from `$CODEX_HOME/hooks.json` (default
`~/.codex/hooks.json`; hook source `user`). Evidence: `codex app-server
generate-json-schema` (`HookSource` includes `user`, `HookHandlerConfig`
`command`/`mcpTool`), and `hooks/list` in an isolated `CODEX_HOME` reports a
user `hooks.json` entry with `source: "user"`, `trustStatus: "untrusted"`.

- **Install** (`$substrate-connect` runs it after a successful connect):
  `python3 scripts/codex_user_hooks.py install`. It merges the seven Substrate
  entries (six events) into `hooks.json`. Each entry carries
  `"x-substrate-managed": "substrate-codex"`. Foreign entries are not changed.
  Our entries go first in each event list. Running it twice writes nothing the
  second time. The first write keeps `hooks.json.substrate-backup`. Writes are
  atomic. A malformed file is reported and left as it is.
- **Approve once**: Codex shows *Hooks need review*. Open `/hooks` and press
  `t` (trust all), or review each entry. Codex stores approvals in
  `config.toml` under `[hooks.state."<path>:<event>:<group>:<handler>"]` with a
  `trusted_hash`. A changed command (for example a new plugin version path)
  needs approval again.
- **Remove**: `python3 scripts/codex_user_hooks.py remove`. It deletes exactly
  the tagged entries and their `[hooks.state]` tables, keeps all other entries
  and tables, and deletes `hooks.json` only if install created it.
  `status` prints the current counts.
- **Duplicates**: if a future Codex loads this plugin's hooks itself, install
  detects it (`hooks/list`, source `plugin`) and removes its own copy instead.
  If the check cannot run, install proceeds.
- The `mcp_tool` entries call the `substrate-memory` server by name. In Codex
  0.154 a local plugin install does not register that server (`/mcp` shows
  "No MCP servers configured"), so each hook reports `unknown MCP server` on
  every prompt until it exists. `$substrate-connect` therefore runs
  `codex mcp add substrate-memory --url <the url in this plugin's mcp.json>` when
  `codex mcp list` does not show it. With the server registered, the hooks
  report `not connected` (authentication required) until sign-in, and the
  prompt still goes through (verified; see the pull request).
- The command hook points at the absolute path of the plugin's script when
  installed. After a plugin upgrade, run `$substrate-connect` again and approve
  the changed entry in `/hooks`.

## Connect and import

Follow `$substrate-connect`, in this order: approve the connection
(`codex mcp login` prints the link; the user chooses Approve connection), then
`memory_search`, **Connected to Substrate.**, install the hooks, and in the same
reply `SYNC --preview` plus the offer **Import all** / **Let me pick** / **Not
now**; then `$substrate-import`. Last, the user approves the hooks in `/hooks`,
starts a new session and says continue. The skill path works even where the
offer hook does not run (hooks untrusted, web).

## Notes for the operator

- Plugin hooks do not load from a local install in Codex 0.154 (`/hooks`
  shows 0 for every event). The connect skill installs the same entries in the
  user hook file; the user approves them once in `/hooks`.
- Not verified here: that `mcp_tool` hooks in the user file resolve the
  `substrate-memory` server and fill `${...}` placeholders the same way as
  plugin hooks. That needs a live, authenticated connection. Without one they
  fail open (see the CDX-04 verification in the pull request).
- Codex runs hooks and commands inside its sandbox. The offer hook still
  speaks when its state directory is read-only; the upload needs network
  access (escalated permissions).
- `server` in hooks is the bare `mcpServers` key `substrate-memory`
  (verified: docs example uses the bare server name; sandbox install shows
  no hook parse warnings — see worker report).
- Never write to `~/.codex` or `~/.agents`; use sandbox `CODEX_HOME` under
  `/tmp` for probing.
- `python3 scripts/codex_user_hooks.py status` shows what is installed.
