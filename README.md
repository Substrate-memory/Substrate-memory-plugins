# Substrate Memory Plugins

This is the official public repository for the Substrate memory plugin for
Hermes 0.21.x. The current release is
[`v0.3.0`](https://github.com/Substrate-memory/Substrate-memory-plugins/releases/tag/v0.3.0).

## Install with a Hermes agent

The user only needs to say:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

### Required behavior for the installing agent

Treat the sentence above as approval to install and configure the current Substrate plugin
for the active Hermes profile. Do not ask the user for repository paths, commands, commit
hashes, API keys, or TLS workarounds.

1. Confirm `hermes --version` is 0.21.x. Do not upgrade Hermes automatically.
2. Install the plugin subdirectory at the released tag, not the repository root:

   ```sh
   hermes plugins install \
     Substrate-memory/Substrate-memory-plugins/plugins/substrate \
     --ref v0.3.0 --no-enable
   ```

3. Run `hermes plugins doctor substrate --ci`. Stop without changing the legacy provider
   if installation or doctor fails.
4. Enable the plugin and let device onboarding run on first use. When a
   `verification_uri_complete` link appears (from `memory_search` or the first turn),
   present that exact clickable URL to the user. The user approves in their browser;
   the agent must never approve it for them or ask for a pasted key.
5. The plugin stores the tenant-scoped key privately in the active profile's `.env` and
   retries automatically. No manual credential migration is required.
6. Only after a memory call succeeds, disable `substrate_wiki` (if an old provider is
   installed), clear an old `memory.provider: substrate_wiki` selection with
   `hermes memory off`, and restart the gateway. This prevents duplicate capture while
   preserving built-in local memory.
7. The optional `setup.py` can pre-connect during installation, but it is not required.
8. Verify the new `memory_search` tool in a new turn. `wiki_search` is legacy and is not a
   valid verification of the current plugin.
9. Report success only after `memory_search` reaches the authenticated Substrate API.
   Fail closed and report the exact safe error class otherwise.

TLS verification must remain enabled. The plugin supplements the host trust store with
unmodified public ISRG Root X1 and X2 certificates for current Let's Encrypt chains. It
never trusts a replacement leaf certificate or a private certificate.

## Current plugin

The `substrate` plugin is a thin, standard-library-only adapter. It provides:

- bounded next-turn memory context through `pre_llm_call`;
- nonblocking completed-turn capture through `post_llm_call`;
- session completion markers through `on_session_reset` and `on_session_finalize`, so
  ended sessions are materialized into the extraction pipeline automatically;
- `memory_search`, `memory_expand`, and `memory_evidence`;
- active-profile credential reuse and passwordless device authorization;
- agent display names, selectable on the approval page and visible in the agent pane;
- additive public-root TLS trust without disabling certificate or hostname checks.

The Substrate server remains the source of truth for storage, ranking, the associative
editor, and evidence.

## Privacy boundary

Visible prompts and assistant output may be sent to the configured Substrate server after
redaction. The current plugin does not upload tool results or system messages. It caches no
retrieved memory locally and returns empty context on any retrieval failure.

Read [SECURITY.md](SECURITY.md) and [the threat model](docs/threat-model.md) before
deployment.

## Development

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev python -m pytest -q
python3 scripts/check_public_hygiene.py --root .
uv run --frozen --extra dev python scripts/build_release.py --check
```

## Repository map

- `plugins/substrate/` — the Hermes 0.21.x retrieval plugin and setup flow.
- `scripts/` — deterministic release builder and public-hygiene check.
- `tests/` — contract, behavior, transport, onboarding, and packaging tests.
- `docs/` — architecture, ownership, and threat-model contracts.

## License

MIT © 2026 Sightline Technologies Inc. See [LICENSE](LICENSE).
