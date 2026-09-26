# Releasing

Release `0.8.0` fixes Hermes install and sign-in on top of the `0.7.0` unified-connection release. The repository ships four deterministic archives (see `scripts/build_release.py` for the exact archive set; if the builder still lists the old two-archive layout, it must be extended to the four packages below before release):

- `substrate-hermes.zip`: the Hermes package (`plugins/substrate-hermes`) with durable write-ahead spool, session boundaries, subagent capture, and device login.
- `substrate-claude.zip`: the Claude package (`plugins/substrate-claude`) with `memory_*` hook wiring and the sync command for Cowork and Claude Code.
- `substrate-codex.zip`: the Codex package (`plugins/substrate-codex`) with `memory_*` hook wiring and the sync command for ChatGPT Work, the Codex app, and Codex CLI.
- `substrate-mcp.zip`: the thin fallback (`plugins/substrate-mcp`) with a remote HTTP `.mcp.json`, usage skill, and installation documentation. It has no local server and no hooks.

The remote MCP service at `https://app.trysubstrate.co/mcp` and browser OAuth backend are operated separately from this client repository. Release docs must not claim live host support until an authenticated smoke test succeeds on that host. The server keeps the Hermes `/api/v1` wire during the rollout. The immutable `v0.5.0` and `v0.6.0` tags remain available for rollback.

## Review staging

1. Prepare a review branch and draft pull request. Every commit needs DCO sign-off (`git commit -s`). Do not merge it before the owner's review acceptance.
2. Run the repository-native checks and build the archives twice.
3. Dispatch `.github/workflows/release-staging.yml` with the candidate for short-lived draft artifacts. For a protected-main candidate, `.github/workflows/release.yml` may also run with `publish_approval=REVIEW`; it scans, tests, double-builds, and uploads artifacts without publication.
4. Inspect the draft artifact and verify the archive hashes. Do not tag, publish, deploy, or advertise the release from this branch.
5. The protected release workflow may promote only an owner-created matching draft after explicit approval (`publish_approval=PUBLISH`) and exact protected-main candidate binding. It verifies the exact draft asset set and every byte, then creates the immutable tag at the reviewed main commit (or verifies an existing tag points there) and promotes the draft without replacing its notes. It refuses missing drafts, published releases, different assets or conflicting tags. The staging workflow has no publication job and cannot create tags or GitHub releases.

An owner may prepare a GitHub draft targeting the review-branch commit and attach the four archives plus `SHA256SUMS` before review. This does not create a public tag. After code review, separately approved backend deployment and host acceptance, merge the reviewed changes into main. If archive bytes changed since draft preparation, rebuild and re-review the draft assets before selecting `PUBLISH`. Merely creating a draft is never deployment or publication approval.

## Deterministic local check

```sh
uv sync --frozen --extra dev
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
python3 scripts/check_public_hygiene.py --root .
rm -rf dist && python3 scripts/build_release.py
cp dist/substrate-hermes.zip /tmp/first-hermes.zip
cp dist/substrate-mcp.zip /tmp/first-mcp.zip
rm -rf dist && python3 scripts/build_release.py
cmp /tmp/first-hermes.zip dist/substrate-hermes.zip
cmp /tmp/first-mcp.zip dist/substrate-mcp.zip
python3 scripts/build_release.py --check
```

Compare all four archives byte-for-byte across the two builds (the two `cp`/`cmp` lines above show the pattern). Older immutable releases (`v0.3.0`, `v0.4.0`, `v0.5.0`, `v0.6.0`) remain available for rollback. Never rebuild or mutate their assets.
