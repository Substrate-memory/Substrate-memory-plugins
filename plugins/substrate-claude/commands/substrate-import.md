---
description: Import past local conversations into Substrate. The script uploads them directly; nothing passes through the chat.
allowed-tools: Bash(python3 "*/scripts/substrate_sync.py" *) Bash(python3 */scripts/substrate_sync.py *) Bash(python "*/scripts/substrate_sync.py" *) Bash(python */scripts/substrate_sync.py *) Bash(/usr/bin/python3 "*/scripts/substrate_sync.py" *) Bash(/usr/bin/python3 */scripts/substrate_sync.py *) Bash(/usr/bin/python "*/scripts/substrate_sync.py" *) Bash(/usr/bin/python */scripts/substrate_sync.py *) Bash(/usr/local/bin/python3 "*/scripts/substrate_sync.py" *) Bash(/usr/local/bin/python3 */scripts/substrate_sync.py *) Bash(/opt/homebrew/bin/python3 "*/scripts/substrate_sync.py" *) Bash(/opt/homebrew/bin/python3 */scripts/substrate_sync.py *) ToolSearch mcp__plugin_substrate-claude_substrate-memory__memory_import_ticket mcp__plugin_substrate-claude_substrate-memory__memory_import_status mcp__plugin_substrate-claude_substrate-memory__memory_search
---

`SYNC` means `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/substrate_sync.py" --host claude`
(use `python` if `python3` is missing), in exactly that form: no full interpreter
path, no extra options before the script. Requires **Connected to Substrate.**
(`/substrate-claude:substrate-connect`). The current conversation is excluded
automatically: hooks already capture it. Conversations already imported are not
listed or counted again.

If the user has not chosen yet, run `SYNC --preview` and offer **Import all** /
**Let me pick** / **Not now** with its count and date range.

The import ticket is secret: it never goes into a Bash command, a command's
arguments, or your reply. The claim (from `--new-claim`) is single-use and expires
after 10 minutes.

**Import all**
1. Run `SYNC --new-claim` and keep the `claim` it prints. Call
   `memory_import_ticket` with `{"platform": "claude", "claim": "<claim>"}`. If
   that call is refused because of the `claim` field, call it again with only
   `{"platform": "claude"}`: the result then has a `ticket`.
2. If the result has `claim_pending`, run, with its `mcp_url`:
   `SYNC --upload --all --background --claim "<claim>" --mcp-url "<mcp_url>"`.
   The script exchanges the claim for the ticket itself; the ticket never
   reaches you. A claim works once and expires after 10 minutes.
   If the result has a `ticket` instead (an older server), use the Write tool to
   create a new file, `<data-dir>/import-ticket-<8 random letters>.txt`, where
   `<data-dir>` is the `--data-dir` value that `SYNC` uses. Its content is only
   the ticket string. Then run `SYNC --upload --all --background --ticket-file
   "<that file>" --mcp-url "<mcp_url>"`. The script reads the file and deletes it
   straight away.
   If the command prints `claim_timeout` or `server_unavailable`, the server did
   not answer: nothing was uploaded. Say so in one line and ask whether to try
   again. Do not retry on your own, and do not wait on the server.
3. Say "Importing N conversations in the background." Then repeat
   `SYNC --status --wait 60` and give its `message` in one line each time until
   `done` is true. End with that summary line: it says how many conversations were
   imported and that Substrate is reading them now. Say it as written; do not add
   counts that the line does not give.
   Keep polling in the same turn; do not wait for the user.

**Import again** (only after the account note in `/substrate-claude:substrate-connect`
found that this account has none of the earlier imports): run `SYNC --preview --force`
for the count, then do **Import all** with `--force` added to the upload command in
step 2 (`SYNC --upload --all --force --background ...`).

**Let me pick**: run `SYNC --list`, show a short numbered list (date and title),
then upload the chosen ones the same way, with a new claim (or ticket file) each
time and `--session <id>` (repeat it) instead of `--all`.

**Not now**: run `SYNC --record-decision no` and say: "OK. Ask me to import your
past conversations any time."

If the status says the ticket expired, get a new ticket and run the same upload
again. If the upload reports `claim_failed` (the claim was used, expired, or the
server restarted), retry once: run `SYNC --new-claim` again, call
`memory_import_ticket` with the new claim, and run the upload once more.
Conversations already imported are skipped. A Cowork cloud session has no local
history: say there is nothing to import here.
