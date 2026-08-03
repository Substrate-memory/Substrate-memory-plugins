# Hermes Substrate Wiki

`substrate_wiki` is the official Hermes integration for Substrate: **the memory that decides what your agents are allowed to do**. This release connects one Hermes profile to a Substrate server through a bounded, cited memory interface and durable, redacted history capture.

The broader product uses memory to ground deterministic agent permissions. **Version 1.5.0 is the memory-provider integration only:** it does not authorize actions, include the local runtime or policy compiler, or support a no-server mode. Those gaps are explicit rather than silently presented as hosted-only features.

**This repository is the source of truth for the plugin.** `Substrate-v2` owns the server and HTTP contract; it does not own or embed plugin source.

## Status

| Surface | Supported |
|---|---|
| Plugin | `substrate_wiki` 1.5.0 |
| Hermes host | Targets 0.18.2; standalone contract suite verified |
| Server contract | `stream-v2`, `entity-wiki-v1`, `entity-quality-v2` |
| Runtime dependencies | Python standard library only |
| License | MIT |

Hermes 0.19.0 is unsupported. The 0.18.2 interface is contract-tested; full host-lifecycle certification is still pending. There is no silent auto-update or configuration rewrite.

## Install the verified release

Download the two assets from [release v1.5.0](https://github.com/Substrate-memory/hermes-substrate-wiki/releases/tag/v1.5.0), then verify and install using the SHA-256 values published with that release:

```bash
curl --fail --location --remote-name \
  https://github.com/Substrate-memory/hermes-substrate-wiki/releases/download/v1.5.0/substrate_wiki.zip
curl --fail --location --remote-name \
  https://github.com/Substrate-memory/hermes-substrate-wiki/releases/download/v1.5.0/install_hermes_plugin.py
curl --fail --location --remote-name \
  https://github.com/Substrate-memory/hermes-substrate-wiki/releases/download/v1.5.0/SHA256SUMS
sha256sum -c SHA256SUMS

python3 install_hermes_plugin.py \
  --archive substrate_wiki.zip \
  --sha256 "$(sha256sum substrate_wiki.zip | cut -d ' ' -f1)" \
  --install-import-service \
  --yes --json
```

The installer validates the archive checksum, repository provenance, declared target-Hermes metadata, and every packaged source digest. It does not probe the installed Hermes executable; compatibility with Hermes `0.18.2` is interface-contract tested, while full host-lifecycle certification remains pending. Installation upgrades atomically and preserves a rollback directory.

Configure the Hermes process environment—not `plugin.yaml` or `config.json`:

```text
HERMES_API_URL=https://your-substrate-server.example
HERMES_API_KEY=your-profile-scoped-bearer-key
```

Then select `substrate_wiki` as the Hermes memory provider. See [configuration and operation](docs/operation.md).

## What it does

- Implements Hermes provider lifecycle hooks and tools.
- Prefetches bounded cited `memory_card` results for automatic recall.
- Captures completed turns, memory writes, and session boundaries.
- Redacts credential-shaped values before durable local spooling or network transfer.
- Replays Hermes history with deterministic event IDs and durable checkpoints.
- Keeps spool/checkpoint state beneath the active `$HERMES_HOME/substrate_wiki` profile.

It does **not** expose arbitrary filesystem writes, include Substrate server code, bundle credentials, or make private history public.

## Privacy boundary

Visible prompts, assistant output, tool calls, and tool results may be sent to the configured Substrate server after redaction. Redaction is defense in depth, not proof arbitrary sensitive prose is absent. Review the server's access and retention policy before enabling capture.

Local failed deliveries remain in a bounded owner-private spool until delivered or explicitly removed. Status, progress, and receipts are content-free.

Read [SECURITY.md](SECURITY.md), [the threat model](docs/threat-model.md), [the source-ownership boundary](docs/source-of-truth.md), and the permanent [open/held commercial boundary](BOUNDARY.md) before deployment.

## Open and paid boundary

The open side is permissively licensed and includes the Hermes plugin/client, local runtime, memory extraction and entity model, credential containment, privacy deletion, policy compiler, and a single-user local self-hosted path that remains free forever. Some of those components are not built yet; [BOUNDARY.md](BOUNDARY.md) distinguishes the permanent commitment from the current `v1.5.0` implementation.

The paid hosted tier covers hosted brokerage, multi-user operation, cross-organizational graph services, audit/attestation, and insurance-backed decisions. Its meter is per authorized action, never seats. This repository does not contain or license those held services.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
python -m compileall -q src scripts
python scripts/verify_public_plugin_candidate.py --root . --layout destination
```

Builds are deterministic and require an immutable source commit for releases:

```bash
HERMES_PLUGIN_SOURCE_COMMIT="$(git rev-parse HEAD)" python scripts/build_plugin.py
python scripts/build_plugin.py --check
```

Version `1.4.1` remains an imported immutable release with its original Substrate-v2 provenance. Repository-native releases begin at `1.5.0` and use standalone provenance.

## Repository map

- `src/substrate_wiki/` — canonical plugin source.
- `scripts/` — deterministic builder, verified installer, publication scanner, import benchmark.
- `tests/` — provider, privacy, replay, packaging, and resource-boundary tests.
- `legacy-assets/` — immutable releases imported from Substrate-v2.
- `docs/` — architecture, compatibility, ownership, release, and threat-model contracts.
- `BOUNDARY.md` — permanent open-source/commercial boundary and current implementation gaps.

## License

MIT © 2026 Sightline Technologies Inc. See [LICENSE](LICENSE).
