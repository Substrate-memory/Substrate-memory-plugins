# Install Substrate memory

Use the same setup experience as Hermes:
**Install → Sign in → Review → Approve connection → Return to the agent →
Connected to Substrate.**

Start with:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

The shared [installation and recovery guide](https://github.com/Substrate-memory/Substrate-memory-plugins/blob/main/docs/installation.md)
is the source of truth for both integrations. This package configures
`https://app.trysubstrate.co/mcp` using remote **Streamable HTTP**; it does not
install another server.

Open the exact browser link returned by the client. Both integrations use
**Connect your agent to Substrate**. Sign in if needed, review the connection
name, permissions and proof details, then choose **Approve connection** or deny.
Only the user can approve. **Connection approved** means consent was accepted;
the client must still finish its handoff and make an authenticated memory request.
Only then report **Connected to Substrate.** Empty authenticated search results
are valid. Never request API keys, manual tokens or client secrets.

## Cowork actions the agent cannot automate

1. Open **Cowork → Customize → Plugins → Personal plugins → + → Add marketplace**.
2. Choose **Add from a repository (GitHub URL)** and enter the repository URL above.
3. Choose **Browse**, then install `substrate-mcp`.
4. Confirm the endpoint and **Streamable HTTP** transport above.
5. Complete the common Substrate browser flow, then verify from the agent.

For another MCP-capable host, use its documented configuration interface instead
of these Cowork menu names. Provide precise steps if it cannot edit its own MCP
settings. Do not bypass client permissions or create a manual-token alternative.

For an expired link, return to the agent and start connecting again. On a
transient error, preserve existing settings and retry; never claim success before
verification. This package has no hooks or full-transcript capture guarantee.
