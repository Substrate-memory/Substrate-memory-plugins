# Releasing

Releases are immutable Git tags cut from protected `main` through the Release workflow.
One tag publishes the whole multi-host plugin set.

## Versioning

- The repo-level release version lives in the root `VERSION` file (currently `0.4.0`).
  The Release workflow reads the tag version from `VERSION`, never from a hard-coded string.
- `plugins/substrate/` (the Hermes plugin) is frozen at content version `0.3.0`:
  its `plugin.yaml` version and its `substrate.zip` bytes are unchanged by the `0.4.0`
  release. The release version covers the plugin set, not the Hermes plugin content.
- The five host adapters (`plugins/claude-code/`, `plugins/claude-cowork/`,
  `plugins/codex/`, `plugins/grok-bot/`, `plugins/openclaw/`) carry the release
  version in their native manifests (currently `0.4.0`).

## Process

1. Land the release contents on `main` through a reviewed pull request. Every commit
   must carry a DCO sign-off and pass all CI checks.
2. Dispatch the **Release** workflow with `candidate_sha` set to the exact reviewed
   `main` commit.
3. The workflow re-verifies the candidate (hygiene scan before dependency install,
   DCO sign-off grep, candidate-sha binding to protected `main`, pinned action SHAs,
   version checks, full tests, deterministic double-build of all six archives with
   byte-compare plus `scripts/build_release.py --check`) and publishes tag
   `v<VERSION>` with all six archives (`substrate.zip`, `claude-code.zip`,
   `claude-cowork.zip`, `codex.zip`, `grok-bot.zip`, `openclaw.zip`) and the single
   `SHA256SUMS` covering all six, plus build attestation for every artifact.
4. The workflow refuses to mutate an existing tag or release: if the tag already
   exists it must point at the same candidate commit, and if the release already
   exists the run fails instead of overwriting immutable assets.
5. Never mutate a published tag or its assets. Fixes ship as a new reviewed commit
   and a new tag.

## Tag history

- `v0.3.0` provides the Hermes `substrate` plugin only (`substrate.zip` +
  `SHA256SUMS`). Hermes installs pinned to `v0.3.0` keep working.
- `v0.4.0` provides all six plugins. The `substrate.zip` bytes inside `v0.4.0`
  are identical to the `v0.3.0` asset; the other five archives are new at `0.4.0`.
  New-plugin installs pin `v0.4.0`; Hermes installs may use either tag.

## Local verification

```sh
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev python -m pytest -q
python3 scripts/check_public_hygiene.py --root .
rm -rf dist && python3 scripts/build_release.py
cp dist/substrate.zip /tmp/first-substrate.zip
rm -rf dist && python3 scripts/build_release.py
cmp /tmp/first-substrate.zip dist/substrate.zip
python3 scripts/build_release.py --check
```
