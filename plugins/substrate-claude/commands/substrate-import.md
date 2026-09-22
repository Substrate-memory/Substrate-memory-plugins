---
description: Import past local conversations into Substrate memory after confirmation.
---

Import past conversations. Ask first, show what you found, and write nothing
until the user confirms.

1. The precondition is **Connected to Substrate.** (see
   `/substrate-claude:substrate-connect`). If the smoke test has not passed,
   run it first.
2. Run `python scripts/substrate_sync.py --host claude --list` from the plugin
   directory. Honour `CLAUDE_CONFIG_DIR` when set.
3. Show the session list to the user and ask which sessions to import. Import
   only the sessions the user confirms.
4. For each confirmed session, run the script with
   `--session <id> --origin history_replay` (no `--after-index`: import the
   whole session) and a fresh `batch_id`, then pass each printed batch to
   `memory_import` in order.
5. Call `memory_import_status` for the batch and report the result.

Rules: use only history this agent can read on this host. Never import secrets.
A cloud session with no local transcript file has nothing to import; say so and
stop. If the user declines, do not ask again.
