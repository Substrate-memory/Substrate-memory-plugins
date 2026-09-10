# Releasing

Release `0.6.0` is a review-stage redesign. The repository now ships exactly
two deterministic archives:

- `substrate.zip`: the Hermes `substrate` plugin. Its runtime files are
  preserved byte-for-byte from the v0.5.0 golden tree. Hermes installation stays
  pinned to the already-published `v0.5.0` tag while the v0.6.0 release is a draft,
  so merging this repository does not break new Hermes installations.
- `substrate-mcp.zip`: the thin Cowork-compatible plugin with a remote HTTP
  `.mcp.json`, generic skill, and installation documentation. It has no local
  server and no hooks.

The remote MCP service at `https://app.trysubstrate.co/mcp` and browser OAuth
backend are backend-owned and not deployed by this repository yet. Release docs
must not claim live Cowork support until an authenticated smoke test succeeds.

## Review staging

1. Prepare a review branch and draft pull request. Every commit needs DCO sign-off.
   Do not merge it before the owner's review acceptance.
2. Run the repository-native checks and build the archives twice.
3. Dispatch `.github/workflows/release-staging.yml` with the candidate for short-lived
   draft artifacts. For a protected-main candidate, `.github/workflows/release.yml`
   may also run with `publish_approval=REVIEW`; it scans, tests, double-builds,
   and uploads artifacts without publication.
4. Inspect the draft artifact and verify the archive hashes. Do not tag, publish,
   deploy, or advertise the release from this branch.
5. The protected release workflow may promote only an owner-created matching draft
   after explicit approval (`publish_approval=PUBLISH`) and exact protected-main
   candidate binding. It verifies the exact draft asset set and every byte,
   then creates the immutable tag at the reviewed main commit (or verifies an
   existing tag points there) and promotes the draft without replacing its notes.
   It refuses missing drafts, published releases, different assets or conflicting
   tags. The staging workflow has no publication job and cannot create tags or
   GitHub releases.

An owner may prepare a GitHub draft targeting the review-branch commit and attach
`substrate.zip`, `substrate-mcp.zip`, and `SHA256SUMS` before review. This does not
create a public tag. After code review, separately approved backend deployment and
Cowork acceptance, merge the reviewed changes into main. If archive bytes changed
since draft preparation, rebuild and re-review the draft assets before selecting
`PUBLISH`. Merely creating a draft is never deployment or publication approval.

## Deterministic local check

```sh
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev python -m pytest -q
python3 scripts/check_public_hygiene.py --root .
rm -rf dist && python3 scripts/build_release.py
cp dist/substrate.zip /tmp/first-substrate.zip
cp dist/substrate-mcp.zip /tmp/first-substrate-mcp.zip
rm -rf dist && python3 scripts/build_release.py
cmp /tmp/first-substrate.zip dist/substrate.zip
cmp /tmp/first-substrate-mcp.zip dist/substrate-mcp.zip
python3 scripts/build_release.py --check
```

Older immutable releases (`v0.3.0`, `v0.4.0`, `v0.5.0`) remain available for
rollback. Never rebuild or mutate their assets.
