---
name: substrate-connect
description: Sign in to Substrate, verify, and offer to import past Codex conversations, in one go.
---

# Substrate connect

Run this end to end. Pause only for the user's browser approval and their
import choice; never end the turn just because a step finished.

`SYNC` means `python3 "${PLUGIN_ROOT}/scripts/substrate_sync.py" --host codex`
(`python` if `python3` is missing; if `${PLUGIN_ROOT}` is not filled in, the
plugin is under `$CODEX_HOME/plugins/cache/*/substrate-codex/*/`, default
`~/.codex`). A `[substrate] Import offer pending` note gives the exact command.

1. **Connected already?** Call `memory_search` with a short, non-secret query.
   Any successful result, even empty: say **Connected to Substrate.**, run
   `SYNC --record-connected`, and go to step 4.
2. **Not installed?** ChatGPT/Codex app: Plugins → add the marketplace from the
   GitHub repo `Substrate-memory/Substrate-memory-plugins` → install
   **Substrate Memory** → new chat. Codex CLI:
   `codex plugin marketplace add Substrate-memory/Substrate-memory-plugins` and
   `codex plugin add substrate-codex@substrate-marketplace`, then a new session.
   If it is installed, do not install it again.
3. **Sign in now.** If `memory_search` needs sign-in, run
   `codex mcp login substrate-memory` (it may need network permission; the
   user approves). Give the user the link in one line: *Open this link, sign
   in, and choose **Approve connection**.* The command returns once they
   approve; then rerun `memory_search`. Never ask for a token or key.
4. **Same reply: offer the import.** Run `SYNC --preview`. If `decision` is set
   or `sessions` is 0, stop. Otherwise ask once, in plain words:
   *I found N past conversations on this computer (T turns, FIRST to LAST).
   Import them into Substrate? **Import all** / **Let me pick** / **Not now***
   Act on the answer with `$substrate-import`.
5. Tell the user once to trust the Substrate hooks in `/hooks`; until then
   Codex skips automatic capture.

**Connection approved** is not **Connected to Substrate.**; only a successful
`memory_search` is.
