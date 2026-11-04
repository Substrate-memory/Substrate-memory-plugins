# Contributing

## Scope

This repository owns the golden Hermes client (`plugins/substrate`), the thin
remote MCP installation package (`plugins/substrate-mcp`), their shared installation
guide, deterministic release builder, tests and user documentation. Server routes,
the shared browser connection screen, authentication services, persistence and
deployment belong in the Substrate server repository. Do not add another per-agent
server or credential implementation here.

## Workflow

1. Create a branch from `main`.
2. Add regression tests before changing behavior.
3. Keep the runtime standard-library-only unless an explicit reviewed decision changes that boundary.
4. Run:

   ```bash
   UV_PROJECT_ENVIRONMENT=$PWD/.venv uv sync --project tooling --frozen --extra dev
   .venv/bin/ruff check .
   .venv/bin/python -m pytest -q
   python3 scripts/check_public_hygiene.py --root .
   .venv/bin/python scripts/build_release.py --check
   ```

5. Update `CHANGELOG.md` and compatibility/security docs when behavior changes.
6. Sign every commit with the [Developer Certificate of Origin 1.1](https://developercertificate.org/) by adding `Signed-off-by: Name <email>` (`git commit -s`). This repository uses DCO sign-off, not a contributor license agreement.
7. Open a PR. Every PR requires passing CI.

## Compatibility

Do not claim a Hermes or server version based on import success alone. Hook registration,
tool schemas, profile isolation, onboarding, session capture, configuration, and failure
behavior must all pass.

Breaking server behavior requires a new protocol/schema identifier with concurrent old-version support. Never silently rewrite user configuration or state.

## Releases

Every release bumps `VERSION`, runs `python3 scripts/check_versions.py --write`, and adds a `CHANGELOG.md` entry. In order:

1. Merge to `main`. The Claude directory picks up `main` through its webhook.
2. Publish the MCP registry entry (`server.json`) with `mcp-publisher publish`.
3. Build the OpenAI package with `python3 scripts/build_openai_package.py` and upload `dist/substrate-openai-<version>.zip` in the OpenAI portal when the listing text (`openai/plugin.json`) or the tool set changed. OpenAI rescans a hosted server's tool schema daily on its own.

The same steps are in `docs/releasing.md`.

Release versions and immutable artifact paths are write-once. Release archives must identify a committed source tree. Never rebuild an old version and present different bytes under the same tag. See `docs/releasing.md`.

## Sensitive material

Only synthetic fixtures are allowed. Never commit credentials, private history, customer data, private endpoints, or production evidence. Use the private vulnerability-reporting flow for security findings.
