# Operation

## Environment

Set in the environment source used to launch Hermes:

- `HERMES_API_URL` — Substrate server origin without `/api/v1`.
- `HERMES_API_KEY` — profile-scoped bearer key accepted by that server.

Do not persist the key in plugin files. Optional non-secret settings live at `$HERMES_HOME/substrate_wiki/config.json`.

## Import

Start or attach to the durable import:

```bash
hermes substrate_wiki import-history --yes --wait --json
```

Inspect or resume:

```bash
hermes substrate_wiki import-status --json
hermes substrate_wiki import-resume --job-id <job-id> --yes --wait --json
```

Before upgrading during an active import, record its content-free job and batch identifiers, stop the exact profile-scoped import unit, wait until inactive, install, then resume the same job. Do not cancel and create a replacement job.

## Tools

- `wiki_search` — lexical search over permitted published pages.
- `wiki_read` — read a returned canonical page path.
- `wiki_query` — cited synthesis, optionally saved by the server.
- `wiki_ingest` — submit text or a public URL to server ingestion.
- `wiki_job_status` — inspect asynchronous ingestion.

## Recovery

The installer returns a rollback directory. Rolling plugin code back does not delete spool items, checkpoints, or non-secret configuration. Never resume the v1.2+ import protocol with the OOM-unsafe 1.1 importer.
