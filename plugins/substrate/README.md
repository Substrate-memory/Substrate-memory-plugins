# Substrate memory for Hermes 0.21.x

This is the current Substrate v5 retrieval plugin. The server owns storage, ranking,
the associative editor, and evidence. The plugin caches no memory and fails closed.

## Installing agents

Install this directory, not the repository root:

```sh
substrate_ref="$(git ls-remote https://github.com/Substrate-memory/Substrate-memory-plugins.git refs/heads/main | awk '{print $1}')"
printf '%s\n' "$substrate_ref" | grep -Eq '^[0-9a-f]{40}$'
hermes plugins install \
  Substrate-memory/Substrate-memory-plugins/plugins/substrate \
  --ref "$substrate_ref" --no-enable
```

No manual credential setup is required. The plugin connects itself on first use:

- it reuses a valid existing profile credential when one is present;
- otherwise it starts RFC 8628 device authorization and shows the browser
  approval link through the agent (`memory_search` or the first turn);
- it polls for the tenant-scoped key, validates it with an authenticated
  capabilities and search health check, and stores it privately as
  `SUBSTRATE_API_KEY` in the active profile's `.env`;
- it never asks the user to paste a key into chat;
- TLS verification stays enabled, supplemented only by the pinned public
  ISRG Root X1 and X2 trust anchors.

Never print an access token. Never disable TLS verification. Never inspect another Hermes
profile. After setup succeeds, disable `substrate_wiki`, clear `memory.provider`, enable
`substrate`, restart the gateway, and verify `memory_search` in a new turn.

## Runtime

The plugin registers:

- `pre_llm_call` for validated turn context;
- `post_llm_call` for nonblocking full completed-turn capture;
- `memory_search`, `memory_expand`, and `memory_evidence`.

Optional overrides are `SUBSTRATE_API_URL`, `SUBSTRATE_API_KEY`, and
`SUBSTRATE_WIKI_ORIGIN`. The active profile's secure plugin state is used when these are
unset.

The bundled CA files are unmodified public ISRG roots, loaded in addition to system trust:

- ISRG Root X1 SHA-256: `96:BC:EC:06:26:49:76:F3:74:60:77:9A:CF:28:C5:A7:CF:E8:A3:C0:AA:E1:1A:8F:FC:EE:05:C0:BD:DF:08:C6`
- ISRG Root X2 SHA-256: `69:72:9B:8E:15:A8:6E:FC:17:7A:57:AF:B7:17:1D:FC:64:AD:D2:8C:2F:CA:8C:F1:50:7E:34:45:3C:CB:14:70`

Wire schemas and limits are defined in [`CONTRACT.md`](CONTRACT.md). Runtime code uses only
the Python standard library.
