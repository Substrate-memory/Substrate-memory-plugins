---
description: Check Substrate MCP wiring and authentication before installing or reconnecting (fallback package).
---

Run the Substrate self-check before changing configuration:

1. If `memory_search` is available, call it with a focused, non-secret query. An empty result is valid. If the authenticated call succeeds, report exactly: **Connected to Substrate.**
2. If the plugin/server is configured but tools are absent, say that it is installed but not wired into this session. Tell the user to use the host's documented restart or session action. Then give this exact prompt for the next session:

   ```text
   Verify my Substrate connection with memory_search and report Connected to Substrate.
   ```
3. If nothing is configured, tell the user to install the package through the host's documented interface and then return to the common six-step flow. (Claude, Codex/ChatGPT, and Hermes users should install their native package instead of this fallback.)
4. If authentication is required, show the exact browser URL returned by the client. The user signs in, reviews the request, and chooses **Approve connection**. Never ask for or accept an API key, token, secret, or pasted credential.

Do not report **Connection approved** as **Connected to Substrate.** without a successful authenticated smoke call.

5. After the first successful smoke call, ask exactly once: "Do you want to import past conversations into Substrate? I will show you the sessions I can read on this host, and import only what you confirm." Import only confirmed sessions as raw, redacted turns via `memory_import` (or confirmed facts via `memory_remember` where the host cannot replay turns). Nothing is written before confirmation. Never import secrets. If the user says no, do not ask again.
