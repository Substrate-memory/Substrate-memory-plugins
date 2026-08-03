# Architecture

```text
Hermes lifecycle / tools
        |
        v
substrate_wiki provider
  - bounded prefetch cache
  - capture + redaction
  - durable spool/checkpoint
  - verified HTTPS client
        |
        v
versioned Substrate HTTP capabilities
```

## Recall

Hermes queues retrieval asynchronously. `prefetch()` reads only a bounded session/query cache and never blocks the model turn on network I/O. Accepted automatic-recall results are bounded cited `memory_card` values from canonical entity pages.

## Capture

Completed turns, pre-compression boundaries, memory writes, and session boundaries become versioned events. Credential-shaped values are redacted before admission to the durable spool. Delivery uses deterministic event IDs, idempotency keys, bounded retries, and durable acknowledgement checkpoints.

## History replay

The importer reads the active Hermes profile's canonical SQLite or supported export source through bounded cursors. It resumes exact acknowledged progress after interruption and sends a content-free completion boundary. It refuses servers without `stream-v2`.

## Local state

All writable plugin state is rooted beneath the active `$HERMES_HOME/substrate_wiki` directory. No state is shared across profiles. Spool files and checkpoints are owner-private on POSIX systems.

## Dependency boundary

The runtime uses the Python standard library and Hermes host interfaces. The Substrate server never imports plugin Python; this repository never imports Substrate-v2 server code.
