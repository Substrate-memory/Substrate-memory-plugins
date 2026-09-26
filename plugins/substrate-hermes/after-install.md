# Substrate memory is installed

Connect it now (one browser approval, no API key):

1. Start the sign-in and show the user the printed link and code:

   ```sh
   python "$(dirname "$(hermes config path)")/plugins/substrate/onboard.py" start
   ```

2. The user opens the link, signs in, checks the code, and chooses
   **Approve connection**.
3. Wait for approval. It prints **Connected to Substrate as <account>.**

   ```sh
   python "$(dirname "$(hermes config path)")/plugins/substrate/onboard.py" poll
   ```

4. Enable and load the plugin if it is not yet active:
   `hermes plugins enable substrate`, then `hermes gateway restart` (with the
   user's agreement, if the gateway is running this chat).

After a restart, the plugin also shows the link and code in chat by itself
on the next message when it is not connected yet. Never paste an API key
into chat.

Tested on Hermes 0.21.0-0.21.x. Other versions should work; if something
does not, tell us at https://github.com/Substrate-memory/Substrate-memory-plugins/issues.

## Upgrading from the old `substrate_wiki` provider

Only after a memory call succeeds, complete the cutover:

```sh
profile_home="${HERMES_HOME:-$(dirname "$(hermes config path)")}"
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
  # 'memory off' disables the external provider only; built-in local memory stays.
  hermes memory off
fi
hermes gateway restart
trap - ERR INT TERM
rm -f -- "$config_backup"
```

In a new agent turn, call `memory_search`. Do not use the legacy `wiki_search` tool.
Report success only when `memory_search` reaches the authenticated Substrate API.

Do not disable TLS verification, install a private certificate, or inspect a different
Hermes profile.
