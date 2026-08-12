# Hermes Substrate Wiki

`substrate_wiki` is the official Hermes integration for Substrate: **the memory that decides what your agents are allowed to do**. The pending `v2.0.0` candidate connects one Hermes profile to hosted Substrate at `https://app.trysubstrate.co` through a bounded, cited memory interface and durable, redacted history capture.

The broader product uses memory to ground deterministic agent permissions. **Version 2.0.0 is the memory-provider integration only:** it does not authorize actions, include the local runtime or policy compiler, or support a no-server mode. Those gaps are explicit rather than silently presented as hosted-only features.

**This repository is the publication-approved source candidate for the plugin.** It becomes
the sole editable source only after the protected default branch, first immutable release,
and companion `Substrate-v2` retirement change are complete. `Substrate-v2` owns the server
and HTTP contract; its embedded plugin copy remains frozen during that transition.

## Status

| Surface | Supported |
|---|---|
| Plugin | `substrate_wiki` 2.0.0 |
| Hermes host | Targets 0.20.x; standalone contract suite verified |
| Server contract | `stream-v2`, `entity-wiki-v1`, `entity-quality-v2` |
| Runtime dependencies | Python standard library only |
| License | MIT |

Hermes 0.20.x provider discovery and setup hooks are targeted. The hosted origin is fixed; there is no local/self-hosted discovery path.

## Release availability

`v2.0.0` is not published yet. Do not treat candidate archives or CI artifacts as an
installable release. After the immutable release exists, download its three assets from
[release v2.0.0](https://github.com/Substrate-memory/hermes-substrate-wiki/releases/tag/v2.0.0),
then verify and install using the SHA-256 values published with that release:

```bash
curl --fail --location --remote-name \
  https://github.com/Substrate-memory/hermes-substrate-wiki/releases/download/v2.0.0/substrate_wiki.zip
curl --fail --location --remote-name \
  https://github.com/Substrate-memory/hermes-substrate-wiki/releases/download/v2.0.0/install_hermes_plugin.py
curl --fail --location --remote-name \
  https://github.com/Substrate-memory/hermes-substrate-wiki/releases/download/v2.0.0/SHA256SUMS
sha256sum -c SHA256SUMS

python3 install_hermes_plugin.py \
  --archive substrate_wiki.zip \
  --sha256 "$(sha256sum substrate_wiki.zip | cut -d ' ' -f1)" \
  --yes --json
```

The installer validates the archive checksum, provenance, target-Hermes metadata, and packaged source digests, installs beneath `$HERMES_HOME/plugins/substrate_wiki`, activates the provider, and starts hosted device onboarding. Desktop browsers open automatically; headless users receive a hosted URL and one-time code. Tenant credentials are stored in native credential custody (with an owner-private fallback), never in ordinary configuration or logs. Historical upload is the sole consent choice; declining it leaves future capture enabled. See [configuration and operation](docs/operation.md).

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

The open side is permissively licensed and includes the Hermes plugin/client, memory extraction and entity model, credential containment, privacy deletion, and policy compiler. This Hermes integration itself is hosted-only. [BOUNDARY.md](BOUNDARY.md) distinguishes the permanent commitment from the current `v2.0.0` implementation.

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

Version `1.4.1` remains an imported immutable release with its original Substrate-v2 provenance. Repository-native releases begin at `2.0.0` and use standalone provenance.

## Repository map

- `src/substrate_wiki/` — canonical plugin source.
- `scripts/` — deterministic builder, verified installer, publication scanner, import benchmark.
- `tests/` — provider, privacy, replay, packaging, and resource-boundary tests.
- `legacy-assets/` — immutable releases imported from Substrate-v2.
- `docs/` — architecture, compatibility, ownership, release, and threat-model contracts.
- `BOUNDARY.md` — permanent open-source/commercial boundary and current implementation gaps.

## License

MIT © 2026 Sightline Technologies Inc. See [LICENSE](LICENSE).
