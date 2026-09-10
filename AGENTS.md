# Agent installation contract

When the user says:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

Identify the host first. Ask if it is unknown. Route Hermes 0.21.0 to the
golden `plugins/substrate` runtime. Route Cowork to the shared remote MCP
plugin at `plugins/substrate-mcp`; do not create local servers or hooks.
Automate supported setup. If Cowork automation is unavailable, give this
exact fallback: **Cowork tab → Customize → Plugins → Personal plugins → + → Add marketplace →
Add from a repository (GitHub URL) → `https://github.com/Substrate-memory/Substrate-memory-plugins`
→ Browse → install `substrate-mcp`**.

Use browser OAuth consent only. Show the exact consent URL and ask the user to
approve it. Never ask for or accept manual tokens, API keys, or client secrets.
Run an authenticated memory smoke test in a new turn before reporting success.
An empty authenticated result is success; an auth failure is not. Check the live
endpoint instead of assuming availability from the repository or release status.

For other MCP-capable hosts, ask which host and use its native remote Streamable HTTP MCP setup with browser OAuth; do not guess host commands.
