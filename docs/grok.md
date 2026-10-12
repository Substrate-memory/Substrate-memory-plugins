# Connect Grok to Substrate

Grok has no Substrate package. Connect it as a generic remote MCP server through the Grok CLI. The server is `https://app.trysubstrate.co/mcp` (Streamable HTTP, OAuth). You approve the connection in your browser. No API key or token is needed.

## Grok CLI

1. Add the server:

   ```sh
   grok mcp add --transport http substrate https://app.trysubstrate.co/mcp
   ```

2. On first use, the CLI opens a browser for OAuth. Sign in to Substrate if asked. Check the connection name and security details, then choose **Approve connection**. The CLI stores its tokens in `~/.grok/mcp_credentials.json`.

3. Check the setup:

   ```sh
   grok mcp doctor substrate
   ```

   In the Grok TUI, `/mcps` opens the MCP tab. Select the Substrate server and press `i` to authenticate it again.

4. Ask Grok to run `memory_search`. Report **Connected to Substrate.** only after that call succeeds.

## Not supported yet

- **xAI API remote MCP tool.** The API accepts only a static token in its `authorization` field. Substrate does not issue static tokens or API keys, so the API cannot connect today. Use the Grok CLI.
- **grok.com connectors.** Not verified for Substrate. Use the Grok CLI.

## What this connection does

- Recall and writes are best effort. Ask Grok to search memory before it relies on a fact.
- Turns are not captured automatically. This is the MCP fallback, not a native plugin with hooks.
- Revoke the connection from the Substrate Agents page at any time.

See the [install guide](installation.md) for the shared approval flow and the [substrate-mcp README](../plugins/substrate-mcp/README.md) for the full fallback package.
