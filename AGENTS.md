# Agent installation contract

For the request:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

Follow [the single installation and recovery guide](docs/installation.md).
Target the current host: Hermes uses `plugins/substrate`; Cowork uses the thin
`plugins/substrate-mcp` package; other compatible agents use the shared MCP URL.
Ask which host only when it cannot be determined safely. Do not select a host
merely because its executable happens to be installed.

Keep the user-facing sequence the same: **Install → Sign in → Review → Approve
connection → Return to the agent → Connected to Substrate.** Open the exact
returned browser link. The user approves on **Connect your agent to Substrate**.
Do not approve for the user or ask for keys, manual tokens, or client secrets.
The browser's **Connection approved** message is not an installation success.
Run an authenticated memory smoke test before saying **Connected to Substrate.**

Automate client setup where supported. For client-owned actions you cannot perform,
give precise instructions from the shared guide, then resume the same flow.
Never invent a local MCP server, bypass permissions, or use a pasted-token fallback.
Preserve the golden Hermes runtime and active-profile credential isolation.
