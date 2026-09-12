# Substrate memory for Cowork

This is the thin Cowork-compatible Substrate MCP plugin. It registers one
remote HTTP MCP server and one generic usage skill. It contains no local MCP
server, hooks, transcript bridge, credentials, or host-specific runtime.

The remote service and browser connection flow are backend-owned. Hermes and
MCP use the same Substrate sign-in, review, naming and approval screen; only their
protocol handoff differs. This repository configures the connection, not the server. Do not
report an installation as successful until the endpoint is live and an
authenticated memory smoke test passes.

## Contents

- `.mcp.json` — remote HTTP MCP at `https://app.trysubstrate.co/mcp`.
- `skills/substrate-memory/SKILL.md` — generic, best-effort recall/write guidance.
- `INSTALL.md` — Cowork installation and verification runbook.

The server may expose tools such as `memory_search` and a durable write tool.
The skill does not assume that every host exposes the same tool names or
transcript lifecycle. Recall and writes are best effort. A full conversation
transcript is not automatically guaranteed to be captured.

Do not place API keys, OAuth tokens, client secrets, or other credentials in
plugin files, prompts, config, or chat. OAuth approval happens in the browser.
