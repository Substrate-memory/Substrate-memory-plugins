# Substrate memory is installed

Connect it now and keep going without stopping: do every step below in a row,
and pause only where the user has to act (the browser approval and the
import question). Never end a reply with "let me know when you are done".

1. If the plugin is not enabled yet (it is when installed with `--enable`):
   `hermes plugins enable substrate`. If the gateway runs this chat, the plugin
   loads after `hermes gateway restart` (ask the user first); the steps below
   work either way.
2. Start the sign-in:

   ```sh
   python "$(dirname "$(hermes config path)")/plugins/substrate/onboard.py" start
   ```

3. Show the user the printed link and code in one short message: open the
   link, sign in, check the code, choose **Approve connection**.
4. Right after that message, without waiting for the user to reply, run the
   wait command so approval is detected by itself (the user never has to type
   "approved"). Prefer a background run that wakes you when it finishes:
   `terminal(command="python .../onboard.py poll", background=true, notify_on_complete=true)`;
   otherwise run it in the foreground:

   ```sh
   python "$(dirname "$(hermes config path)")/plugins/substrate/onboard.py" poll
   ```

   It prints **Connected to Substrate as you@example.com.** (your account).
5. In the same reply, offer the import of past conversations. `poll` ends with
   the exact question and commands when this profile has earlier
   conversations: ask *Import all, let me pick, or not now?*, then run
   `onboard.py import --yes` (all, in the background), `onboard.py import --list`
   followed by `onboard.py import --yes --session <id> ...` (pick), or
   `onboard.py import --no` (not now; never asked again). Report progress with
   `onboard.py import --status` and the result when it is done.

Once the plugin is loaded, the same flow also runs in chat by itself: when the
profile is not connected, the next message shows the link and code; after the
user approves, the next reply says **Connected to Substrate as you@example.com.**
and asks the import question in the same reply (tool `substrate_import`).
Automated runs (cron, batch, subagents), group chats and the current
conversation are never imported; imported text is redacted like live turns.
Never paste an API key into chat.

Tested on Hermes 0.21.0 or newer. Other versions should work; if something
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
