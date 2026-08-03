# Hermes migration baseline and closed-beta budget (SUB-75)

This baseline measures the unchanged `stream-v2` transfer path and a deterministic,
content-free downstream model. It is evidence for later protocol and tuning work; it
is **not** a protocol change, a production tuning claim, or hosted-provider evidence.

## Reproduce

Python 3.12 and the repository's frozen development environment are required.

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev python scripts/benchmark_migration.py \
  --manifest benchmarks/hermes-migration-manifest.json \
  --profile ci \
  --output benchmarks/evidence/hermes-migration-baseline.json
uv run --frozen --extra dev python scripts/verify_migration_baseline.py --write-budget
uv run --frozen --extra dev python scripts/verify_migration_baseline.py
uv run --frozen --extra dev pytest -q tests/test_migration_baseline.py
```

The `contract` profile is the bounded CI canary and runs through the canonical
`import-memory` pytest shard. The `ci` and `release-candidate` profiles currently
select all six deterministic fixture classes and are suitable for an independently
provisioned preview/reviewer host. No command opens a hosted connection.

Canonical inputs and retained evidence:

- workload: `benchmarks/hermes-migration-manifest.json`;
- receipt contract: `benchmarks/hermes-migration-receipt.schema.json`;
- retained canonical aggregate: `benchmarks/evidence/hermes-migration-baseline.json`;
- measured budget: `benchmarks/hermes-migration-budget.json`.

Fixture generation runs in a child process before each measured run. SQLite and
JSONL fixtures are deterministic for a manifest revision. The official-export case
uses the bounded JSONL shape produced by the supported export adapter; it does not
invoke a local Hermes runtime export API. The current-production case fixes the
observed retained-window cardinality (1,212), while the large case fixes 2,048
windows. The oversized case is 2 MiB and therefore exercises stream-v2 fragmentation
above the 262,144-byte event ceiling.

Each receipt binds the exact manifest and benchmark implementation with SHA-256
digests. The verifier recomputes both before accepting schema, matrix, or budget
evidence; the budget then binds the canonical receipt bytes.

## What each measurement means

| Field | Boundary / method |
| --- | --- |
| `discovery` | Production SQLite/JSONL source discovery and content-free checkpoint inventory. |
| `source_reading` | Time spent advancing the production bounded source iterator. |
| `normalization_redaction` | From source yield to sink entry: production event construction, normalization, fragmentation and redaction. |
| `request_encoding` | Deterministic JSON wire serialization at the local sink boundary. |
| `network_wait` | Fixed local sleep only; explicitly simulated and opens no network connection. |
| `server_persistence` | Durable content-free SQLite acknowledgement commit. |
| `queue_delay` | Enqueue-to-worker-start delay; summed worker time may exceed wall time. |
| `provider_extraction` | Fixed deterministic provider double latency, including configured synthetic retries. |
| `entity_resolution` | Content-free deterministic digest work standing in for resolution. |
| `projection_indexing` | Durable content-free SQLite projection commit. |
| `summary_reduction` | Periodic and terminal deterministic reduction digest. |

`transferred` is reached only after the production importer has received durable
acknowledgements for every event. `first_usable` is the first completed synthetic
projection and may precede transfer completion because local workers consume the
queue concurrently. `fully_ready` is reached only after transfer and every queued
projection completes. Validation requires both earlier terminals to be strictly before
`fully_ready` and requires `transferred` and `first_usable` to remain distinct; their
relative ordering is measured rather than assumed. These labels are never aliases.

Rates use aggregate wall boundaries: events/s and MiB/s use `transferred`; windows/minute
uses `fully_ready`. CPU is process CPU time. Peak RSS is the maximum current RSS sampled
from `/proc/self/status` (with `/proc/self/statm` fallback) every 5 ms inside a fresh
per-case worker, excluding fixture generation and inherited `ru_maxrss`. Phase values are aggregate phase time, not a
partition of wall time; concurrent phase sums and queue age sums can exceed wall time.

## Provider model and limits of the evidence

The representative provider mode is an honest deterministic simulation: 5 ms of
observed local latency per extraction request, one retry opportunity every 257th
request, a modeled quota of 60 requests/minute, and modeled cost of $0.00025/request.
The receipt records both actually slept provider time and quota-equivalent time. It
never claims the accelerated local sleep is provider wall time.

The harness installs socket-construction denial in the orchestrator, every fixture child,
and every measured worker. A network attempt therefore fails the run rather than silently changing the
`hosted_calls: 0` claim. `worker_threads_used` and `max_in_flight` are measured at the
processing seam; the verifier requires retained evidence to demonstrate each configured
concurrency from one through four.

The harness uses the production importer, checkpoint, redaction, fragmentation,
SQLite reader and JSONL reader. Server persistence and all phases after persistence
are benchmark doubles. It therefore establishes a reproducible bottleneck baseline
and target budget, but does not predict hosted-provider variance, production disk
latency, model quality, Azure worker contention, or protocol speedups. No prompts,
transcripts, source payloads, credentials, event identifiers, or memory material are
written to traces or receipts; only aggregate counts, timings, sizes and synthetic
fixture digests are retained.

## Measured result and closed-beta budget

The retained receipt is the numeric source of truth. The budget file is derived from
that receipt with these explicit policies:

- integrity ratio 1.0, zero redaction/projection/terminal failures;
- one measured duplicate replay acknowledgement and zero duplicate side effects per run;
- peak RSS remains below the unchanged 256 MiB importer ceiling;
- per-case transfer, first-usable, fully-ready and CPU ceilings use the worst measured
  concurrency plus a 50% tolerance and a 2-second host-jitter allowance;
- provider quota-equivalent seconds, deterministic retry count and cost are fixed to
  the workload's measured/modelled maximum, not traded for speed;
- no quality or integrity constraint may be relaxed to satisfy a latency ceiling.

Numeric findings and the dominant phases are summarized below after the retained
receipt generated on the candidate host; reviewers should recompute them directly
from the JSON rather than relying only on rounded prose.

<!-- SUB75_NUMERIC_SUMMARY -->

| Case | Transfer max (s) | First usable max (s) | Fully ready max (s) | Peak RSS max (MiB) | CPU max (s) | Integrity | Dominant aggregate phase |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `current-production-jsonl` | 52.509 | 0.275 | 52.511 | 36.5 | 3.007 | 4/4 | `network_wait` |
| `large-sqlite` | 69.322 | 0.448 | 69.324 | 39.2 | 5.316 | 4/4 | `network_wait` |
| `official-export-jsonl` | 2.307 | 0.359 | 2.311 | 32.7 | 0.289 | 4/4 | `network_wait` |
| `oversized-message-jsonl` | 2.457 | 2.455 | 2.458 | 36.8 | 1.867 | 4/4 | `source_reading` |
| `small-sqlite` | 0.343 | 0.200 | 0.346 | 32.6 | 0.028 | 4/4 | `network_wait` |
| `sqlite-adapter` | 2.322 | 0.752 | 2.325 | 33.2 | 0.172 | 4/4 | `network_wait` |

All 24 runs completed with `hosted_calls: 0`, exactly one duplicate acknowledgement,
zero duplicate side effects, zero redaction/projection/terminal failures, and measured
worker concurrency matching each configured level from one through four.

## Implications for SUB-76 and SUB-77

The baseline keeps protocol and tuning deliberately unchanged. A follow-up must
preserve all receipt integrity, quality, redaction, RSS, CPU, quota and cost gates,
then compare the three lifecycle boundaries independently. Transfer work belongs in
SUB-76 only if source/normalization/encoding/network/persistence evidence dominates.
Bounded worker/backpressure work belongs in SUB-77 only if queue/provider/resolution/
projection/reduction evidence dominates. A faster `transferred` result cannot be
reported as faster `first_usable` or `fully_ready` without separately measured proof.
