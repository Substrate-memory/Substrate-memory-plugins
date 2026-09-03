# Complete Substrate setup

The plugin files are installed. The installing agent must finish setup for the same active
Hermes profile. Run this block and keep the setup process open while authorization is
pending:

```sh
profile_home="${HERMES_HOME:-$(dirname "$(hermes config path)")}"
HERMES_HOME="$profile_home" python3 "$profile_home/plugins/substrate/setup.py" \
  --hermes-home "$profile_home"
```

If setup prints `verification_uri_complete`, show that exact URL to the user and ask them
to approve it. Do not expose or request an access token. Continue only after setup prints
`"status": "ready"`.

Then complete the cutover:

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
Hermes profile. `setup.py` adds only the bundled public ISRG roots to normal system trust.
