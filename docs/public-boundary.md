# Public plugin boundary

This repository is the publication-approved MIT-licensed source candidate for the Hermes
`substrate_wiki` plugin. Public/release status becomes factual only after GitHub readback.

## Identity

- package/provider: `substrate_wiki`;
- Hermes configuration: `memory.provider: substrate_wiki`;
- hosted origin: `https://app.trysubstrate.co`;
- credential source: hosted device onboarding and profile-scoped secure custody;
- profile state: `$HERMES_HOME/substrate_wiki`;
- user-plugin directory: `$HERMES_HOME/plugins/substrate_wiki`;
- assets: `substrate_wiki.zip`, `install_hermes_plugin.py`.

## Ownership

This repository owns Hermes lifecycle integration, transport, credential custody, local
spool/checkpoints, redaction, history consent/replay, build/install tooling, tests,
documentation, and releases. `Substrate-v2` owns hosted OAuth/account routes, tenant
credentials, content APIs, idempotency, persistence, queues, deletion, projection,
indexing/search, infrastructure, and deployment. Neither repository imports the other's
runtime source.

## Compatibility and data boundary

Hermes 0.20.x is the target. The plugin connects only to hosted Substrate and fails closed on
origin or capability mismatch. Credentials may not enter source, artifacts, normal config,
logs, errors, receipts, or support material.

Future capture begins after account connection. Past direct conversations and explicit saved
memories remain local until the user approves historical upload. Group sessions,
cron/webhooks, hidden reasoning, unrelated files, secrets, and binary bodies are excluded.
Status and receipts remain content-free.

## Legal

- license: MIT;
- copyright: Sightline Technologies Inc;
- scope: this plugin repository only;
- publication: authorized; public only after GitHub readback.
