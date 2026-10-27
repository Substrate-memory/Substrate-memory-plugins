---
name: substrate-import
description: Import past Codex conversations into Substrate. The script uploads them directly; nothing passes through the chat.
---

# Substrate import (history)

`SYNC` as in `$substrate-connect`. Requires **Connected to Substrate.** The
current conversation is excluded automatically (hooks capture it live). A
`[substrate] Offer this once` note is not for this plugin: ignore it.

If the user has not chosen yet, run `SYNC --preview` and offer **Import all** /
**Let me pick** / **Not now** with its count and date range.

**Import all**
1. Run `SYNC --new-claim` and keep the `claim` it prints. Call
   `memory_import_ticket` with `{"platform": "codex", "claim": "<claim>"}`. If
   that call is refused because of the `claim` field, call it again with only
   `{"platform": "codex"}`. The ticket is secret: never write it in a command or
   in your reply.
2. If the result has `claim_pending`, run, with its `mcp_url`:
   `SYNC --upload --all --background --claim "<claim>" --mcp-url "<mcp_url>"`.
   The script exchanges the claim for the ticket itself.
   If the result has a `ticket` instead (an older server), use the file-write
   tool to create `<data-dir>/import-ticket-<8 random letters>.txt` (`<data-dir>`
   is the `--data-dir` value that `SYNC` uses) containing only the ticket string,
   then run `SYNC --upload --all --background --ticket-file "<that file>" --mcp-url "<mcp_url>"`.
   The script reads the file and deletes it at once.
   This command needs network (it makes the claim exchange and the upload): run it
   with escalated permissions from the first try, as with the install curl. The
   user approves. `SYNC --new-claim`, `--preview` and `--status` are local.
   If the command prints `network_blocked`, rerun it with escalated permissions.
   If the command prints `claim_timeout` or `server_unavailable`, the server did
   not answer: nothing was uploaded. Say so in one line and ask whether to try
   again later. Do not retry on your own.
3. Say "Importing N conversations in the background." Then repeat
   `SYNC --status --wait 60` and give its `message` in one line each time until
   `done` is true; end with that summary line: it says how many conversations were
   imported and that Substrate is reading them now. Say it as written.
   Keep polling in the same turn; do not wait for the user.

**Let me pick**: `SYNC --list`, show a short numbered list (date and title),
then upload the chosen ones the same way with `--session <id>` (repeat it)
instead of `--all`.

**Import again** (only after the `memory_import_status` check in `$substrate-connect`
found that this account has none of the earlier imports): `SYNC --preview --force`
for the count, then Import all with `--force` added to the upload command in step 2.

**Not now**: `SYNC --record-decision no`; say: "OK. Ask me to import your past
conversations any time."

If the status says the ticket expired, get a new ticket and run the same upload
again. If the upload reports `claim_failed` (the claim was used, expired, or the
server restarted), retry once: run `SYNC --new-claim` again, call
`memory_import_ticket` with the new claim, and run the upload once more.
Finished conversations are skipped.
Plugins installed on the web have no local scripts or history: say there is
nothing to import there.
