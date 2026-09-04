# Substrate ↔ Cowork plugin wire contract

`contract_version` **1**, `schema_version` **3**. This plugin vendors the
Hermes reference contract byte-identically (`substrate_core/contract.py` and
`substrate_core/contract/envelope-fixtures.json`) and implements the same
rules as `plugins/substrate/CONTRACT.md`. Anything not restated here is
unchanged from that document.

Fixture SHA-256: 627615398b726d04f32b5bab58b480b00ba85ca80c65d66864d7e8ea1a30ab85

Host adaptations (only these differ from Hermes):

- Home resolution: `SUBSTRATE_HOME`, then `CLAUDE_CONFIG_DIR`, then `~/.claude`
  (see `substrate_core/hosthome.py`). Env names, `CLIENT_ID`,
  `SUBSTRATE_API_KEY`, limits, allowed hosts, and validation are unchanged.
- Tool transport: the same three tools and schemas are served over MCP stdio
  (`substrate_core/mcp_server.py`) instead of an in-process registry; tool
  names, descriptions, JSON schemas, bounded JSON-string results, and error
  strings are identical.
- Pre-turn context: the same `turn-context` request/response validation feeds
  the `UserPromptSubmit` hook's `additionalContext` instead of `pre_llm_call`.
- Static prompt: the identical `STATIC_MEMORY_PROMPT` string is delivered via
  the `SessionStart` hook and `skills/substrate-cowork-memory/SKILL.md`.
- Capture: the same `capture_turn` envelope and worker semantics run
  synchronously in a detached runner (hooks exit immediately) or via the same
  threaded queue inside the long-lived MCP server process.
- Session markers: `SessionEnd` posts `capture_session` with boundary `end`
  (the Cowork equivalent of `on_session_finalize`); subagent completion also
  snapshots capture via `SubagentStop`.
- Host limitation: Cowork exposes no `on_session_reset`/new-session event, so
  only boundary `end` is ever emitted and boundary `reset` (with
  `next_session_id`) never is; `next_session_id`/`parent_session_id` are
  therefore normally absent, and resumed sessions get clean windows with no
  explicit old→new link (see CONTRACT.md gap note in the Hermes reference).
