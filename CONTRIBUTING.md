# Contributing

## Scope

This repository owns the Hermes `substrate` retrieval plugin (`plugins/substrate`), its
device onboarding, session-completion capture, response validation, release builder,
tests, and user documentation. Server routes, persistence, deployment, and production
data belong in the Substrate server repository.

## Workflow

1. Create a branch from `main`.
2. Add regression tests before changing behavior.
3. Keep the runtime standard-library-only unless an explicit reviewed decision changes that boundary.
4. Run:

   ```bash
   uv sync --frozen --extra dev
   uv run --frozen --extra dev ruff check .
   uv run --frozen --extra dev python -m pytest -q
   python3 scripts/check_public_hygiene.py --root .
   uv run --frozen --extra dev python scripts/build_release.py --check
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

Release versions and immutable artifact paths are write-once. Release archives must identify a committed source tree. Never rebuild an old version and present different bytes under the same tag. See `docs/releasing.md`.

## Sensitive material

Only synthetic fixtures are allowed. Never commit credentials, private history, customer data, private endpoints, or production evidence. Use the private vulnerability-reporting flow for security findings.
