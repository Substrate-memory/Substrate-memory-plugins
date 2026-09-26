# Changelog

## 0.8.0

Hermes install and sign-in fixed end to end.

- **Sign-in no longer fails with `invalid_response`.** v0.7.0 rejected the server's valid approval link whenever the plugin reached Substrate by an address other than `https://app.trysubstrate.co` (an old `SUBSTRATE_API_URL`, a stored address from an earlier install, a proxy or tailnet name). The server builds the link on its public address, so the whole login failed although the server had issued a code. The plugin now accepts the link when it is HTTPS (plain HTTP only for a loopback host), points at `/oauth/device`, and carries this grant's code, and tells the user which old setting to remove.
- **Connection check and memory tools match the current server.** The server answers `memory_search`, `memory_expand`, `memory_evidence`, `memory_remember` and `memory_forget` with `contract_version: 1` and offers `memory_shares` only when sharing is wired. v0.7.0 demanded `2` and all twelve tools, so even an approved login ended in `authenticated_health_check_failed` and memory calls in `invalid_response`. The plugin now accepts `1` or `2` for those tools and does not require `memory_shares` (it never calls it). `docs/mcp-contract.md` says so.
- **One-command install from the repository URL.** The repository root is now a Hermes plugin (`plugin.yaml` + `__init__.py`) that loads `plugins/substrate-hermes`: `hermes plugins install https://github.com/Substrate-memory/Substrate-memory-plugins --enable`. No more "not a valid plugin" warning. A root `onboard.py` gives one login path for both layouts.
- **Passes the Hermes install scan without an override.** Hermes 0.21.4 `plugin_guard` verdict for the repository root moved from CAUTION (89 findings, 1 high) to SAFE (0 high). The dynamic `__import__("os")` in `scripts/check_public_hygiene.py` is a normal import; docs and CI use `uv sync --frozen` + `.venv/bin/...` instead of `uv run`. Remaining medium findings are subprocess calls in tests and the optional Secret Service lookup (`secret-tool`).
- **Login in chat.** When the profile is not connected, the plugin starts the sign-in itself and gives the agent the link and code to show in chat. It finishes by itself after approval and the next turn says **Connected to Substrate as you@example.com.** (your account) (the server adds the account from its next release; older servers give **Connected to Substrate.**). Notices show the real `onboard.py` path, never `<plugin-dir>`.
- **Plain errors.** Every sign-in failure has a sentence saying what happened and what to do (`message`), next to the `error_class` code.
- **Universal version policy.** No package refuses a host version. Outside the tested range the user gets one friendly note (*Tested on Hermes 0.21.0-0.21.x; you are on X. It should work; tell us if not.*). The Hermes plugin shows it by itself; for the other hosts the installing agent follows the same rule. See `COMPATIBILITY.md#version-policy`.
- Verified end to end on a real Hermes 0.21.4 in an isolated profile: install by URL → enable → approval link → approve → memory write and recall.

## 0.7.0

One connection experience for every agent:

- The user pastes one request into their agent (`Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins`). The agent identifies its host and installs the right package: Claude Cowork and Claude Code → `plugins/substrate-claude`; ChatGPT Work, the Codex app and Codex CLI → `plugins/substrate-codex`; Hermes → `plugins/substrate-hermes`; any other MCP-capable agent → the thin `plugins/substrate-mcp` fallback, only when no package fits.
- Same flow everywhere: **Install → Sign in → Review → Approve connection → Connected to Substrate.** Success is reported only after an authenticated `memory_search`. No API keys, tokens, or secrets are ever requested.
- All hosts get Hermes-level features through one server-side MCP contract (v2): recall injected before every turn, automatic capture of every completed turn including bounded redacted tool calls and results, session boundaries, subagent capture, durable delivery, explicit remember/forget, and evidence.
- Durable delivery per host: Hermes keeps its profile-local write-ahead spool; Claude and Codex treat the host transcript as the spool — the server reports missing turns and the plugin sync command re-imports them with deterministic ids, so nothing is stored twice.
- Import past conversations is asked once, after connection: raw, redacted turns from that host's own transcripts, imported after the user confirms the session list. Nothing is written before confirmation; secrets are redacted client- and server-side.
- Known limits, stated honestly: Codex has no session-end hook (the server seals idle sessions after 30 minutes); Claude's SessionStart hook at launch runs before MCP connects; if a Cowork cloud session ends while Substrate is unreachable and is never reopened, its last turn is not recovered; plugin hooks in Codex must be trusted once via `/hooks`; Cowork needs a new session after install.
- Legacy: the August 2026 API-key plugins stay deprecated and their spools are not migrated; the `v0.5.0` and `v0.6.0` tags remain for rollback; the server keeps the Hermes `/api/v1` wire during the rollout.


## 0.6.0

Unified onboarding and install parity:

- Use one Substrate connection journey for Hermes and MCP: sign in, review the
  connection name, permissions and proof details, approve, then verify from the agent.
- Centralize installation and recovery guidance in `docs/installation.md`.
- Distinguish browser consent (**Connection approved**) from a verified client
  connection (**Connected to Substrate.**). No manual credential setup is added.
- Preserve the golden Hermes runtime and the thin remote MCP manifest.
- Add host mechanics, a self-check protocol, and Cowork's required new-session
  step before MCP tools appear.
- Ask once, after verification, whether to import past conversations; the agent
  extracts durable facts from history it can read and writes confirmed items with
  `memory_remember`. Same step for Hermes and MCP hosts.
- Correct `SECURITY.md`: the MCP endpoint and browser OAuth backend are deployed.
- Document Claude Code and Codex migration from deprecated August 2026 API-key
  plugins (`substrate_capture` and local spool paths); legacy spool data is not
  migrated and users never set `SUBSTRATE_API_KEY` by hand.

Client strategy:

- Replace the five host adapters with one thin Cowork-compatible
  `plugins/substrate-mcp` package. It contains a remote HTTP MCP manifest at
  `https://app.trysubstrate.co/mcp`, a generic best-effort usage skill, and
  installation docs only. It has no local MCP server, hooks, or automatic
  full-transcript guarantee.
- Preserve the complete golden Hermes `plugins/substrate` tree from v0.5.0,
  including its published v0.5.0 installation pin during the rollout.
- Ship deterministic Hermes and thin-MCP archives. The remote MCP endpoint and
  browser OAuth backend are deployed; a real Cowork installation was verified with
  an authenticated `memory_search` before publication.

## 0.5.0

- Durable v5 Hermes `substrate` plugin at content version `0.4.0`
  (`substrate.zip` rebuilt from the new `src/`-layout tree with a
  namespace-loader root scaffold): write-ahead spool with strict ACK
  retirement, `memory_remember`/`memory_forget` write tools, true session
  boundaries (`on_session_finalize`), session rotation
  (`on_session_reset`), parent-routed subagent capture
  (`subagent_start`/`subagent_stop`), and install-friendly device login
  (`onboard.py start`/`status`/`poll --json` plus the `setup.py` compat
  entry point) against the canonical `https://app.trysubstrate.co` origin.
  Pre-login capture stays pending (never quarantined) until browser
  approval; the tenant-scoped key is stored owner-only in the active
  profile. No API key is requested or pasted at any step.
- Keep the five host adapters (`claude-code`, `claude-cowork`, `codex`,
  `grok-bot`, `openclaw`) byte-identical to the `v0.4.0` release assets.
- Preserve the `v0.3.0` and `v0.4.0` tags and assets for rollback.

## 0.4.0

- First multi-host release of the plugin set (release version `0.4.0`, read from
  the root `VERSION` file): one immutable tag publishes six deterministic
  archives (`substrate.zip`, `claude-code.zip`, `claude-cowork.zip`, `codex.zip`,
  `grok-bot.zip`, `openclaw.zip`) plus a single `SHA256SUMS`.
- Ship the five new host adapters at manifest version `0.4.0`: `plugins/claude-code/`
  (Claude Code), `plugins/claude-cowork/` (Claude Cowork), `plugins/codex/`
  (Codex CLI), `plugins/grok-bot/` (Grok Build CLI / MCP-capable harness), and
  `plugins/openclaw/` (OpenClaw).
- Keep the Hermes reference plugin (`plugins/substrate/`) frozen at content
  version `0.3.0`: its bytes and its `substrate.zip` archive are identical to
  the `v0.3.0` release.
- Include the two live fixes found against checkouts without explicit home
  variables: `78c1549` credential-location parity (persist the token file and
  read the profile `.env` fallback so first-use onboarding does not loop) and
  `2b23b52` home resolution (route all vendored home resolution through the
  per-host `hosthome` modules so state lands in the host home, never the repo
  checkout).

## Unreleased

- Add self-contained host adapters with tool, redaction, envelope, onboarding,
  and fail-closed parity to the Hermes reference plugin (still `0.3.0`, still
  frozen under `plugins/substrate/`): `plugins/claude-code/`,
  `plugins/claude-cowork/`, `plugins/codex/`, `plugins/grok-bot/`, and
  `plugins/openclaw/`, each with its native manifest, `README.md`, `INSTALL.md`,
  and offline test file.
- Merge the Claude marketplace entries (`substrate-memory` for Claude Code,
  `substrate-cowork` for Cowork) into the root `.claude-plugin/marketplace.json`
  and add the root `.agents/plugins/marketplace.json` entry for the Codex
  Git-URL marketplace flow.
- Isolate the per-host test modules with `tests/_hostload.py` plus
  `tests/conftest.py` so the full suite is green in any collection order.
- Cover the new plugins in CI (full pytest suite, `node --check` for the OpenClaw
  adapter, JSON-parse validation of every manifest).

## 0.3.0

- First full release of the `substrate` plugin for Hermes 0.21.x: one-prompt install
  from the `v0.3.0` tag, self-contained RFC 8628 device onboarding with agent display
  names, `memory_search`/`memory_expand`/`memory_evidence`, live session completion
  markers, pinned public ISRG trust roots, and health-gated legacy cutover.
- Remove the legacy `substrate_wiki` provider, its installer/builder, migration
  benchmarks, release assets, and publication-policy machinery. The repository now
  contains only the current plugin, its tests, and a deterministic release builder.
- Replace the closed-inventory publication scanner with a dependency-free public
  hygiene check (`scripts/check_public_hygiene.py`).

- Add the current `substrate` plugin (`plugins/substrate`, now version 0.3.0) for Hermes 0.21.x: one-prompt `hermes plugins install` of the subdirectory, self-contained RFC 8628 device onboarding on first use (browser approval link, background polling, key stored privately in the active profile's `.env`), `memory_search`/`memory_expand`/`memory_evidence` tools, verified TLS with bundled public ISRG root anchors, and health-gated legacy cutover. The legacy `substrate_wiki` 2.0.x provider below remains only for existing Hermes 0.20.x installations.
- Let hosted onboarding validate only the history replay contract, so a setup-and-upload MVP does not need to advertise entity retrieval features.
- Add an explicit `SUBSTRATE_WIKI_ORIGIN` override for isolated v5 import tests; the hosted origin remains the default.

## 2.0.5

- Make browser approval completion return an explicit history-consent action to the installing agent.
- Keep blank or interrupted consent prompts pending instead of treating them as refusal.
- Always print the complete browser approval URL while the polling installer waits.
- Support the Substrate v5 history-only capability handshake and test origin.
- Rename the public source repository to `Substrate-memory-plugins`.

## 2.0.4

- Upload only completed user/assistant text; exclude tool calls, tool results, system messages,
  memory writes, session boundaries, provider scope, hashes, retention metadata, and duplicate
  envelope fields.
- Batch historical dialogue up to the request-size limit instead of posting one message per
  request, while retaining deterministic IDs and resumable checkpoints.

## 2.0.3

- Keep approved device polling active across transient hosted transport and edge failures.
- Use a 60-second OAuth request timeout and fixed content-free failure categories.

## 2.0.2

- Retry transient post-approval capability checks across one hosted tenant cold start.
- Preserve a content-free capability failure category without storing an unvalidated credential.

## 2.0.1

- Present the complete one-click device authorization URL in agent and headless prompts.
- Require a server-supplied complete URL to carry the issued one-time code; construct the correct hosted URL when that optional field is absent.

## 2.0.0

- Targets Hermes 0.20.x user-plugin discovery and native setup hooks.
- Installs and activates under `$HERMES_HOME/plugins/substrate_wiki`.
- Adds hosted RFC 8628 onboarding, tenant credential custody, revocation repair, and fixed-origin enforcement.
- Makes prior-history upload optional while keeping future capture automatic.
- Adds durable cross-platform history supervision, stable replay IDs, resumable checkpoints, and content-free progress.

## 1.5.0 — pending standalone source candidate

- When released and cut over with the companion Substrate-v2 change, establishes this public repository as the independently built plugin source.
- Preserves provider, configuration, state, and server protocol identities.
- Separates repository-native release assets from immutable imported releases.
- Moves privacy-safe migration benchmarks and plugin-owned verification out of Substrate-v2.
- Declares the permanent MIT/open versus held hosted-service boundary and DCO contribution terms.
- Explicitly records that local/no-server runtime and policy authorization are not implemented in this candidate.

All notable changes to the public `substrate_wiki` plugin are recorded here.

## 1.4.1 — imported release

- Preserves the existing OOM-safe `stream-v2` replay protocol and durable checkpoint behavior.
- Adds graceful import-service pause on systemd stop.
- Retains Hermes 0.18.2 compatibility and `entity-quality-v2` bounded memory-card recall.
- Imported byte-for-byte from Substrate-v2. Archive SHA-256: `877ccf9b0212792b699d9c98912a26980675a6050df3bd319e927639e3d901f1`.
- Original source commit: `a3953b0512bbb84fb62b48a75bab04cbcb845c78`.

Earlier immutable artifacts remain under `legacy-assets/` with their original provenance.