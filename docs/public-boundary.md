# Public plugin boundary

This repository is the publication-approved, MIT-licensed source-of-truth candidate for the Hermes `substrate_wiki` plugin. Public/release status becomes factual only after GitHub readback. The permanent product/commercial line is in [`BOUNDARY.md`](../BOUNDARY.md).

## Identity preserved

- package/provider: `substrate_wiki`;
- Hermes configuration: `memory.provider: substrate_wiki`;
- environment: `HERMES_API_URL`, `HERMES_API_KEY`;
- profile state: `$HERMES_HOME/substrate_wiki`;
- assets: `substrate_wiki.zip`, `install_hermes_plugin.py`.

## Ownership

This repository owns Hermes lifecycle integration, client transport, local spool/checkpoints, client-side redaction, history replay, build/install tooling, tests, documentation, and releases.

`Substrate-v2` owns server endpoints/authentication, capability behavior, idempotency, limits, persistence, queues, deletion, triage, projection, indexing/search, infrastructure, and deployment. The server must not import plugin Python. This repository must not import server source.

The integration boundary is immutable plugin release assets plus a versioned HTTP capability contract.

## Compatibility

Hermes 0.18.2 is the contract-tested target; full host-lifecycle certification remains pending. Hermes 0.19.0 remains unsupported until the full lifecycle matrix passes. A breaking server change requires a new protocol/schema identifier and concurrent old-version support. No silent auto-update or state rewrite is allowed.

## Secret and data boundary

Secrets come only from the two documented environment variables. Real values may not enter source, artifacts, fixtures, logs, errors, receipts, or support material. Production credentials and private history may never be copied into this repository.

Raw Hermes history stays profile-local until explicit capture or replay. The plugin emits only bounded versioned redacted events to the configured server. Status and receipts remain content-free.

## Legal

- license: MIT;
- copyright: Sightline Technologies Inc;
- scope: this plugin repository only;
- publication: authorized; public only after GitHub readback;
- private Substrate server: not licensed by this repository.
