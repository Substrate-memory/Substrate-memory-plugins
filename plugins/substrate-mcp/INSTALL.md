# Install Substrate memory in Cowork

The exact install request is:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

This package configures Cowork to use the Substrate backend's MCP endpoint and
browser OAuth. It does not install a server on the user's machine. Check the
endpoint and report success only after an authenticated memory smoke test.

## Preferred automated path

1. Confirm the active host is Cowork. If the host is unknown, ask the user
   which host they want; do not guess.
2. Add the repository as a Cowork/Claude marketplace and install
   `substrate-mcp` from the current reviewed release. Use the host's native
   automation. Do not ask for a repository path, API key, token, client
   secret, or manual credential configuration.
3. Let the browser OAuth flow open. Show the exact consent URL if the host
   returns one. The user must approve in their browser. Never approve it for
   them and never request a pasted secret.
4. Start a new turn and call the exposed memory recall tool (normally
   `memory_search`). A successful authenticated response, including an empty
   result set, is required before reporting success.
5. If the smoke test fails, report the safe error class and leave the previous
   configuration unchanged. Do not claim that installation succeeded.

## Other MCP-capable hosts

If the active host is not Cowork, ask which host is intended and use its native
remote **Streamable HTTP** MCP configuration. Set the server URL to
`https://app.trysubstrate.co/mcp`, use browser OAuth, and run an authenticated
recall smoke test. Do not assume a marketplace, create a local server, or ask
for manual credentials.

## Exact Cowork UI fallback

If native marketplace automation is unavailable:

1. Open the **Cowork** tab.
2. Choose **Customize → Plugins → Personal plugins → + → Add marketplace**.
3. Choose **Add from a repository (GitHub URL)** and enter
   `https://github.com/Substrate-memory/Substrate-memory-plugins`.
4. Choose **Browse**, then install `substrate-mcp`.
5. In the plugin's MCP panel, confirm the URL is exactly
   `https://app.trysubstrate.co/mcp` and transport is **Streamable HTTP**.
6. Start a new chat/turn and run the memory recall tool. Complete browser OAuth
   consent if prompted, then rerun the tool and require an authenticated
   response before reporting success.

Do not create a local stdio server. Do not add hooks. Do not paste API keys,
OAuth tokens, or client secrets into Cowork or chat. The plugin does not promise
automatic full-transcript capture; use explicit memory writes when the host
provides them and when the user intends a durable write.
