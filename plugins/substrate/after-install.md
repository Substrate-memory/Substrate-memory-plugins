# Complete Substrate setup

No manual credential step is required. The plugin runs device onboarding by itself
on the first turn or first `memory_search` call: it shows a browser approval link
through the agent, and after approval the tenant-scoped key is stored privately in
the active profile's `.env`. Never ask the user to paste a key into chat.

Optionally, to connect during installation instead of on first use, run:

```sh
profile_home="${HERMES_HOME:-$(dirname "$(hermes config path)")}"
HERMES_HOME="$profile_home" python3 "$profile_home/plugins/substrate/setup.py" \
  --hermes-home "$profile_home"
```

If it prints `verification_uri_complete`, show that exact URL to the user and ask them
to approve it in a browser. Do not expose or request an access token.

Then complete the cutover (only after a memory call succeeds):

```sh
config_path="$(hermes config path)"
config_backup="$(mktemp)"
cp -p -- "$config_path" "$config_backup"
rollback_substrate_cutover() {
  cp -p -- "$config_backup" "$config_path"
  rm -f -- "$config_backup"
  exit 1
}
trap rollback_substrate_cutover ERR INT TERM
hermes plugins enable substrate
if [ -d "$profile_home/plugins/substrate_wiki" ]; then
  hermes plugins disable substrate_wiki
fi
if [ "$(hermes config get memory.provider 2>/dev/null || true)" = "substrate_wiki" ]; then
  hermes memory off
fi
hermes gateway restart
trap - ERR INT TERM
rm -f -- "$config_backup"
```

In a new agent turn, call `memory_search`. Do not use the legacy `wiki_search` tool.
Report success only when `memory_search` reaches the authenticated Substrate API.

Do not disable TLS verification, install a private certificate, or inspect a different
Hermes profile. The plugin supplements system trust only with the bundled public ISRG
roots.
