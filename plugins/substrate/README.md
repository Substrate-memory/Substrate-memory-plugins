# Hermes Substrate plugin

Thin, stdlib-only adapter for the Substrate v5 retrieval API. The server owns ranking, the associative editor, storage, and evidence. The plugin caches nothing and injects nothing on any error.

## Configure

```bash
export SUBSTRATE_API_URL=https://app.trysubstrate.co
export SUBSTRATE_API_KEY=sk_sub_...
hermes --profile developer plugins install \
  Substrate-memory/Substrate-memory-plugins/plugins/substrate --enable
```

The plugin registers:

- `pre_llm_call` for validated turn context;
- `post_llm_call` for nonblocking full completed-turn capture;
- `memory_search`, `memory_expand`, and `memory_evidence`.

Wire schemas and limits are defined in [`CONTRACT.md`](CONTRACT.md). Runtime code has no third-party dependencies.

## Verify

```bash
uv run --with pytest pytest -q
cd /path/to/hermes-agent
uv run hermes plugins doctor /path/to/Substrate-memory-plugins/plugins/substrate --ci
```
