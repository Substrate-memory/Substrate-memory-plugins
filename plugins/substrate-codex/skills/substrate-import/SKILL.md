---
name: substrate-import
description: Import past Codex conversations into Substrate. The script uploads them directly; nothing passes through the chat.
---

# Substrate import (history)

`SYNC` as in `$substrate-connect`. Requires **Connected to Substrate.** The
current conversation is excluded automatically (hooks capture it live).

If the user has not chosen yet, run `SYNC --preview` and offer **Import all** /
**Let me pick** / **Not now** with its count and date range.

**Import all**
1. Call `memory_import_ticket` with `{"platform": "codex"}`. Keep its `ticket`
   and `mcp_url` values. The ticket is secret: never write it in a command or in
   your reply.
2. Use the file-write tool to create a new file, `<data-dir>/import-ticket-<8
   random letters>.txt` (`<data-dir>` is the `--data-dir` value that `SYNC` uses),
   whose content is only the ticket string. Then run, with `mcp_url` from that
   result: `SUBSTRATE_MCP_URL='<mcp_url>' SYNC --upload --all --background --ticket-file "<data-dir>/import-ticket-<8 random letters>.txt"`
   (PowerShell: `$env:SUBSTRATE_MCP_URL='<mcp_url>'; SYNC --upload --all --background --ticket-file "<path>"`).
   The script reads the file and deletes it at once.
   The upload needs network access: if the sandbox blocks it, rerun the same
   command with escalated permissions (the user approves).
3. Say "Importing N conversations in the background." Then repeat
   `SYNC --status --wait 60` and give its `message` in one line each time until
   `done` is true; end with that summary line
   (`Imported N sessions (T turns): S stored, D duplicate, R rejected.`).
   Keep polling in the same turn; do not wait for the user.

**Let me pick**: `SYNC --list`, show a short numbered list (date and title),
then upload the chosen ones the same way with `--session <id>` (repeat it)
instead of `--all`.

**Not now**: `SYNC --record-decision no`; say: "OK. Ask me to import your past
conversations any time."

If the status says the ticket expired, get a new ticket and run the same upload
again; finished conversations are skipped. Plugins installed on the web have no
local scripts or history: say there is nothing to import there.
