---
description: Import past local conversations into Substrate. The script uploads them directly; nothing passes through the chat.
---

`SYNC` means `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/substrate_sync.py" --host claude`
(use `python` if `python3` is missing). Requires **Connected to Substrate.**
(`/substrate-claude:substrate-connect`). The current conversation is excluded
automatically: hooks already capture it. Conversations already imported are not
listed or counted again.

If the user has not chosen yet, run `SYNC --preview` and offer **Import all** /
**Let me pick** / **Not now** with its count and date range.

The import ticket is secret. It goes into a file, never into a Bash command, a
command's arguments, or your reply.

**Import all**
1. Call `memory_import_ticket` with `{"platform": "claude"}`. Keep its `ticket`
   and `mcp_url` values.
2. Use the Write tool to create a new file, `<data-dir>/import-ticket-<8 random
   letters>.txt`, where `<data-dir>` is the `--data-dir` value that `SYNC` uses.
   Its content is only the ticket string. Never reuse a file name.
3. Run, with `mcp_url` from that result:
   `SUBSTRATE_MCP_URL='<mcp_url>' SYNC --upload --all --background --ticket-file "<data-dir>/import-ticket-<8 random letters>.txt"`
   The script reads the file and deletes it straight away.
4. Say "Importing N conversations in the background." Then repeat
   `SYNC --status --wait 60` and give its `message` in one line each time until
   `done` is true. End with that summary line
   (`Imported N sessions (T turns): S stored, D duplicate, R rejected.`).
   Keep polling in the same turn; do not wait for the user.

**Let me pick**: run `SYNC --list`, show a short numbered list (date and title),
then upload the chosen ones the same way, with a new ticket file each time and
`--session <id>` (repeat it) instead of `--all`.

**Not now**: run `SYNC --record-decision no` and say: "OK. Ask me to import your
past conversations any time."

If the status says the ticket expired, get a new ticket and run the same upload
again with a new ticket file; conversations already imported are skipped. A Cowork
cloud session has no local history: say there is nothing to import here.
