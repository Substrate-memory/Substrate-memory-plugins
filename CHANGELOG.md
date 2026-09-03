# Changelog

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