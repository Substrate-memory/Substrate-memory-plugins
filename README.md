# Substrate Memory Plugins

This is the official public repository and canonical editable source for Substrate memory integrations. The current
Hermes plugin is [`plugins/substrate`](plugins/substrate) and supports Hermes 0.21.x.

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
2. Install the plugin subdirectory, not the repository root:

   ```sh
   substrate_ref="$(git ls-remote https://github.com/Substrate-memory/Substrate-memory-plugins.git refs/heads/main | awk '{print $1}')"
   printf '%s\n' "$substrate_ref" | grep -Eq '^[0-9a-f]{40}$'
   hermes plugins install \
     Substrate-memory/Substrate-memory-plugins/plugins/substrate \
     --ref "$substrate_ref" --no-enable
   ```

3. Run `hermes plugins doctor substrate --ci`. Stop without changing the legacy provider
   if installation or doctor fails.
4. Enable the plugin and let device onboarding run on first use. When a
   `verification_uri_complete` link appears (from `memory_search` or the first turn),
   present that exact clickable URL to the user. The user approves in their browser;
   the agent must never approve it for them or ask for a pasted key.
5. The plugin stores the tenant-scoped key privately in the active profile's `.env` and
   retries automatically. No manual credential migration is required.
6. Only after a memory call succeeds, disable `substrate_wiki`, clear an old
   `memory.provider: substrate_wiki` selection with `hermes memory off`, and restart the
   gateway. This prevents duplicate capture while preserving built-in local memory.
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
- `memory_search`, `memory_expand`, and `memory_evidence`;
- active-profile credential migration and passwordless device authorization;
- additive public-root TLS trust without disabling certificate or hostname checks.

The Substrate server remains the source of truth for storage, ranking, the associative
editor, and evidence.

## Legacy compatibility

Hermes 0.21.x is certified for the current `substrate` plugin. The legacy
`src/substrate_wiki` provider and its release tooling are retained for existing Hermes 0.20.x
installations and migration tests. It is not the current install target, and its
compatibility rows in `COMPATIBILITY.md` apply only to 0.20.x. Do not run
`scripts/install_hermes_plugin.py` for the prompt above. Legacy packaging state remains
unchanged: `v2.0.5` is not published yet, its historical URL would contain
`releases/download/v2.0.5`, and its unpublished checksum marker remains
`PLUGIN_SHA256_PENDING`. These strings document legacy release truth; they are not install
instructions. Immutable historical releases remain under `legacy-assets` and Git tags.

## Privacy boundary

Visible prompts and assistant output may be sent to the configured Substrate server after
redaction. The current plugin does not upload tool results or system messages. It caches no
retrieved memory locally and returns empty context on any retrieval failure.

Read [SECURITY.md](SECURITY.md), [the threat model](docs/threat-model.md),
[the source-ownership boundary](docs/source-of-truth.md), and [BOUNDARY.md](BOUNDARY.md)
before deployment.

## Development

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev python -m pytest -q
python3 scripts/verify_public_plugin_candidate.py --root . --layout destination
```

## Repository map

- `plugins/substrate/` — current Hermes 0.21.x retrieval plugin and setup flow.
- `src/substrate_wiki/` — legacy provider retained for migration compatibility.
- `scripts/` — deterministic legacy builder, installer, and publication scanner.
- `tests/` — provider, privacy, replay, packaging, retrieval, and setup tests.
- `legacy-assets/` — immutable historical releases.
- `docs/` — architecture, compatibility, ownership, and threat-model contracts.

## License

MIT © 2026 Sightline Technologies Inc. See [LICENSE](LICENSE).
