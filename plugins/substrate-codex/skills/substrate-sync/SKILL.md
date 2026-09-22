---
name: substrate-sync
description: Fill gaps in the current session by importing unsaved local Codex turns into Substrate.
---

# Substrate sync (catch-up for this session)

Use this when turn context ends with the `[substrate] ... not saved yet` line,
or when the user invokes `$substrate-sync`.

1. Call `memory_import_status` with this session id. Note `message_high_water`.
2. Run the bundled script (it only reads local files, never the network):
   `python3 ${PLUGIN_ROOT}/scripts/substrate_sync.py --host codex --session <id> --after-index <message_high_water> --origin catchup`
   It prints one JSON batch per line (at most 64 items / 240 KiB each).
3. Pass each printed batch to `memory_import` and report the summary line
   (for example `Imported 12 items (10 stored, 2 duplicate).`).
4. If the script reports sessions the server already holds, skip them.
5. Never print or send credentials. If no local transcript exists, say so
   and stop.
