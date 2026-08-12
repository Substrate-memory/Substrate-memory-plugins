# Operation

## Connection and credentials

The plugin connects only to `https://app.trysubstrate.co`. Installation and Hermes' native
`post_setup` hook start device onboarding automatically. Desktop browsers open the hosted
verification page; headless users receive the same URL and one-time code.

Do not configure `HERMES_API_URL` or `HERMES_API_KEY`. The issued tenant-scoped credential
is revocable and stored by native credential custody where available, with an owner-private
profile file fallback. It never belongs in plugin config, logs, or arguments. Optional
non-secret tuning lives at `$HERMES_HOME/substrate_wiki/config.json`.

Use `hermes substrate_wiki onboarding-status --json` to inspect content-free connection and
consent state. Expired or revoked credentials trigger automatic reconnect attempts.

## Optional history import

The consent prompt is only for past history; future capture is enabled whether history is
approved or declined. Approval creates or attaches to one durable import:

```bash
hermes substrate_wiki import-status --json
hermes substrate_wiki import-resume --job-id <job-id> --yes --wait --json
```

The worker is cross-platform and profile-scoped. It resumes the same checkpoint after
process interruption, crash, or reboot. Before upgrading during an active import, record the
content-free job and batch identifiers and resume that job afterward rather than creating a
replacement.

## Tools

- `wiki_search` — lexical search over permitted published pages.
- `wiki_read` — read a returned canonical page path.
- `wiki_query` — cited synthesis, optionally saved by the hosted service.
- `wiki_ingest` — submit text or a public URL to hosted ingestion.
- `wiki_job_status` — inspect asynchronous ingestion.

## Recovery

The installer activates the provider atomically and retains a rollback directory on upgrade.
Automatic retries use bounded backoff. Rolling back plugin code does not delete spool items,
checkpoints, credential custody, or non-secret configuration.
