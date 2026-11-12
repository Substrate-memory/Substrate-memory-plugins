# Substrate Memory for Claude Code: Manual capture

This is the **Manual** build of [`substrate-claude`](../substrate-claude/README.md). It is generated from that
plugin by `scripts/build_manual_variant.py`, so do not edit it by hand.

| | Automatic (`substrate-claude`) | Manual (`substrate-claude-manual`) |
|---|---|---|
| Recall on every prompt (`memory_turn_context`) | yes | yes |
| Live capture of tool calls (`memory_capture_tool`) | yes | **no** |
| Live capture of assistant turns (`memory_capture_turn`) | yes | **no** |
| Compaction boundary (`memory_session_boundary`) | yes | **no** |
| Prompt stored as a captured turn | yes | **no** (`capture_mode: "manual"`) |
| `memory_search` / `memory_remember` / import on request | yes | yes |

In Manual, nothing from a conversation is saved unless you ask: tell Claude to remember something, or run
`/substrate-claude-manual:substrate-import`. Recall still sends each prompt to Substrate to find relevant memory, but the server does not store it
(this needs a Substrate server that accepts `capture_mode`).

Install exactly one of the two plugins:

```bash
claude plugin marketplace add Substrate-memory/Substrate-memory-plugins
claude plugin install substrate-claude-manual@substrate-marketplace
```

To switch from Automatic, uninstall `substrate-claude@substrate-marketplace` first, then restart Claude Code.
`/substrate-claude-manual:substrate-connect` asks for the mode and prints the switch commands.
Everything else (sign-in, import, scope, permissions) works as described in the
[substrate-claude README](../substrate-claude/README.md).
