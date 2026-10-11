---
name: substrate-connect
description: Sign in to Substrate, verify, install the Substrate hooks in the Codex config, and offer to import past Codex conversations, in one go.
---

# Substrate connect

Run this end to end. Pause only for the user's browser approval and their
import choice; never end the turn just because a step finished.

`SYNC` means `python3 "${PLUGIN_ROOT}/scripts/substrate_sync.py" --host codex`
(`python` if `python3` is missing; if `${PLUGIN_ROOT}` is not filled in, the
plugin is under `$CODEX_HOME/plugins/cache/*/substrate-codex/*/`, default
`~/.codex`). A `[substrate] Import offer pending` note gives the exact command.
`HOOKS` means `python3 "${PLUGIN_ROOT}/scripts/codex_user_hooks.py"` with the
same fallbacks.

1. **Connected already?** Call `memory_search` with a short, non-secret query.
   Any successful result, even empty: say **Connected to Substrate.**, run
   `SYNC --record-connected`, and go to step 4.
2. **Not installed?** ChatGPT/Codex app: Plugins → add the marketplace from the
   GitHub repo `Substrate-memory/Substrate-memory-plugins` → install
   **Substrate Memory** → new chat. Codex CLI:
   `codex plugin marketplace add Substrate-memory/Substrate-memory-plugins` and
   `codex plugin add substrate-codex@substrate-marketplace`, then a new session.
   If it is installed, do not install it again.
3. **Sign in now.** The hooks and the sign-in call the `substrate-memory`
   server by name. If `codex mcp list` does not show `substrate-memory` (a
   local plugin install in Codex 0.154 does not register it), run
   `codex mcp add substrate-memory --url https://app.trysubstrate.co/mcp`; this
   is the endpoint in `mcp.json`. Then, if `memory_search` needs sign-in, run
   `codex mcp login substrate-memory` (it may need network permission; the
   user approves). Tell the user: *Open this link, sign in and choose
   **Approve connection**. I'll continue automatically.* The command returns
   once they approve; then rerun `memory_search` in the same turn. Never ask
   for a token or key. A bare "continue"/"done"/"ok" during setup just means
   carry on; don't comment on it.
4. **Same reply: install the Substrate hooks.** Run
   `python3 "${PLUGIN_ROOT}/scripts/codex_user_hooks.py" install`. It merges
   the Substrate hook entries into the user's Codex hook file
   (`$CODEX_HOME/hooks.json`, default `~/.codex/hooks.json`), leaves every other
   entry alone, keeps a one-time backup, and is safe to run again. Its JSON
   output says `"changed"`. If it says `"skipped"` (Codex already runs the
   plugin's own hooks), add nothing and say nothing about hooks. Otherwise tell
   the user this one sentence: *Substrate hooks were added to your Codex config;
   approve them once in `/hooks` so recall and capture start.* Until they
   approve, Codex skips automatic capture and recall still works when asked.
5. **Same reply: offer the import.** Run `SYNC --preview`. If `decision` is set
   or `sessions` is 0, stop. Otherwise ask once, in plain words:
   *I found N past conversations on this computer (T turns, FIRST to LAST).
   Import them into Substrate? **Import all** / **Let me pick** / **Not now***
   Act on the answer with `$substrate-import`.

**Connection approved** is not **Connected to Substrate.**; only a successful
`memory_search` is.

## Disconnect

To disconnect, or before uninstalling the plugin, run `HOOKS remove` first. It
removes exactly the Substrate entries (and their approval records in
`config.toml`), and nothing else. Then say: *Substrate hooks were removed from
your Codex config.* `codex plugin remove substrate-codex` does not remove them.
