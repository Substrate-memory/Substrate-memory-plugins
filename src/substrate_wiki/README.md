# Substrate Wiki memory provider

This external memory plugin connects Hermes Agent v0.18.2 to one Substrate cited Markdown wiki. Published conversation memory is materialized as canonical entity wiki pages; there is no separate representation search store. Its manifest identity is `substrate_wiki`, matching the Python package and installation directory.

## Installation for Hermes v0.18.2

Hermes v0.18.2 discovers plugins directly beneath `$HERMES_HOME/plugins`. Install the complete package at exactly:

```text
$HERMES_HOME/plugins/substrate_wiki/
```

The installed directory contains the provider, streaming adapters, content-free checkpoint store, managed worker, service supervisor, manifest, and this README. Archive installations also include generated `PROVENANCE.json` release metadata. Do not add another `substrate_wiki` directory level, and do not install it under `plugins/memory`.

Use the release installer rather than replacing this directory manually:

After downloading the authenticated release archive and standalone installer into a
private temporary directory, run:

```bash
python3 install_hermes_plugin.py \
  --archive substrate_wiki.zip \
  --sha256 <published-sha256> \
  --install-import-service \
  --yes --json
```

The installer validates the archive checksum, provenance, declared target Hermes version, and every packaged file hash. Confirm separately that the running Hermes version is exactly 0.18.2 before installation. It upgrades the existing direct plugin atomically and retains a rollback copy; if the plugin is unexpectedly absent it installs the same package. Hermes discovers the module-level `register(ctx)` function and `plugin.yaml` metadata. If Hermes presents a provider selector, choose `substrate_wiki`.

When upgrading while a durable import is active, first record its content-free `job_id` and `batch_id` with `import-status`. Stop the exact `substrate-wiki-import-<home-hash>@<job-id>.service` unit and wait until it is inactive; SIGTERM makes the worker pause after the current acknowledgement is durably checkpointed. Do not use `import-cancel`, which changes job state, and do not run a fresh `import-history` after the upgrade. Install v1.4, then continue exactly the recorded job:

```bash
hermes substrate_wiki import-resume --job-id <same-job-id> --yes --wait --json
```

Stopping this separate import unit does not stop or restart the Hermes gateway.

Version 1.5.0 keeps the OOM-safe v1.2 replay protocol and the same v2 durable checkpoint. It targets Hermes 0.18.2 exactly and adds no protocol, event-envelope, checkpoint, service-unit, or server-capability requirement beyond v1.4.0. Automatic recall requires both the server's backward-compatible `entity-wiki-v1` surface and its `entity-quality-v2` canonical-quality capability, and consumes only bounded `memory_card` results. It does not create a second import job, retransmit acknowledged history, install Honcho, or use another memory provider:

```bash
hermes substrate_wiki import-history --yes --wait --json
```

The command creates or attaches to the existing durable job, starts the profile-scoped import service, and monitors it when `--wait` is present. Disconnecting the monitor does not stop the worker. The worker streams the active profile's canonical `state.db` through read-only cursors, checkpoints every acknowledged event, resumes after crashes or reboots, and sends a content-free completion boundary instead of duplicating each transcript. It refuses to run unless Azure advertises `stream-v2`; there is no unsafe legacy fallback. No gateway restart is required.

The v1.5.0 worker handles a systemd stop as a graceful pause: it finishes and checkpoints the current acknowledgement before exiting, without cancelling the job or changing its batch ID. Starting the same unit or using `import-resume` continues that checkpoint. Deterministic server deduplication remains the safety net if an older worker is interrupted during the one-time upgrade.

Content-free management commands are:

```bash
hermes substrate_wiki import-status --json
hermes substrate_wiki import-resume --yes --wait --json
hermes substrate_wiki import-cancel --job-id <job-id> --yes --json
```

## Configuration

Set these variables in the environment of the Hermes process before it starts:

- `HERMES_API_URL`: Substrate service base URL, without `/api/v1`.
- `HERMES_API_KEY`: bearer key accepted by the service's Hermes endpoints.

Keep these variables in the environment source already used to launch Hermes. Do not install Honcho or another memory provider, and do not put the API key in `plugin.yaml` or the plugin's `config.json`.

The key remains environment-backed and is never copied to the provider's JSON state. Provider state is created only beneath the `hermes_home` supplied by Hermes, in `substrate_wiki/`. Optional non-secret tuning in `config.json` supports:

- `spool_max_items`: maximum number of locally spooled events.
- `spool_max_bytes`: maximum total size of the local spool.
- `prefetch_ttl_seconds`: lifetime of prefetched search results. Hermes receives an exact-query hit when available, otherwise the latest cited result warmed for the same session is used on the next turn.

## Tools

- `wiki_search`: lexical search over maintained pages. Results include a canonical repository-relative `path` and a legacy `slug`.
- `wiki_read`: read one page using the returned repository-relative path (for example `notes/hermes.md`); legacy slugs such as `notes/hermes` remain accepted.
- `wiki_query`: request a cited synthesis, optionally saved by the service.
- `wiki_ingest`: submit text or a public URL to the service's validated ingestion workflow.
- `wiki_job_status`: inspect an asynchronous job.

No arbitrary filesystem-write tool is exposed.

Automatic memory prefetch requires `entity-wiki-v1` plus `entity-quality-v2` and searches only published canonical entity pages through `/api/v1/hermes/memory/search`. The plugin admits the server's bounded memory card rather than full page bodies or provenance ledgers. The legacy representation-context API is only a server compatibility alias over those same pages. Explicit wiki tools continue to access all permitted entity, topic, source, and synthesis pages. Neither path can search raw events, transcript chunks, private stubs, temporary summaries, or unpublished claims.

## Session data and privacy

The plugin asynchronously sends bounded completed-turn chunks, including optional OpenAI-style `messages` supplied by Hermes. Those messages can contain prompts, assistant output, tool calls, and tool results. Session completion is content-free in v1.2; pre-compression and built-in memory-write capture remain supported. Treat the configured Substrate service as a recipient of visible content and review its access and retention controls before enabling the plugin.

Authorization-like fields, configured environment secret values, common tokens, passwords, and private keys are redacted before an event is queued, persisted, or transmitted. Redaction is a defense in depth measure, not a guarantee that arbitrary sensitive prose will be detected. Do not place secrets in prompts or tool output when avoidable.

Captured and failed deliveries are written to a bounded durable spool beneath `hermes_home/substrate_wiki/spool` and replayed oldest-first with an idempotency key. Transient transport and server failures use capped exponential backoff with jitter; rate limits honor bounded `Retry-After` hints, and authentication failures open a longer retry delay rather than hammering the endpoint. A successful delivery resets the backoff. Protect `$HERMES_HOME` with user-only filesystem permissions and include the spool in local data-handling and deletion procedures. Events declare a 90-day raw-event retention policy; the Substrate service enforces deletion. Curated wiki pages are not automatically deleted.

Events declare a 90-day raw-event retention policy. The configured Substrate server owns enforcement, publication, review, and durable-page retention; verify that server's policy before enabling capture.

`sync_turn` durably admits the redacted event before waking the asynchronous sender and does not wait for network I/O. Shutdown waits at most five seconds, interrupts retry cooldowns, and leaves unfinished events durable for the next initialization.

## Rollback

The installer reports the exact `substrate_wiki.rollback-<timestamp>` directory and prior service-unit rollback it created. Rolling back plugin code does not delete queued events, content-free checkpoints, or configuration under `$HERMES_HOME/substrate_wiki`. Never resume history import with a rolled-back v1.1 plugin because that importer is OOM-unsafe.

## Building and verifying the release archive

From the repository root:

```bash
python scripts/build_plugin.py
python scripts/build_plugin.py --check
```

The builder accepts only the documented release files, excludes generated Python bytecode, rejects unknown release files, and writes a deterministic `dist/substrate_wiki.zip`. It generates `PROVENANCE.json` with the provider and version identities, target Hermes version, build-format version, source commit, and a SHA-256 digest for every packaged source file. Freeze and commit the plugin source first, then set `HERMES_PLUGIN_SOURCE_COMMIT` to that full 40-character commit SHA for the release build. The release command deliberately refuses the `unknown` marker; only direct test helpers may use it for local canonical-byte tests. Working-tree dirty state is deliberately not embedded, while the builder proves that every packaged plugin byte matches the pinned commit. The check command takes its expected source commit from the archive's validated provenance rather than the environment, so a SHA-stamped release verifies deterministically without release environment variables.
