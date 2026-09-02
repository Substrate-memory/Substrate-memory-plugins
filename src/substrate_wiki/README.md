# Substrate Wiki memory provider

`substrate_wiki` connects Hermes Agent 0.20.x to the hosted Substrate service at
`https://app.trysubstrate.co`. It provides cited recall, automatic future capture, and an
optional, resumable import of eligible prior conversations and explicitly saved memories.

## Install and connect

Use the verified release installer. It installs beneath
`$HERMES_HOME/plugins/substrate_wiki`, activates `memory.provider: substrate_wiki`, and
starts browser/device onboarding automatically:

```bash
python3 install_hermes_plugin.py \
  --archive substrate_wiki.zip \
  --sha256 <published-sha256> \
  --yes --json
```

Hermes 0.20.x discovers user plugins directly beneath `$HERMES_HOME/plugins`; do not add
another directory level or install under `plugins/memory`. The plugin has no third-party
runtime dependencies.

On a desktop the verification page opens automatically. In a headless environment the
installer prints the hosted URL and one-time user code. Sign in with the hosted magic-link
flow and approve the device. The resulting tenant-scoped, revocable credential is stored in
the OS credential store when available, with an owner-private profile file as the fallback.
It is never written to `plugin.yaml`, `config.json`, command arguments, or logs.

The hosted origin is fixed. `HERMES_API_URL` and `HERMES_API_KEY` are not normal setup
options. Legacy shared bearer configuration exists only on the server as a temporary
migration path.

## History consent

After browser approval, the polling installer returns control to the same Hermes
conversation with `action_required: history_consent`. The agent must report that the
connection succeeded and ask whether to upload eligible history. It must not infer or
pre-authorize the answer. Blank or interrupted input leaves consent pending.

This is the only optional step. Declining history upload leaves future capture and recall
enabled. Approval starts exactly one durable profile-scoped job. Its content-free status reports discovered,
uploaded, processed, duplicate, retry, and error counts; disconnects and restarts resume the
same checkpoint and stable batch/event IDs.

Eligible history includes direct/one-to-one conversations and explicit saved memories.
Group chats, cron/webhook sessions, unrelated files, hidden reasoning, secrets, and binary
bodies are excluded. Content is bounded and redacted before local spooling or network
transfer.

Management commands include:

```bash
hermes substrate_wiki onboarding-status --json
hermes substrate_wiki import-status --json
hermes substrate_wiki import-resume --yes --wait --json
hermes substrate_wiki import-cancel --job-id <job-id> --yes --json
```

## Operation and privacy

Live completed user/assistant turns are delivered asynchronously. Failed
transient deliveries stay in a bounded owner-private spool and retry with capped backoff.
Authentication failures trigger automatic reconnect onboarding rather than exposing or
logging credentials. Status, receipts, checkpoints, and diagnostics are content-free.

Visible prompts and assistant output can be sent after redaction. Tool calls, tool results,
system messages, memory-write events, and provider/session metadata are excluded. Redaction is
defense in depth, not proof that arbitrary sensitive prose is absent.

The provider exposes bounded cited wiki search/read/query/ingest/job tools and automatic
memory-card prefetch. It exposes no arbitrary filesystem-write tool.
