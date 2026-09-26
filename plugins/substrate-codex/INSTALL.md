# Install runbook: substrate-codex (agent)

## Package layout

- `plugin.json` — portable manifest (`$schema` agent-plugins 1.0.0,
  `extensions.com.openai` with `interface` and `hooks: ./hooks/hooks.json`).
- `.codex-plugin/plugin.json` — Codex compatibility manifest. No `hooks`
  field: the bundled validator rejects it; Codex discovers `hooks/hooks.json`
  by default.
- `mcp.json` — portable MCP config (`substrate-memory`,
  `streamable-http`, `https://app.trysubstrate.co/mcp`).
- `.mcp.json` — legacy MCP config, same server.
- `hooks/hooks.json` — `mcp_tool` hooks against server `substrate-memory`
  (bare key from `mcpServers`), platform `codex`, `turn_id` passed through.
  No `SessionEnd` entry: Codex does not support MCP hooks there.
- `skills/` — `substrate-memory`, `substrate-connect`, `substrate-sync`,
  `substrate-import` (invoke as `$skill-name` in Codex).
- `scripts/substrate_sync.py` — byte-identical copy of the Claude worker's
  script; local transcript reader, stdlib only, never touches the network.

## Contract mapping (docs/mcp-contract.md section 7)

| Host event | Tool | Key args |
|---|---|---|
| `UserPromptSubmit` | `memory_turn_context` | `session_id, prompt, platform, turn_id` |
| `PostToolUse` (all tools) | `memory_capture_tool` | `session_id, tool_use_id, tool_name, tool_input, tool_response, platform, turn_id` |
| `Stop` | `memory_capture_turn` | `session_id, assistant_message=last_assistant_message, agent_context main, platform, turn_id` |
| `SubagentStop` | `memory_capture_turn` | `agent_context subagent, agent_id, parent_session_id=session_id, platform, turn_id` |
| `SessionStart` (`startup\|resume\|clear\|compact`) | `memory_session_boundary` | `session_id, boundary=source, platform` |
| `PreCompact` | `memory_session_boundary` | `boundary compact` |

Timeouts 5 s. Every hook fails open. `${field}` placeholders filling a whole
value keep JSON type.

## Verify

```text
cd Substrate-memory-plugins  # your checkout
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/substrate-codex
.venv/bin/python -m pytest -q tests/test_substrate_codex_plugin.py
CODEX_HOME=/tmp/sb-codex codex plugin marketplace add "$PWD"
CODEX_HOME=/tmp/sb-codex codex plugin add substrate-codex@substrate-marketplace
CODEX_HOME=/tmp/sb-codex codex plugin list --json
```

## Notes for the operator

- Plugin hooks need user trust via `/hooks`; tell the user.
- `server` in hooks is the bare `mcpServers` key `substrate-memory`
  (verified: docs example uses the bare server name; sandbox install shows
  no hook parse warnings — see worker report).
- Never write to `~/.codex` or `~/.agents`; use sandbox `CODEX_HOME` under
  `/tmp` for probing.
