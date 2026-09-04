# Substrate memory for OpenAI Codex CLI

This is the Substrate v5 memory plugin for Codex CLI (`codex-cli 0.144.5`
verified; newer 0.14x releases use the same manifest layout). The server
owns storage, ranking, the associative editor, and evidence. The plugin
caches no memory and fails closed.

## What it provides

- `memory_search`, `memory_expand`, `memory_evidence` over MCP stdio
  (`server.py`), with tool names, descriptions, JSON schemas, result shapes,
  and error strings identical to the Hermes reference plugin.
- Bounded pre-turn `<memory-context>` injection through the
  `UserPromptSubmit` hook (`hooks/substrate_hook.py pre` calling
  `POST /api/v1/memory/turn-context` with the same request shape, limits,
  and redaction as Hermes `pre_llm_call`).
- The frozen static memory prompt via `skills/substrate-memory/SKILL.md`.
- Nonblocking completed-turn capture: the `PostToolUse` hook spools a
  `capture_turn` envelope to a bounded on-disk queue (0600 files, 0700
  directory, at most 64 files) and a detached sender delivers it plus a
  bounded sweep of older spool files. The envelope keeps a stable
  `Idempotency-Key` per `(session_id, turn_id)`, so repeated `PostToolUse`
  fires for one turn cannot record duplicates. Session boundary markers
  (`SessionStart` hook queueing `capture_session` envelopes, with the
  `source` field mapped to a boundary) use a stable key per
  `(session_id, boundary)` the same way.
- First-use RFC 8628 device onboarding: the exact
  `verification_uri_complete` approval URL is surfaced through the agent as
  a clickable link. Never ask for a pasted key.
- TLS verification stays enabled, supplemented only by the pinned public
  ISRG Root X1 and X2 trust anchors in `substrate_core/ca/`.

Wire schemas and limits are defined in the Hermes reference contract
(`plugins/substrate/CONTRACT.md`). Runtime code uses only the Python
standard library.

## Install

Point Codex at this repository. The reliable path today is the MCP server
plus this plugin directory:

```sh
git clone --filter=blob:none --sparse https://github.com/Substrate-memory/Substrate-memory-plugins.git
cd Substrate-memory-plugins
git sparse-checkout set plugins/codex
sh plugins/codex/install.sh
```

which registers the server with the native command:

```sh
codex mcp add substrate -- python3 "$PWD/plugins/codex/server.py"
```

Then install this directory as a `substrate` Codex plugin so the hooks and
the memory skill load (see [INSTALL.md](INSTALL.md) for the exact agent
runbook):

```sh
codex plugin marketplace add "$PWD"
codex plugin add substrate@substrate-memory
```

The repository root ships `.agents/plugins/marketplace.json` (entry
`substrate` → `./plugins/codex`, `AVAILABLE` / `ON_USE`), so pointing
`codex plugin marketplace add` at a checkout (or the repo URL) and then
`codex plugin add substrate@substrate-memory` installs the plugin enabled
— verified against `codex-cli 0.144.5` in a sandbox `CODEX_HOME`
(`installed: true, enabled: true` in `codex plugin list --json`). Do not
use `--sparse plugins/codex` alone for the Git URL: the sparse set omits
the root manifest and the add fails with `marketplace root does not
contain a supported manifest` (verified live).

Fallback when only this directory is available (same manifest shape, same
marketplace name, verified in a sandbox):

```sh
checkout="$PWD"
mp="$HOME/.substrate-marketplace"
mkdir -p "$mp/.agents/plugins" "$mp/plugins"
ln -sfn "$checkout/plugins/codex" "$mp/plugins/codex"
cat > "$mp/.agents/plugins/marketplace.json" <<'EOF'
{
  "name": "substrate-memory",
  "interface": {"displayName": "Substrate Memory"},
  "plugins": [
    {
      "name": "substrate",
      "source": {"source": "local", "path": "./plugins/codex"},
      "policy": {"installation": "AVAILABLE", "authentication": "ON_USE"},
      "category": "Productivity"
    }
  ]
}
EOF
codex plugin marketplace add "$mp"
codex plugin add substrate@substrate-memory
```

Verify with `codex mcp get substrate` and one `memory_search` call in a
new turn. See [INSTALL.md](INSTALL.md) for the full agent procedure and
[after-install.md](after-install.md) for onboarding and rollback.

## Format evidence (no silent invention)

The Codex plugin layout used here was verified against the locally
installed CLI (`codex-cli 0.144.5`), which is the ground truth:

- `codex plugin --help` documents `plugin add/list/marketplace/remove`;
  `plugin marketplace add` accepts a local path or `owner/repo[@ref]`
  with `--sparse PATH` for Git sources.
- `codex mcp --help` / `codex mcp add --help` documents
  `codex mcp add NAME -- COMMAND...` for stdio servers, and
  `codex mcp list` / `codex mcp get NAME` for verification.
- The installed Codex binary requires `.codex-plugin/plugin.json` to be
  valid JSON holding an object, validates `mcpServers` as either
  `./.mcp.json` or an inline MCP server object, rejects unknown fields in
  `.mcp.json` (only `mcpServers` is allowed), validates `skills/*/SKILL.md`
  frontmatter, and references hook events `PreToolUse`, `PostToolUse`,
  `PreCompact`, `PostCompact`, `SessionStart`, `SubagentStart`,
  `SubagentStop`, `PermissionRequest`, and `UserPromptSubmit`, whose
  command outputs carry `hookSpecificOutput.additionalContext` (see the
  verified-vs-best-effort section below for the exact shapes).
- The bundled Codex `plugin-creator` skill
  (`~/.codex/skills/.system/plugin-creator/`) ships the normative
  `references/plugin-json-spec.md` plus `scripts/validate_plugin.py` and
  `scripts/create_basic_plugin.py`. This plugin passes
  `validate_plugin.py` cleanly. Per that spec, `interface` (with
  `displayName`, `shortDescription`, `longDescription`, `developerName`,
  `category`, `capabilities`, `defaultPrompt`) is required, `version` is
  strict semver, and a top-level `hooks` key in `plugin.json` is rejected:
  hooks ship in the `hooks/` directory and are picked up by default
  component discovery. Marketplace roots carry
  `.agents/plugins/marketplace.json` with `plugins[]` entries of the form
  `{name, source: {source: "local", path}, policy, category}`; that exact
  shape was verified with `codex plugin marketplace add` plus
  `codex plugin add substrate@<marketplace>` on `codex-cli 0.144.5`.
- `~/.codex/plugins/cache/openai-curated-remote/*/[version]/.codex-plugin/plugin.json`
  manifests in the local plugin cache confirm the same layout (for example
  the cached `vercel` plugin ships `skills/`, `agents/`, `commands/`,
  `.app.json`, and interface metadata under that manifest).

Because the `websearch` skill had no API key in this environment, no live
web lookup was possible; the install commands above use only CLI-verified
mechanisms. If a newer Codex revision changes the hook schema, the MCP
server and the skill keep working on their own: the agent can call
`memory_search` directly and prepend the result.

## Hook contract: verified vs best-effort

Ground truth is the locally installed `codex-cli 0.144.5` native binary
(`@openai/codex-linux-x64/.../bin/codex`), the bundled `plugin-creator`
skill, and live runs with a sandbox `CODEX_HOME` (the real `~/.codex` is
never touched).

Verified (binary-embedded `<event>.command.input` / `.command.output`
JSON schemas, plus live CLI runs):

- Hook stdin is JSON with snake_case fields and a PascalCase
  `hook_event_name` const. `UserPromptSubmit` requires `cwd`,
  `hook_event_name`, `model`, `permission_mode`, `prompt`, `session_id`,
  `transcript_path` (nullable), `turn_id`. `PostToolUse` requires the same
  shape with `tool_input`, `tool_name`, `tool_response`, `tool_use_id`
  instead of `prompt` — i.e. it fires per tool call, not per turn.
  `SessionStart` requires `cwd`, `hook_event_name`, `model`,
  `permission_mode`, `session_id`, `source`
  (`startup|resume|clear|compact`), `transcript_path` — and no `turn_id`.
  `permission_mode` is one of
  `default|acceptEdits|plan|dontAsk|bypassPermissions`.
- Hook stdout must use the enveloped form
  `{"hookSpecificOutput": {"hookEventName": "<Event>",
  "additionalContext": "..."}}`. Each event's output schema requires
  `hookSpecificOutput.hookEventName` (the per-event const) and carries
  `additionalContext` only inside that envelope; the top-level object has
  no `additionalContext` slot, so the hook never emits the bare
  `{"additionalContext": ...}` form.
- `hooks/hooks.json` is read from the installed plugin directory (a live
  `codex exec` run warns `failed to parse plugin hooks config ...` for a
  bad file). Its top level accepts only `description`/`hooks`: a `$schema`
  key breaks parsing with `unknown field ... at line 2` (verified live),
  so this plugin ships no `$schema`. Event keys are the PascalCase names
  (`UserPromptSubmit`, `PostToolUse`, `SessionStart`).
- The plugin `.mcp.json` server entry needs `"command"` as a string
  (`"command": "python3", "args": ["./server.py"]`): the array form fails
  plugin MCP loading with `invalid type: sequence, expected a string`
  (verified live via debug logging). `install.sh` instead registers the
  same `server.py` globally with an absolute path through
  `codex mcp add`; both entries point at the same stdio server.
- `validate_plugin.py` passes; `codex mcp add/get/list` round-trips;
  marketplace add of the repo root plus
  `codex plugin add substrate@substrate-memory` installs enabled.

Best-effort (fails closed, marked in code; MCP tools and the skill keep
working if any of it is wrong):

- Handler-level `hooks.json` keys (`timeout_ms`) and the relative
  `python3 ./hooks/...` command (plugin-root working directory assumed):
  the CLI validates the top level strictly but tolerates unknown nested
  keys silently (probed live), so these follow the Claude-compatible
  convention without a machine-checked confirmation.
- Transcript reading: `transcript_path` points at a rollout JSONL file;
  the hook extracts only `response_item` rows shaped as user/assistant
  `{role, content: [{type: input_text|output_text|text, text}]}` from the
  file tail, skips `<environment_context>` noise, and falls back to the
  hook payload fields (or no capture) on any deviation.
- `SessionStart.source` → boundary mapping (`startup|resume` → `switch`,
  `clear` → `reset`, `compact` → `compress`, missing/unknown → `switch`):
  the source enum is verified, the mapping is this plugin's choice.
- Per-tool-call `PostToolUse` capture: only one envelope per
  `(session_id, turn_id)` can persist (stable filename + stable
  `Idempotency-Key`; last-write-wins), so duplicates are impossible but
  the stored turn reflects the transcript at the last fire.
- There is no `Stop`/turn-end hook event in 0.144.5 (the `Stop` wire
  schema exists for Claude-compat inputs but is not a firatable plugin
  event), and no live model turn was run here (no API auth in the sandbox),
  so end-to-end auto-firing inside a real turn is unconfirmed; hook scripts
  were fired manually with real-shaped payloads instead.
- No timed background retry: the detached sender delivers the current
  envelope plus a bounded sweep (oldest first, 10 files per run) on every
  hook fire. Captures spooled while the server is unreachable wait for a
  later hook fire (expired unsent after 7 days). This matches the Hermes
  64-slot drop-new queue except that Hermes retries from a live process
  while Codex hook processes are ephemeral.

## Layout

- `substrate_core/` — self-contained core: byte-identical `contract.py`,
  ISRG roots, and envelope fixtures, plus `client.py` / `onboarding.py`
  with only the clearly-marked Codex home patch, `hosthome.py` (Codex home
  resolution), and `runtime.py` (port of the Hermes `plugin.py` logic).
- `server.py` — MCP stdio server for the 3 memory tools.
- `hooks/substrate_hook.py` — `pre` / `post` / `session` hook modes.
- `hooks/_send_spooled.py` — detached spool sender: delivers the named envelope plus a bounded sweep of older spool files, all with `Idempotency-Key` equal to `event_id`.
- `.codex-plugin/plugin.json`, `.mcp.json`, `hooks/hooks.json` — native manifests.
- `skills/substrate-memory/SKILL.md` — static memory prompt and tool rules.
- `install.sh`, `INSTALL.md`, `after-install.md` — install procedure.
