---
description: Check Substrate wiring and authentication before installing or reconnecting.
---

Run the Substrate self-check before changing configuration:

1. If `memory_search` is available, call it with a focused, non-secret query. An
   empty result is valid. If the authenticated call succeeds, report exactly:
   **Connected to Substrate.**
2. If the plugin/server is configured but tools are absent, say that it is
   installed but not wired into this session. For Cowork, tell the user to start
   a new session because plugin MCP servers register at session start. Otherwise
   tell the user to reload plugins (`/reload-plugins`) or restart the session.
   Then give this exact prompt for the next session:

   ```text
   Verify my Substrate connection with memory_search and report Connected to Substrate.
   ```
3. If nothing is configured, tell the user to install the package through the
   host's documented interface and then return to the common six-step flow.
4. If authentication is required, show the exact browser URL returned by the
   client. The user signs in, reviews the request, and chooses **Approve connection**.
   Never ask for or accept an API key, token, secret, or pasted credential.

Do not report **Connection approved** as **Connected to Substrate.** without a
successful authenticated smoke call.

5. After the first successful smoke call, ask exactly once: "Do you want to
   import past conversations into Substrate?" If yes, use
   `/substrate-claude:substrate-import`. If the user says no, do not ask again.
