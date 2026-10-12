# Connect OpenClaw to Substrate

OpenClaw has no Substrate package. It connects as a generic remote MCP server, the same path as any other MCP client. The server is `https://app.trysubstrate.co/mcp` (Streamable HTTP, OAuth). You approve the connection in your browser. No API key or token is needed.

## Steps

1. Add the server to your OpenClaw config under `mcp.servers`:

   ```json5
   {
     mcp: {
       servers: {
         substrate: {
           url: "https://app.trysubstrate.co/mcp",
           transport: "streamable-http",
           auth: "oauth",
         },
       },
     },
   }
   ```

2. Sign in:

   ```sh
   openclaw mcp login substrate
   ```

   Follow the printed authorization URL. OpenClaw registers its own OAuth client automatically.

3. In the browser, sign in to Substrate if asked. Check the connection name and security details, then choose **Approve connection**.

4. Check the connection:

   ```sh
   openclaw mcp doctor substrate --probe
   ```

   The probe lists the Substrate tools, including `memory_search`. Ask OpenClaw to run `memory_search`. Report **Connected to Substrate.** only after that call succeeds.

## If login doesn't come back

- **Running behind a Gateway:** set `gateway.publicOrigin` to the Gateway's external HTTPS origin. The OAuth provider redirects to `<publicOrigin>/oauth/mcp/callback`, so a wrong origin means the redirect never reaches OpenClaw. HTTP is accepted only for `localhost`, `127.0.0.1` or `[::1]` during local development.
- **Browser on another machine, or it cannot reach the printed loopback address:** copy the code the browser returns and run `openclaw mcp login substrate --code <code>`.

The steps follow OpenClaw's documented remote MCP setup. If your OpenClaw version differs, run `openclaw mcp --help`.

## What this connection does

- Recall and writes are best effort. Ask OpenClaw to search memory before it relies on a fact.
- Turns are not captured automatically. This is the MCP fallback, not a native plugin with hooks.
- Revoke the connection from the Substrate Agents page at any time.

See the [install guide](installation.md) for the shared approval flow and the [substrate-mcp README](../plugins/substrate-mcp/README.md) for the full fallback package.
