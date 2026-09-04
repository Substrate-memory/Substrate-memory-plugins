# Installing the Substrate plugin on OpenClaw

Required behavior for the installing agent. Treat

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

as approval to install and configure the current Substrate plugin for the
active OpenClaw home. Do not ask the user for repository paths, commands,
API keys, or TLS workarounds.

## Steps

1. Confirm prerequisites: `openclaw --version` (needs 2026.4.24+ for the
   `hooks.*` config keys; older hosts omit that block), `node --version`
   (needs 22+), and `python3 --version` (needs 3.11+; override path with
   `SUBSTRATE_PYTHON`).
2. Clone the repo and link the plugin subdirectory (not the repo root):
   `git clone` the repo URL above, then
   `openclaw plugins install --link ./Substrate-memory-plugins/plugins/openclaw --force`.
   A `git:<repo>` source installs the repo root, which is not an OpenClaw
   package — always `--link` the `plugins/openclaw` subdirectory after cloning.
   (Path and `npm-pack:`/`git` sources install the same directory; see the
   OpenClaw "Building plugins" and "Managing plugins" docs shipped in the
   `openclaw` npm package.)
3. Run `openclaw plugins inspect substrate --runtime --json`. Stop without
   changing anything if the plugin is not listed and loaded.
4. Enable the plugin and grant hook access (`allowConversationAccess` is
   required for `before_prompt_build`/`agent_end` message access,
   `allowPromptInjection` for context injection). Merge into the existing
   OpenClaw config (`~/.openclaw/openclaw.json` by default), keeping every
   other key:
   `plugins.entries.substrate = { enabled: true, hooks: {
   allowPromptInjection: true, allowConversationAccess: true }, config: {
   server: { url: "", agentName: "" } } }`.
   Leave `server.url` empty to use the default Substrate origin. Auth uses
   the device-onboarding flow (0600 `.env` + token file); there is no
   `server.apiKey` option — never paste secrets into `openclaw.json`
   (plaintext). Headless hosts may set `SUBSTRATE_API_KEY` env instead.
5. Optionally pre-connect during installation (not required):
   `python3 ./Substrate-memory-plugins/plugins/openclaw/setup.py`.
   If it prints a `verification_uri_complete` URL, show that exact clickable
   URL to the user and ask them to approve it in a browser. Do not expose or
   request an access token. Otherwise onboarding runs automatically on first
   use: when a `verification_uri_complete` link appears (from `memory_search`
   or the first turn), present that exact clickable URL to the user. The user
   approves in their browser; the agent must never approve it for them or ask
   for a pasted key.
6. Restart the gateway (`openclaw gateway restart`), then verify in a new turn
   by calling `memory_search`. Report success only when `memory_search`
   reaches the authenticated Substrate API. Fail closed and report the exact
   safe error class otherwise.

## Verification

- `openclaw plugins inspect substrate --runtime --json` shows the plugin
  loaded with tools `memory_search`, `memory_expand`, `memory_evidence`.
- A new turn carries the static Substrate memory guidance and, when the
  server has relevant facts, a bounded `<memory-context>` block.
- `memory_search` returns bounded JSON (or the onboarding link on first use).

## Rollback

```sh
openclaw plugins disable substrate
```

then remove the `plugins.entries.substrate` block from the OpenClaw config
and restart the gateway. Delete `<openclaw-home>/substrate/` to forget the
stored credential and onboarding state.

## Rules

TLS verification must remain enabled. The plugin supplements the host trust
store with the unmodified public ISRG Root X1 and X2 certificates in
`substrate_core/ca/` for current Let's Encrypt chains. It never trusts a
replacement leaf certificate or a private certificate. It never inspects
another profile: only `SUBSTRATE_HOME` / `OPENCLAW_STATE_DIR` / the installed
home / `~/.openclaw`.
