# Substrate memory for Grok Bot (xAI Grok agent surface)

This is the Substrate v5 memory plugin for Grok Bot, with exact functional
parity to the Hermes reference plugin in `plugins/substrate/`. The server
owns storage, ranking, the associative editor, and evidence. The plugin
caches no memory and fails closed. Runtime code uses only the Python
standard library.

## What Grok surface this targets (researched 2026-09-04)

Grok / xAI has **no official bot-plugin marketplace or plugin manifest
format**. Evidence (bare hostnames to keep this repo hygiene-clean;
prepend `https://` in a browser):

- xAI API tool calling is OpenAI-compatible function calling: you declare
  `name` + `description` + JSON-schema `parameters` in `tools`, the model
  returns a tool call, you execute it locally and return the result.
  See `docs.x.ai/developers/tools/function-calling` and
  `docs.x.ai/developers/tools/overview`.
- The xAI API also supports server-side Remote MCP tools (`tools` entries
  of `type: mcp` with `server_url` + `server_label`), streaming-HTTP/SSE
  only. See `docs.x.ai/developers/tools/remote-mcp`.
- The `grok` agent CLI ("Grok Build") supports MCP servers via
  `grok mcp add/list/remove/doctor`, TUI `/mcps`, and config files
  `~/.grok/config.toml` (`[mcp_servers.*]`) or project `.grok/config.toml`,
  compatible with `~/.claude.json`, `.cursor/mcp.json`, `.mcp.json`.
  See `docs.x.ai/build/features/mcp-servers` and `docs.x.ai/build/overview`.

So this plugin implements the **closest faithful surface**:

| Piece | Implementation |
|---|---|
| 3 memory tools, identical schemas/shapes | stdlib MCP stdio server `server.py` + function manifest `grok-tools.json` |
| Pre-turn bounded `<memory-context>` | `bridge.py pre_turn_context()` (MCP has no hook channel) |
| Static memory prompt | `instructions.md` + `bridge.STATIC_MEMORY_PROMPT` |
| Completed-turn capture (nonblocking) | `bridge.py capture_turn()` |
| Session markers | `bridge.py session_reset()` / `session_end()` |
| RFC 8628 onboarding, clickable approval URL | vendored `substrate_core/onboarding.py`, surfaced via tool results and `setup.py` |
| Repo-local manifests (not an xAI format) | `grok-bot.yaml`, `plugin.json`, `hooks/hooks.json` — consumed only by `install.sh` and these docs (see the `_note`/header in each file) |

## Evidence and compatibility: what is verified vs best-effort

**Verified in this repo (offline, no Grok host needed):**

- `server.py` is a working stdlib MCP stdio server exposing exactly the
  three memory tools with Hermes-identical schemas — covered by the
  `test_mcp_stdio_round_trip` test in `tests/test_grok_bot_plugin.py`.
- `grok-tools.json` is valid OpenAI-compatible function-calling JSON whose
  tool names, descriptions, and schemas match the Hermes reference —
  covered by the schema-parity test.
- `bridge.py` turn wiring (`pre_turn_context` / `capture_turn` /
  `session_reset` / `session_end`), redaction, capture envelopes, and
  fail-closed retrieval are covered by offline tests.
- `install.sh` (POSIX, idempotent) clones the repo over HTTPS and registers
  `server.py` in `$GROK_HOME/mcp.json`.

**Best-effort / NOT verified against a real Grok host:**

- No `grok` binary was available in the build environment, so no
  `grok ...` CLI command (`grok plugin install`, `grok mcp add/list`,
  `grok inspect`, `/mcps`) was run or verified. The UNVERIFIED section of
  `INSTALL.md` lists them as aspirational — confirm with `grok --help` on
  a real Grok Build host before relying on them.
- `grok-bot.yaml`, `plugin.json`, and `hooks/hooks.json` are repo-local
  conventions, not a published xAI manifest format; no host tool was
  observed consuming them here.
- grok.com web bots and X bot integrations expose no tool/hook surface we
  can install into, so there the agent follows `INSTALL.md` manually
  (paste `instructions.md`, run the MCP server locally, relay tool calls).

**Sources** (bare hostnames to keep this repo hygiene-clean; prepend
`https://` in a browser): `docs.x.ai/developers/tools/function-calling`,
`docs.x.ai/developers/tools/overview`,
`docs.x.ai/developers/tools/remote-mcp`,
`docs.x.ai/build/features/mcp-servers`, `docs.x.ai/build/overview`.

## Install (point the agent at the repo URL)

Say to your Grok agent:

> Install the memory plug-in at `Substrate-memory/Substrate-memory-plugins`
> on github.com.

The agent follows `INSTALL.md`. Non-interactive equivalent:

```sh
sh plugins/grok-bot/install.sh
```

No manual credential setup is required: first use starts RFC 8628 device
authorization and shows the browser approval link through the agent
(`memory_search` or the first turn). Approval stores the tenant-scoped key
privately as `SUBSTRATE_API_KEY` in the active profile `.env`
(`~/.grok/.env` by default) and `~/.grok/substrate/credentials/`.
It never asks the user to paste a key into chat. TLS verification stays
enabled, supplemented only by the pinned public ISRG Root X1 and X2 trust
anchors (see `substrate_core/ca/`).

Wire schemas and limits are defined in the Hermes reference
`plugins/substrate/CONTRACT.md`. Vendored `substrate_core/` holds
byte-identical copies of `contract.py`, the CA roots, and the envelope
fixtures; `client.py` / `onboarding.py` carry only the marked Grok
host-home patch. `CLIENT_ID`, scopes, grant, env names, limits, and
validation are unchanged.
