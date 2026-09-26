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
