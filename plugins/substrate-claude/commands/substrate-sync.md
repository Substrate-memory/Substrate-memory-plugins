---
description: Sync unsaved turns of this session into Substrate memory.
---

Sync the current session's unsaved turns into Substrate. The sync script reads
only this host's own local transcripts, prints redacted `memory_import` items,
and never touches the network itself.

1. Call `memory_import_status` with this `session_id`. Read `message_high_water`.
2. Run the bundled script from the plugin directory:
   `python scripts/substrate_sync.py --host claude --session <session_id> --after-index <message_high_water> --origin catchup`.
   Honour `CLAUDE_CONFIG_DIR` when set; otherwise the script uses `~/.claude`.
3. Pass each printed line (one JSON batch) to `memory_import`, in order.
4. Report the tool's summary line to the user (for example
   `Imported 12 items (10 stored, 2 duplicate).`).
5. If the script reports `session_not_found`, say the local transcript is not
   readable on this host (for example a cloud session with no local file) and
   stop. Never invent turns.

Never print or paste credentials while syncing. The script already redacts
secrets; strip them from anything you type as well.
