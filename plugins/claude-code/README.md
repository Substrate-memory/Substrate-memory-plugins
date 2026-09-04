# Substrate memory for Claude Code

This is the Claude Code host adapter for Substrate memory, with exact
functional parity to the Hermes reference plugin in `plugins/substrate/`.
The server owns storage, ranking, the associative editor, and evidence.
The plugin caches no memory and fails closed.

## Layout

- `.claude-plugin/plugin.json` — native plugin manifest
  (`substrate-memory` 0.4.0).
- `.mcp.json` — MCP stdio server registration (stdlib-only `mcp_server.py`)
  exposing `memory_search`, `memory_expand`, `memory_evidence`.
- `hooks/hooks.json` — `SessionStart` (static prompt + onboarding notice +
  reset markers), `UserPromptSubmit` (bounded pre-turn memory context),
  `Stop` (completed-turn capture), `SessionEnd` (session markers).
- `substrate_core/` — byte-identical vendored copies of the Hermes
  `client.py`, `contract.py`, `onboarding.py`, `ca/` roots, and
  `contract/envelope-fixtures.json`, plus `hosthome.py`. Only the
  host-home resolution is patched (clearly marked `HOST-HOME PATCH`):
  `SUBSTRATE_HOME` override, `CLAUDE_HOME`, or `~/.claude`. All limits,
  `CLIENT_ID`, scopes, env var names, and validation are unchanged.
- `runtime.py` — port of the host-neutral logic of `plugins/substrate/plugin.py`
  (redaction, turn-context building, capture envelopes, `_CaptureWorker`,
  tool handlers, `STATIC_MEMORY_PROMPT`).
- `connect.py` — optional one-prompt device connect (setup equivalent).
- `commands/substrate-connect.md`, `skills/substrate-memory/SKILL.md` —
  installing-agent and model guidance.

## Install (in Claude Code, point at the repo)

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

The installing agent must behave per `INSTALL.md`. The commands are:

```sh
claude plugin marketplace add <path-to-checkout>
claude plugin install substrate-memory@substrate-marketplace -y
```

(From a local checkout; or substitute the repo URL
`https://github.com/Substrate-memory/Substrate-memory-plugins` for
`<path-to-checkout>` when the upstream marketplace manifest is available.
`-y` runs unattended.)

then restart Claude Code (or `/plugin` → enable). No manual credential setup:
first use starts RFC 8628 device authorization and the exact
`verification_uri_complete` approval URL is shown through the agent; the
tenant-scoped key is stored privately under `~/.claude/substrate/`.
Never paste a key into chat. TLS verification stays enabled, supplemented
only by the pinned public ISRG Root X1/X2 trust anchors.

## Parity map (Hermes → Claude Code)

| Hermes | Claude Code |
|---|---|
| `memory_search/expand/evidence` tools | MCP stdio server, identical schemas/results |
| `pre_llm_call` bounded `<memory-context>` | `UserPromptSubmit` hook → `additionalContext` |
| static system-prompt section | `SessionStart` hook → `additionalContext` (+ skill text) |
| `post_llm_call` nonblocking capture | `Stop` hook → detached `capture_worker.py` |
| `on_session_reset` / `on_session_finalize` | `SessionStart(source=clear)` reset marker / `SessionEnd` marker |
| device onboarding link via tool/turn | hook `systemMessage` + tool `authorization_required` result |
| fail closed | identical: empty context, background capture, bounded errors |

## Format evidence (no invented formats)

Web search was unavailable in this environment (no Serper API key), so the
Claude Code plugin format was verified against the locally installed CLI
(`claude --version` → 2.1.259) as ground truth:

- `claude plugin --help` / `claude plugin marketplace --help` /
  `claude plugin validate --help` — installer, marketplace, validator verbs.
- Bundled official marketplace cache
  (`~/.claude/plugins/marketplaces/claude-plugins-official/`):
  `.claude-plugin/marketplace.json` (`$schema`, `name`, `owner`, `plugins[]`
  with `source: "./plugins/<dir>"` for in-repo plugins), plugin roots with
  `.claude-plugin/plugin.json` (`name`, `description`, `author`, optional
  `version`), `hooks/hooks.json` (`{description, hooks: {Event: [{hooks:
  [{type: "command", command, timeout}]}]}}`), `.mcp.json` server maps
  supporting `${CLAUDE_PLUGIN_ROOT}`, hook scripts reading JSON on stdin and
  printing JSON (`systemMessage`, `hookSpecificOutput.additionalContext`)
  with exit 0.
- `claude plugin validate ./plugins/claude-code` passes (see
  `.parity-report.md`).

Official references (consult docs.anthropic.com under
en/docs/claude-code for plugins, hooks, mcp, and plugin-marketplaces;
not fetched here because this environment has no web search).
