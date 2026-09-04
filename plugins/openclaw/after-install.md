# Complete Substrate setup (OpenClaw)

No manual credential step is required. The plugin runs device onboarding by
itself on the first turn or first `memory_search` call: it shows a browser
approval link through the agent, and after approval the tenant-scoped key is
stored privately in the active OpenClaw home's `.env`. Never ask the user to
paste a key into chat.

Optionally, to connect during installation instead of on first use, run:

```sh
python3 ./Substrate-memory-plugins/plugins/openclaw/setup.py
```

If it prints `verification_uri_complete`, show that exact URL to the user and
ask them to approve it in a browser. Do not expose or request an access token.

Then verify in a new agent turn by calling `memory_search`. Report success
only when `memory_search` reaches the authenticated Substrate API.

To roll back: `openclaw plugins disable substrate`, remove the
`plugins.entries.substrate` config block, restart the gateway, and optionally
delete `<openclaw-home>/substrate/` to forget the credential.

Do not disable TLS verification, install a private certificate, or inspect a
different profile. The plugin supplements system trust only with the bundled
public ISRG roots.
