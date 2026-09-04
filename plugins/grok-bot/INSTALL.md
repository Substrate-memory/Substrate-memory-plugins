# Installing the Substrate memory plug-in on Grok Bot

Follow these steps exactly when a user says: "Install the memory plug-in
at `Substrate-memory/Substrate-memory-plugins` on github.com". Ask for
nothing (no paths, commands, keys, or TLS workarounds).

## VERIFIED path (primary — tested offline, no Grok host required)

This is the supported install path. It was verified by offline tests in
this repo (`tests/test_grok_bot_plugin.py`, including the MCP stdio
round-trip test for `server.py`). It needs only `git`, `sh`, and
`python3` — no `grok` CLI.

### 1. Clone the repository

```sh
git ls-remote https://github.com/Substrate-memory/Substrate-memory-plugins.git refs/tags/v0.4.0
```

```sh
git clone --branch v0.4.0 --depth 1 https://github.com/Substrate-memory/Substrate-memory-plugins.git
SUBSTRATE_REF=v0.4.0 sh plugins/grok-bot/install.sh
```

(`install.sh` installs a copy of the repo into
`$GROK_HOME/plugins/substrate-memory` at `$SUBSTRATE_REF` — that second clone
is the install, not a redundant download — then registers the MCP server in
`$GROK_HOME/mcp.json`. The default ref is `v0.4.0`, matching the pin above;
export `SUBSTRATE_REF` explicitly when installing any other ref.
`install.sh` also respects `SUBSTRATE_REPO_URL` and `GROK_HOME`/`SUBSTRATE_HOME`
(default home is `~/.grok`) and is idempotent: safe to re-run.)
Rollback: delete `$GROK_HOME/plugins/substrate-memory` and remove the
`substrate-memory` entry from `$GROK_HOME/mcp.json`.

### 2. Run the MCP stdio server (verified interface)

`server.py` is a stdlib-only MCP server over stdio exposing exactly the
three memory tools (`memory_search`, `memory_expand`, `memory_evidence`).
Register it with any MCP-capable client:

```sh
python3 plugins/grok-bot/server.py
```

`install.sh` registers it automatically as
`mcpServers: {substrate-memory: {command: python3, args: [server.py]}}`
in `$GROK_HOME/mcp.json`.

### 3. Register the function manifest (verified file)

`grok-tools.json` is a valid OpenAI-compatible function-calling manifest
(tool names, descriptions, and JSON schemas match the Hermes reference).
For an xAI-API harness, register `grok-tools.json` as function tools and
call `bridge.py` around turns (pre-turn context, completed-turn capture,
session markers) per `instructions.md`.

### 4. Connect (device onboarding, no pasted keys)

Run once, or let the first `memory_search` do it automatically:

```sh
python3 "$GROK_HOME/plugins/substrate-memory/plugins/grok-bot/setup.py"
```

(`$GROK_HOME` defaults to `~/.grok`; substitute your custom `GROK_HOME` if you
set one during step 1.)

If it prints `verification_uri_complete`, show that exact URL to the user
and ask them to open it in a browser, sign in by email, and approve.
Never ask the user to paste a key into chat. Never print an access token.

### 5. Verify

1. Append `instructions.md` to the agent instructions (static prompt).
2. In a new turn, call `memory_search`. Report success only when it reaches
   the authenticated Substrate API (a real result, not
   `authorization_required`).
3. Confirm a completed turn is captured without blocking the reply.

Do not disable TLS verification, install a private certificate, or inspect
another profile's home. The plugin supplements system trust only with the
bundled public ISRG roots in `substrate_core/ca/`.

## UNVERIFIED section — Grok Build CLI commands (unverified against a real Grok Build host)

The commands below were NOT tested: no `grok` binary was available in the
build environment, so none of these commands was run or verified against a
real Grok Build host. Confirm each one with `grok --help` on a Grok host
before relying on it. Prefer the VERIFIED path above.

- The directory ships repo-local convention files (`plugin.json`,
  `.mcp.json`, `hooks/hooks.json`, `skills/substrate-memory/SKILL.md`,
  `commands/memory.md`) that mirror the Grok Build plugin layout. They are
  consumed by `install.sh` and these docs — they are not a published xAI
  manifest format, and no host tool was observed consuming them here.
- Aspirational (unverified against a real Grok Build host):
  `grok plugin install <path> --trust`, `grok mcp add substrate-memory
  -- python3 <path>/server.py`, `grok mcp list`, `grok inspect`, and `/mcps`
  in the TUI.
