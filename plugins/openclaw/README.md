# Substrate memory for OpenClaw

This is the Substrate v5 retrieval plugin for OpenClaw. The server owns storage,
ranking, the associative editor, and evidence. The plugin caches no memory and
fails closed. Functional parity target: the Hermes reference plugin in
`../substrate/` (frozen; never modified by this plugin).

## What it is

A thin zero-dependency JS adapter (`index.js`, Node builtins only) that registers
the same 3 tools and the same lifecycle behavior, delegating every memory
operation to the vendored stdlib-only Python core in `substrate_core/` via a
short-lived `bridge.py` stdio-JSON child process:

- `before_prompt_build` fetches bounded `<memory-context>` from
  `POST /api/v1/memory/turn-context` and returns it as `prependContext`;
  the static memory prompt (`STATIC_MEMORY_PROMPT`, identical string on both
  sides) is returned as cacheable `appendSystemContext`;
- `agent_end` captures the completed turn with a detached `bridge.py capture`
  call (synchronous POST inside that child, timeout 5 s) so the agent turn is
  never blocked;
- `session_end` / `before_reset` queue `capture_session` boundary markers
  (`end`, or `reset` for reason `new`/`reset`); `session_start` seals a
  `switch` marker on the session named by `resumedFrom`;
- `memory_search`, `memory_expand`, `memory_evidence` with identical names,
  descriptions, JSON schemas, bounded JSON-string results, and identical error
  strings (`{"error": "<category>"}` inside the tool text);
- first use with no key starts RFC 8628 device authorization and surfaces the
  exact `verification_uri_complete` approval URL through the tool result (JSON
  field) or the turn context (`<substrate-connect>` block). Never ask the user
  to paste a key. The issued key is validated (capabilities + search health
  check) and stored privately as `SUBSTRATE_API_KEY` in the active OpenClaw
  home's `.env` (0600), with the token file at
  `<home>/substrate/credentials/access-token` (0600);
- TLS verification stays enabled; system trust is supplemented only by the
  pinned public ISRG Root X1/X2 anchors in `substrate_core/ca/`.

Why subprocess delegation instead of a pure-JS port: the wire contract,
validators, redaction, envelope builders, and device flow stay byte-identical
with the reference implementation (one Python core, no drift), the JS shim
needs zero npm runtime dependencies, and OpenClaw hosts already run Node 22+
where spawning `python3` is cheap. Requirement: `python3` (3.11+) on PATH
(override with `SUBSTRATE_PYTHON`). Every handler fails closed when Python or
the network is unavailable.

## Layout

- `openclaw.plugin.json` — native manifest (id `substrate`, 3 tools, config schema).
- `package.json` — zero runtime dependencies; `openclaw.extensions: ["./index.js"]`
  (plain JS entry, no build step).
- `index.js` — adapter: `export default function register(api)` using
  `api.registerTool` + `api.on(...)` (falls back to `api.registerHook`).
- `substrate_core/` — vendored core: byte-identical `contract.py`, `ca/`,
  `contract/envelope-fixtures.json`; `client.py` / `onboarding.py` with only
  the marked OpenClaw host-home patch; new `hosthome.py` (home resolution),
  `runtime.py` (host-neutral port of the reference `plugin.py`), `bridge.py`
  (stdio JSON CLI), plus `setup.py` one-prompt connect at the plugin root.
- `INSTALL.md`, `after-install.md` — installing-agent behavior.

## Install

Point an OpenClaw agent at the repo URL (see `INSTALL.md` for the exact
agent steps):

```sh
git clone https://github.com/Substrate-memory/Substrate-memory-plugins.git
openclaw plugins install --link ./Substrate-memory-plugins/plugins/openclaw --force
openclaw plugins enable substrate
```

then grant conversation hooks and restart (details in `INSTALL.md`).

## Configuration

Optional plugin config (explicit `SUBSTRATE_*` env vars always win).
Auth uses the device-onboarding flow plus the 0600 token file; never put
secrets in plugin config (`openclaw.json` is plaintext — there is no
`server.apiKey` option by design). `SUBSTRATE_API_KEY` env remains supported
for headless hosts. `python` selects the bridge interpreter (lowest
precedence after `SUBSTRATE_PYTHON` env):

```json
{
  "plugins": {
    "entries": {
      "substrate": {
        "enabled": true,
        "hooks": { "allowPromptInjection": true, "allowConversationAccess": true },
        "config": { "server": { "url": "", "agentName": "" }, "python": "python3" }
      }
    }
  }
}
```

Home resolution: `SUBSTRATE_HOME`, else `OPENCLAW_STATE_DIR`, else the
installed-layout home, else `~/.openclaw`.

## Format evidence (no invented format)

Researched against the real, current OpenClaw plugin system:

- Local ground truth on this machine:
  `TencentDB-Agent-Memory/MemoryCore/openclaw-plugin/` (`openclaw.plugin.json`,
  `package.json` with `openclaw.extensions`, `index.ts` using
  `api.pluginConfig` / `api.logger` / `api.registerTool(def, { name })` /
  `api.on("before_prompt_build" | "agent_end", ...)` returning
  `{ prependContext, appendSystemContext }`; `openclaw.plugin.json` and
  `package.json` with `openclaw.extensions`) and its `openclaw.json` shape
  (`openclaw plugins install -l <dir>`, config with `plugins.slots` /
  `plugins.entries` and `hooks.allowPromptInjection` /
  `hooks.allowConversationAccess` on OpenClaw >= 2026.4.24).
- Official format confirmed in the published `openclaw` npm package
  (version 2026.9.1, inspected locally): `docs/plugins/building-plugins.md`
  (manifest + `openclaw.extensions` + `openclaw plugins install
  clawhub:|npm:|git|local-path` sources), `docs/plugins/hooks.md` (typed hooks
  via `api.on(...)`; `registerHook` is the internal-hook API, not the typed
  one), `docs/plugins/manifest.md` (manifest read before code runs),
  `docs/plugins/sdk-entrypoints.md` (plain JS runtime entries need no build),
  and `dist/*.d.ts` hook catalog (`PluginHookName` includes
  `before_prompt_build`, `agent_end`, `session_start`, `session_end`,
  `before_reset`; `PluginHookBeforePromptBuildResult` =
  `{ systemPrompt?, prependContext?, appendContext?, toolsAllow?,
  prependSystemContext?, appendSystemContext? }`;
  `PluginHookAgentEndEvent = { messages, success, ... }`;
  `PluginHookSessionEndEvent = { sessionId, reason, nextSessionId,
  messageCount, ... }`; tool execute returns
  `{ content: [{ type: "text", text }], details? }`).
- Web search was unavailable in this environment (no Serper key), so no
  browser sources were consulted; the npm-shipped docs above are the
  authoritative reference and are newer than any cached web copy.

Wire schemas and limits: see `../substrate/CONTRACT.md` (identical contract).
Runtime code is stdlib-only Python plus zero-dependency JS. Never print an
access token. Never disable TLS verification. Never inspect another profile.
