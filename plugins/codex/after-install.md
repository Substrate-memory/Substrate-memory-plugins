# Complete Substrate setup for Codex CLI

No manual credential step is required. The plugin runs device onboarding by
itself on the first turn or first `memory_search` call: it shows a browser
approval link through the agent, and after approval the tenant-scoped key
is stored privately in the active Codex home. Never ask the user to paste a
key into chat.

Optionally, to connect during installation instead of on first use, run:

```sh
codex_home="${CODEX_HOME:-$HOME/.codex}"
python3 "$checkout/plugins/codex/setup.py" --codex-home "$codex_home"
```

If it prints `verification_uri_complete`, show that exact URL to the user and ask them
to approve it in a browser. Otherwise, simply call `memory_search` once: if the result reports
`authorization_required`, show the exact `verification_uri_complete` URL to
the user and ask them to approve it in a browser. Do not expose or request
an access token.

Then verify in a new agent turn:

```sh
codex mcp get substrate
```

and call `memory_search`. Report success only when `memory_search` reaches
the authenticated Substrate API.

Do not disable TLS verification, install a private certificate, or inspect
a different Codex profile. The plugin supplements system trust only with
the bundled public ISRG roots. Roll back with `codex mcp remove substrate`.
