# One Substrate connection experience

Start with this request in the agent you want to connect:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

## User-facing flow (Hermes and MCP)

Use the same sequence and setup messages for both integrations:

1. **Install.** The agent identifies its current host and configures the supported
   integration. Ask the user only if that host cannot be determined safely.
2. **Sign in.** Open the exact browser link returned by Substrate or the client's
   authorization flow. Both arrive at **Connect your agent to Substrate**. Reuse
   an existing Substrate session; otherwise sign in or create an account normally.
3. **Review.** Use the same connection-name field and requested-access list. Check
   the connection details against the agent being installed. A displayed name is
   not proof of identity. Do not hide the verification code or registered callback
   host when those details are supplied.
4. **Approve connection.** The user approves or denies in the browser. Never
   approve on the user's behalf. No API keys, manual tokens, or client secrets
   are requested, displayed, copied, or pasted.
5. **Finish in the agent.** The browser shows **Connection approved**, not a claim
   that installation is already complete. The client resumes automatically where
   supported; otherwise return to the agent. It completes authentication privately
   and runs an authenticated memory smoke test, normally `memory_search`.
6. **Verify.** An empty authenticated result is valid. Only after a successful
   request report **Connected to Substrate.** If a restart is required, perform or
   explain it first and verify in the new session before reporting success.

Use the same recovery language for either setup. For an expired or unavailable
link, return to the agent and start connecting again. For a denied request, say
that access was not granted. For a transient service error, preserve entered
fields and retry safely; do not substitute a manual-token flow. A repository
checkout or release label is not evidence that the live connection works.

## Installation mechanics (not separate onboarding)

These are client-specific preparation steps, not different Substrate sign-in flows.
Do not select Hermes merely because a Hermes executable exists elsewhere on the
machine. Target the agent currently handling the installation request.

### Hermes

Use the golden `plugins/substrate` plugin and its published v0.5.0 install pin.
Follow its README for the standard-library login CLI and supported host version.
Do not change its runtime, inspect another profile, upgrade Hermes automatically,
or replace its working device authorization with a custom OAuth client. Wait for
browser approval, let the plugin obtain/store credentials privately, then verify.
Keep existing memory configuration unchanged until a memory request succeeds.

### Cowork

Use the thin `plugins/substrate-mcp` package. Configure the remote Streamable HTTP
endpoint `https://app.trysubstrate.co/mcp` through the client's supported interface.
Do not install another server or add capture hooks.

Automate marketplace setup when the host permits it. Otherwise give these exact
instructions, without asking for credentials:

1. Open the **Cowork** tab.
2. Choose **Customize → Plugins → Personal plugins → + → Add marketplace**.
3. Choose **Add from a repository (GitHub URL)** and enter the repository URL above.
4. Choose **Browse**, then install `substrate-mcp`.
5. Confirm the server URL above and **Streamable HTTP** transport.
6. Complete the same Substrate sign-in, review, approval and verification sequence.

Required client installation/permission dialogs cannot be bypassed by a repo prompt.
The user must perform steps the client does not let the agent automate.

### Other MCP-capable agents

Use the same endpoint and the host's native remote Streamable HTTP/browser OAuth
support. Discover supported configuration commands instead of inventing them.
If the host cannot edit its own configuration, provide precise instructions for
that host and then return to the same Substrate connection flow. A client with
only local stdio or incompatible OAuth is not automatically supported; never work
around that limitation by asking the user for a token.

## Implementation boundary

The Substrate browser screen, consent model and account/entitlement checks are
shared. Hermes device polling and MCP authorization-code/PKCE callbacks remain
protocol adapters underneath. Their proof details and client-controlled handoff
can differ, but there is no separate product onboarding journey.

This common setup does not claim identical memory automation: Hermes retains its
durable automatic capture. MCP clients use explicit/best-effort tool invocation.
