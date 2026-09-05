# Substrate memory for Hermes 0.21.x

Durable v5 retrieval plugin. The server owns storage, ranking, the
associative editor, and evidence. The plugin caches no memory and fails
closed. Delivery is durable: capture and boundary events enqueue into a
write-ahead spool at live priority, and explicit writes enqueue at
explicit priority. Nothing is fire-and-forget.

## Installing agents

Install this directory, not the repository root:

```sh
substrate_ref="$(git ls-remote https://github.com/Substrate-memory/Substrate-memory-plugins.git refs/tags/v0.5.0 | awk '{print $1}')"
printf '%s\n' "$substrate_ref" | grep -Eq '^[0-9a-f]{40}$'
hermes plugins install \
  Substrate-memory/Substrate-memory-plugins/plugins/substrate \
  --ref "$substrate_ref" --no-enable
```

No manual credential setup is required. Connect the active profile with the
bundled login CLI (standard library only, no `PYTHONPATH` needed):

```sh
python <plugin-dir>/onboard.py start --json
```

When a `verification_uri_complete` link appears (from the login CLI, from
`memory_search`, or on the first turn), present that exact clickable URL to
the user. The user approves in their browser; the agent must never approve
it for them or ask for a pasted key. Then wait for approval:

```sh
python <plugin-dir>/onboard.py poll --json
python <plugin-dir>/onboard.py status --json
```

The plugin stores the tenant-scoped key privately in the active profile
only (`<profile>/.env` plus `<profile>/substrate/credentials/access-token`,
both owner-only). It never asks the user to paste a key into chat. If
`memory_search` reports `authorization_required`, run the commands above
and retry.

TLS verification stays enabled with the host trust store. Never disable
TLS verification, install a private certificate, or inspect another Hermes
profile.

After setup succeeds, disable `substrate_wiki` (if an old provider is
installed), enable `substrate`, restart the gateway, and verify
`memory_search` in a new turn. Do not change `memory.provider` before a
memory call succeeds.

## Runtime

The plugin registers:

- `pre_llm_call` for validated turn context;
- `post_llm_call`, fired once per turn by the host with the finalized
  conversation (the sole live-capture entry point);
- `on_session_end`, which fires at the end of every turn and intentionally
  emits nothing;
- `on_session_finalize` for the true content-free session-end boundary;
- `on_session_reset`, fired with the new session id after rotation;
- `subagent_start` / `subagent_stop` for parent-routed capture;
- `memory_search`, `memory_expand`, and `memory_evidence`;
- `memory_remember` (`text` plus `durability` in `durable`,
  `time_bounded`, `transient`), which returns the server's `m:` handle;
- `memory_forget` (exactly one handle plus a required non-empty reason),
  which marks one memory atom as no longer true without deleting its
  record or evidence.

Optional overrides are `SUBSTRATE_API_URL`, `SUBSTRATE_API_KEY`, and
`SUBSTRATE_AGENT_NAME`. The agent name is what you and your agents see in
the Substrate agent pane; the approval page lets you edit it before
approving, and it can be renamed there later.

Wire schemas and limits are defined in [`CONTRACT.md`](CONTRACT.md).
Runtime code uses only the Python standard library. Supported host:
Hermes 0.21.x.
