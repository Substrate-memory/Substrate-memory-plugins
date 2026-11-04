# Releasing

One `VERSION` covers every listing. This is the one checklist; run it top to bottom for each release.

The repository ships four deterministic archives (`scripts/build_release.py`): `substrate-hermes.zip`, `substrate-claude.zip`, `substrate-codex.zip` and `substrate-mcp.zip`, one per package under `plugins/`. The remote MCP service (`https://app.trysubstrate.co/mcp`) and its OAuth backend are operated separately from this repository. Do not claim live host support until an authenticated smoke test succeeds on that host.

## Checklist

1. **Bump.** Write the new number in `VERSION`, run `python3 scripts/check_versions.py --write`, then add the `CHANGELOG.md` entry. CI fails if any copy differs (plugins, `server.json`, `gemini-extension.json`, marketplace file, `openai/plugin.json`).
2. **Check and merge.** Run the local check below, open a PR (every commit signed off: `git commit -s`), squash-merge when CI is green. Every push to `main` is live for some users (see Gemini CLI), so `main` must always be releasable.
3. **Tag and release.** On the merged `main` commit: `gh release create v<version> --target main --title "v<version>: <headline>" --notes-file <notes> dist/*.zip dist/SHA256SUMS`, built with `scripts/build_release.py`. Tags are immutable; never rebuild or edit the assets of an old release. The staged route (owner draft, then `release.yml` with `publish_approval=PUBLISH`) does the same with extra approval; a draft is never publication approval.
4. **MCP registry.** `mcp-publisher publish` (signed in with the `co.trysubstrate` key, kept outside the repository).
5. **Claude directory.** A push webhook delivers `main`. Auto-publish is OFF, so press Publish for the new version at claude.ai/directory/manage.
6. **OpenAI directory.** `python3 scripts/build_openai_package.py`, then upload `dist/substrate-openai-<version>.zip` in the OpenAI portal only if the listing text or the core tool set changed (OpenAI rescans the hosted tool schema daily). The app is matched by package name `substrate-memory`; never change it. The package holds no reviewer credentials: give reviewers sign-in access in the portal form.
7. **Gemini CLI.** `gemini extensions install https://github.com/Substrate-memory/Substrate-memory-plugins` follows the Latest GitHub release when the repository has one, and falls back to the default branch (HEAD) when it has none; `--ref v<version>` pins a tag or branch. So step 3 is what ships to Gemini users; never leave a draft or pre-release as the newest. The gallery indexes the repository daily (topic `gemini-cli-extension`, `gemini-extension.json` at the root); the manifest `version` is what it displays, so keep it equal to the tag (step 1 does).
8. **Glama.** Nothing to do: it syncs automatically from the MCP registry (step 4).
9. **Docker, Cline.** Nothing to do.

## Deterministic local check

```sh
UV_PROJECT_ENVIRONMENT=$PWD/.venv uv sync --project tooling --frozen --extra dev
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
