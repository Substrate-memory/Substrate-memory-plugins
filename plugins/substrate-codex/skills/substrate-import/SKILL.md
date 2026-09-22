---
name: substrate-import
description: Import past Codex conversation history into Substrate after explicit user approval.
---

# Substrate import (history)

Use this after **Connected to Substrate.**, at most once per user unless asked
again, or when the user invokes `$substrate-import`.

1. Ask once: "Do you want to import past conversations into Substrate?"
   If the user says no, do not ask again.
2. On yes, list readable local sessions:
   `python3 ${PLUGIN_ROOT}/scripts/substrate_sync.py --host codex --list`
   (reads `$CODEX_HOME/sessions/**/*.jsonl`; honours `CODEX_HOME`).
   Show the list and ask which sessions to import.
3. For each confirmed session, run the script with `--origin history_replay`
   and a fresh `batch_id`, one JSON batch per line:
   `python3 ${PLUGIN_ROOT}/scripts/substrate_sync.py --host codex --session <id> --origin history_replay`
4. Pass each batch to `memory_import`, then report `memory_import_status`
   for the batch.
5. Never import secrets. Never print credentials. Report what was stored.
