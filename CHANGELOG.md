# Changelog

## 0.11.0

Install once, import everything local, stay current.

- **Codex: one approval line.** Setup ends with the link and one instruction that covers both the Codex app (its hooks settings) and the CLI (`/hooks`), then one new session. No "continue", no repeated instruction; the import is offered once, after the check.
- **Codex hook trust survives updates.** The hooks run a stable shim in `$CODEX_HOME/substrate/` that execs the newest installed plugin, so an update no longer needs re-approval (upgrading from 0.10.3 asks once more).
- **Import finds all local history.** Codex also reads `archived_sessions/`, every `rollout_path` in `state_*.sqlite` (read-only) and running WSL distros' `.codex`; a conversation's own rollout wins over its subagent threads. The preview shows counts per location and says cloud-only chats are not on this computer; Claude Code notes when its retention window already removed older transcripts.
- **Cloud chats:** agents point users at the official ChatGPT / Claude.ai export (dropped on Substrate → Sources) and never read cloud history through a browser.
- **Permissions:** Claude Code offers once to allow the memory tools except `memory_forget` (`--permissions-check/--permissions-allow/--decline`); Cowork gets the one place to set Always allow.
- **Updates:** a once-a-day "update available" line at session start (Claude/Cowork, Codex, Hermes; cached, 2 s timeout, silent on failure); install offers each host's auto-update; `VERSION` is the single source, checked by `scripts/check_versions.py`; Hermes README pin fixed.
- **Versions:** 0.11.0 everywhere.

## 0.10.3

Codex: no "continue" after the browser approval.

- **Hooks first, sign-in detached.** Setup installs the Codex hooks before sign-in (quiet until connected) and starts `codex mcp login substrate-memory` detached (`substrate_sync.py --host codex --signin-detach`), so the sign-in survives the end of the turn. The turn ends with the link and one line: approve, trust Substrate's hooks in `/hooks`, start a new session with `/new`.
- **The next session announces it.** A SessionStart hook says "Connected to Substrate." once and offers the import (no offer when there is nothing to import), or shows the pending link again if sign-in has not finished.
- **Versions:** 0.10.3 everywhere; `substrate-codex` and `substrate-claude` ship the same `substrate_sync.py` (byte-identical).

## 0.10.2

Codex: setup, import and sign-in work end to end (first live Codex test).

- **Network blocked is its own error.** When the sandbox cannot reach Substrate (DNS failure, refused connection, permission denied), `substrate_sync.py --upload` stops at once with `network_blocked` ("Network is blocked here. Rerun this command with network access.") instead of retrying for about 14 seconds and reporting `claim_timeout`. A server that accepts the connection and does not answer still gives `claim_timeout` / `server_unavailable`, as before.
- **Codex import runs with network access.** The import skill tells the agent to run the upload and claim command with escalated permissions from the first try.
- **Codex texts name Codex skills.** The import offer, setup, sign-in and stop-check texts use `$substrate-import`, `$substrate-connect` and `codex mcp login` on Codex hosts, not Claude Code skills or slash commands. Claude Code texts are unchanged.
- **Expired sign-in renews on the spot.** `$substrate-connect` checks `codex mcp list --json`; when Substrate shows `not_logged_in` or `memory_search` reports an expired sign-in, it says so in one sentence and runs `codex mcp login substrate-memory`.
- **Versions:** 0.10.2 everywhere; `substrate-codex` and `substrate-claude` ship the same `substrate_sync.py` (byte-identical).

## 0.10.1

Claude Code: setup keeps going after the sign-in link, and says the same thing about reconnects.

- **Setup continues by itself.** After the sign-in link is shown, a Stop hook holds the setup turn and tells the agent to keep polling until the plugin's own `memory_search` works. It stops at the first success, on "stop"/"cancel"/"not now", after 8 continuations or 10 minutes, and never blocks other turns or other sessions. Any error in the hook lets the turn end, as before.
- **One sign-in sentence.** The agent shows "Open this link, sign in and choose **Approve connection**. I'll continue automatically." with the link, and leaves out the authenticate tool's notes about error pages, localhost and pasting URLs.
- **Reconnect wording matches the computer.** A reconnect says "Reconnected to Substrate." (from `--record-connected`, which also knows about a sign-in on a computer that was connected before).

## substrate-claude 0.10.0

Claude Code and Cowork: one install line for the agent, no permission prompts for the plugin's own sync script, and an import ticket that never reaches the chat.

- **One install line (install.md).** The agent gets one prompt, "Install Substrate memory in this agent by following https://trysubstrate.co/install.md", and the same steps everywhere. The README points at `docs/install.md`.
- **No prompts for the sync script.** `/substrate-connect`, `/substrate-import` and `/substrate-sync` pre-approve only runs of the plugin's own `scripts/substrate_sync.py` (`allowed-tools`: `python3` or `python` followed by a quoted path ending in `/scripts/substrate_sync.py`). Other commands and file writes keep their prompts.
- **The ticket never reaches the agent.** `--new-claim` makes a claim, `memory_import_ticket` takes it and `--upload --claim` exchanges it for the ticket inside the script. A server that still returns the ticket directly keeps the file flow (`--ticket-file`).
- **Upload endpoint without a shell variable.** `--mcp-url` replaces `SUBSTRATE_MCP_URL=… ` in front of the command, so the pre-approved pattern matches. `SUBSTRATE_MCP_URL` still works.
- **Sign-in link.** The agent pastes the full sign-in link in its reply every time, and does not invent ports, callback addresses or remote-machine advice unless the user reports an error page.
- **No 20-minute hangs.** Every import step has a time budget. A server that accepts the connection and never answers ends the claim in about 25 seconds, and the handshake in about 45, with a clear message (`claim_timeout`, `server_unavailable`). Nothing is uploaded in that case.
- **Another account.** The import state is kept per computer, not per account. When this account has none of the conversations this computer imported earlier, the agent says so and can import them again (`--preview --force`).
- **Versions:** `substrate-claude` 0.10.0 (manifest and marketplace). `substrate-codex` 0.10.0 ships the same `substrate_sync.py` (byte-identical).

## substrate-codex 0.10.0

Codex: the hooks are installed before you are asked to approve them, the import offer is part of the same reply, and the ticket flow matches Claude Code.

- **Hook order.** After sign-in the agent runs the hook installer at once, offers the import, and only then asks you to approve the hooks in `/hooks`. In the new session, "continue" is enough.
- **Import.** The same claim-based upload as substrate-claude 0.10.0, with the file fallback for older servers.
- **Versions:** `substrate-codex` 0.10.0 (manifests and marketplace).

## substrate-hermes 0.10.0

Hermes: a reconnect keeps the same agent when the server supports it.

- **Reconnect (same agent).** The device login sends the last revoked credential as `reconnect_token`, so an approval in the same tenant reuses the agent (name, avatar, import history). The token is kept owner-private and removed once a new connection succeeds. A server that rejects the extra field (400) gets the same request again without it, so sign-in always works.
- **Versions:** `substrate-hermes` 0.10.0, matching the repository `VERSION`.

## substrate-mcp 0.10.0

Fallback MCP package: after **Connected to Substrate.**, the agent offers once to save what it already knows about you.

- **What the agent already knows.** The offer is "I can save what I already know about you to Substrate. Want me to?" On yes the agent lists people, projects, decisions, preferences and open items, and imports them after you confirm, as one document (`memory_import`). On a server that rejects documents it saves the confirmed facts with `memory_remember`.
- **Versions:** `substrate-mcp` 0.10.0 (manifest and marketplace).

## Repository 0.10.0

- Redaction catches more secrets before they are stored: names such as `OPENAI_API_KEY`, `AWS_SECRET_ACCESS_KEY`, `DB_PASSWORD` and `GITHUB_TOKEN`, key shapes (OpenAI/Anthropic `sk-`, AWS `AKIA`, GitHub, Slack, Stripe, Google, JWT, PEM private keys) and passwords in connection strings. The same rules run in every plugin and on the server (contract section 6).
- `docs/install.md` is the agent-facing install guide (served at `trysubstrate.co/install.md`). README and `docs/installation.md` are plugin-first; stale version labels are fixed.
- Known host limit: Claude Code shows red "MCP server not connected" or "Unauthorized" hook errors while signed out. The capture hooks are reported by Claude Code and the plugin cannot hide them.

## substrate-claude 0.9.2

Claude Code and Cowork: an honest connect check, a quiet note when the connection is removed, the import ticket kept out of commands, and a clearer import offer (CC-03, CC-05, CC-07, CC-09, CC-10).

- **Connected only means the plugin's server (CC-03).** The connect check and the setup prompt name the plugin's own tool, `mcp__plugin_substrate-claude_substrate-memory__memory_search`. Tools from a claude.ai connector (`mcp__claude_ai_*`) do not count. "Connected to Substrate." is said only after that call succeeds; otherwise the agent tells the user to finish Authenticate in `/mcp`.
- **A removed connection is one quiet line (CC-05).** When this computer's credential store lists sign-ins but none for Substrate, the offer hook adds one short note per session: the connection was removed, answer normally, and reconnect with `/substrate-claude:substrate-connect`. It is silent on macOS (keychain) and never blocks a prompt. The red "MCP server not connected" and "Unauthorized" lines come from the capture hooks, which Claude Code calls itself; the plugin cannot hide them.
- **The ticket never appears in a command (CC-07).** `substrate_sync.py --upload` reads the import ticket from `--ticket-file`: the agent writes it with the file-write tool to a new file in the data folder, the script makes it 0600 and deletes it at once. Background runs hand the ticket to the child on stdin. `SUBSTRATE_IMPORT_TICKET` is no longer read. `SUBSTRATE_MCP_URL` still carries the endpoint.
- **The offer states what was found (CC-09).** The offer hook computes the count, the turns and the date range and gives the agent the exact sentence and the three choices: Import all / Let me pick / Not now. "Not now" is remembered as before.
- **Imported conversations leave the list (CC-10).** `--preview`, `--list` and the offer count only sessions not yet imported. An upload records each session's digest and transcript fingerprint in `import-progress.json`; an unchanged session is recognised by its fingerprint, and a changed one by its upload items, so new turns still come back as pending. The preview reports `already_imported`.
- **Versions:** `substrate-claude` 0.9.2 (manifest and marketplace). `substrate-codex` 0.9.3 ships the same `substrate_sync.py` (the two copies stay byte-identical), with the same ticket and preview fixes; its import skill now uses `--ticket-file`.

## substrate-codex 0.9.3

Codex: the shared sync script changes described under substrate-claude 0.9.2 (ticket file, import offer counts, imported conversations skipped). The import skill passes the ticket with `--ticket-file`, never in the environment.

## substrate-hermes 0.9.1

Hermes: explicit memory searches survive a slow server, unknown Hermes builds get no version note, the install stops picking up dependency tooling, and the server's connected-source description reaches the model (customer test 2026-10-11).

- **Search survives slow servers (CON-04):** `memory_search`, `memory_expand` and `memory_evidence` are user-invoked, so they get a 15 s budget instead of 3 s (`SUBSTRATE_TOOL_TIMEOUT_SECONDS`, 3-60 s). The first attempt waits up to 7 s; one retry runs with what is left, and only for a timeout, a connection reset or a 5xx. A 4xx answer is never retried. When the retry is spent, the tool says "Substrate memory is busy or slow right now. Try again in a moment." instead of a bare `{"error":"timeout"}`, so the agent does not say it has no access to a source. The per-turn recall budget is 2.5 s (was 2 s) so it stays above the server's 1500 ms deadline with about a second of headroom.
- **No false version warning (HER-10):** a git build reporting `0.0.0` (or any unrecognised version) is unknown, not old, and gets no note. An older Hermes gets its one note when the connection is made, never on ordinary answers (it was repeated on answers before).
- **Faster install (HER-09):** the repository root carried a `pyproject.toml` (repository dev tooling). Hermes treats a root `pyproject.toml` as a plugin dependency project, so `hermes plugins install <repo URL>` installed dependencies (2 min 47 s in the customer test) although the plugin is standard-library only. The tooling project, its `uv.lock`, and its CI commands moved to `tooling/` (`uv sync --project tooling --frozen --extra dev`); pytest and ruff settings moved to root `pytest.ini` and `ruff.toml`. The plugin installs nothing. Lookups still take the Hermes tool-search step (HER-12, see the Hermes note in the README).
- **Server's search description (CON-03 follow-up):** the `memory_search` description now comes from the server's `tools/list` when present (it names the connected sources). It is fetched once per process in the background; the built-in text stays as the fallback. The built-in text is shorter and direct.
- **Permanent refusals:** `payload_too_large` is a permanent tool error, so an oversized capture is split or quarantined instead of retried forever.
- **Rejected items are logged with the reason:** when the server rejects an item in a capture or import batch, the sender logs its `error` and `hint` (bounded, no content) at WARNING on `substrate.spool`, not just a count.
- **Versions:** `substrate-hermes` 0.9.1, matching the repository `VERSION`.

## substrate-hermes 0.9.0

Hermes: past-conversation import is offered after connecting, direct import requests reach the import tool, offline writes report queued, and long sessions are captured within the server's limits.

- **Import offer:** after "Connected to Substrate", the agent asks once per connection whether to import earlier Hermes conversations (Import all / Let me pick / Not now). "Not now" is remembered in the profile. Import runs through `substrate_import` and the spool's `memory_import`; progress is reported. A direct request to import goes to `substrate_import`, never `memory_remember`.
- **Offline writes:** `memory_remember` and `memory_forget` that are spooled but could not reach Substrate return `status: "queued"` (it syncs automatically; do not retry). Only a write that was neither sent nor queued reports an error.
- **Long sessions (HER-08):** Substrate's redaction bound is 128 items per array, so a turn with more messages was refused and quarantined. Captures now send only new messages since the last enqueued one, in parts of at most 128 messages and 200 KiB. The local validator enforces the same bounds. A turn the server still refuses for size or shape is split (counter `split`), not quarantined.
- **Versions:** `substrate-hermes` 0.9.0, matching the repository `VERSION`.

## substrate-codex 0.9.2

Codex: the setup nag clears as soon as `codex mcp login` succeeds, without a model turn.

- **Why:** the UserPromptSubmit hook only learned about a connection when the model ran `--record-connected` after a working `memory_search`. With the model unable to run, "Substrate setup is not finished" was repeated on every prompt after a successful OAuth login (CDX-06).
- **What changed:** the hook checks Codex's own sign-in state (`codex mcp list --json`, `auth_status`, never the token) with a 2 s budget and records the connection itself. The same check runs for Claude Code (its `mcpOAuth` entry in `.credentials.json`, read only for existence).
- **Bounded nags:** the setup nag and the import offer each stop after 12 prompts while the connection is unrecorded, so a session that never gets to run the model no longer repeats them forever. Import offer decisions are unchanged.

## substrate-claude 0.9.1

Claude Code: the same connection check as substrate-codex 0.9.2 (CDX-06 pattern), and the same bounded nags. See that entry.

## substrate-codex 0.9.1

Codex: connect now installs the Substrate hooks, so automatic recall and capture start after one approval.

- **Why:** Codex 0.154 does not load a local plugin's hooks, so `/hooks` showed 0 for every event (CDX-04). Codex does load user hooks from `$CODEX_HOME/hooks.json`.
- **What connect does:** `$substrate-connect` runs `scripts/codex_user_hooks.py install` after a successful connect. It merges the plugin's hook table into your `hooks.json`, tags each entry (`x-substrate-managed`), keeps foreign entries untouched, backs the file up once, and writes atomically. The user approves the entries once in `/hooks`.
- **Disconnect:** `codex_user_hooks.py remove` removes exactly the tagged entries and their `config.toml` approval records.
- **Duplicates:** if Codex starts loading the plugin's own hooks, the installer removes its copy instead.

## 0.9.0

Right after connecting, the agent offers to bring your past conversations into Substrate, and does it without you babysitting it.

- **Offered once, in the same reply as "Connected to Substrate."** The agent counts the conversations on this computer and asks: *I found 87 past conversations on this computer (1,204 turns, 3 Mar to 7 Oct 2026). Import them into Substrate? Import all / Let me pick / Not now.* "Not now" is remembered and never asked again; you can still say "import my past conversations" any time. Previously the offer lived only inside the connect command, so connecting through Claude Code's own sign-in never offered it.
- **Import all, in the background.** Everything on this computer except the current conversation (already captured live) is imported while the agent keeps reporting progress (*Importing past conversations: 12 of 87 done*) and ends with `Imported 87 sessions (1204 turns): 1190 stored, 14 duplicate, 0 rejected.` No "done" to type.
- **Your history never passes through the chat.** Claude Code and Codex get a short-lived, import-only ticket from the new `memory_import_ticket` tool; the bundled `substrate_sync.py --upload` posts redacted batches straight to Substrate (60-minute ticket, import tools only, revoked with the connection, read from the environment, never shown in chat). Relaying a session through the model cost about 860 KB of context per conversation; now it costs one status line.
- **Robust uploads.** Batches respect 64 items / 240 KiB; very long agent turns (more than 64 tool calls, megabytes of tool output) are trimmed or split instead of rejected; 429 and server errors are retried with backoff; a refused batch is narrowed down to the one bad turn; an expired ticket stops cleanly and the next run skips finished conversations (the server ignores repeats).
- **Sign-in starts at once and continues by itself.** After a fresh install the only thing to type is `/reload-plugins` (then "continue"): the plugin's hook makes the agent finish setup on that message, open the sign-in link immediately, and keep checking while you approve in the browser.
- **One copy only.** If Claude Code already has `substrate-claude@synced` (synced from claude.ai), the agent signs it in instead of installing a second copy; "MCP server … not connected" errors mean "not signed in", not "not installed".
- **Offer hook.** A small local `UserPromptSubmit` command hook (`substrate_sync.py --offer-check`) reminds the agent until you have answered; afterwards it is silent. It never blocks a prompt and never touches the network. Codex: the connect skill makes the offer itself, because plugin hooks need trust in `/hooks` (and did not load in our sandboxed Codex check).
- **Hermes:** see below.
- **Imported history becomes recallable.** Each imported session now ends with one `capture_session` seal (`boundary: "end"`, `session_complete: true`), which is what makes the server materialize and extract it. Before, imports stored turns that were never extracted (also in 0.8.1). Reruns dedupe the seal; catch-up of the live session never seals.
- Claude subagent transcripts (`subagents/` folders, older top-level `agent-*.jsonl` files, and any `isSidechain` lines) are skipped; they carry the parent's session id and used to collide with it. Versions: `substrate-claude`, `substrate-codex`, `substrate-mcp` 0.9.0. `docs/mcp-contract.md` documents `memory_import_ticket` (4.9) and the new script modes (section 8).

## 0.8.1

Hermes sign-in keeps the user's approval.

- **Approval no longer lost to a slow first memory call.** After approval the plugin checks the new key with one authenticated `memory_search`. v0.8.0 allowed 5 seconds; the first call for a newly approved agent can take longer (the tenant opens cold), so the plugin discarded the issued key and its device code, marked the sign-in failed, and the next message started a new code. From the user's side: "I approved it" followed by another link. The check now allows 20 seconds, and a timeout or server error keeps the grant: the server hands the same key back on the next poll, so the plugin retries until it succeeds. Only a key the server actually refuses fails the sign-in.
- **"Approval received" instead of the link again.** While that check is still running, the agent is told the approval arrived and there is nothing more to do in the browser, and not to start a new sign-in.
- **One poller at a time.** The gateway's background poll, the turn itself and an agent-run `onboard.py` all polled the same grant. Close together, they tripped the server's 5-second `slow_down` window for each other, and two pollers redeeming one approval got `409 temporarily_unavailable`, which v0.8.0 treated as a failed sign-in. Polls are now serialized per profile with a lock file and spaced by the poll interval, and `temporarily_unavailable` / `rate_limited` mean "ask again".
- **"Done" connects on that message.** Every turn with a pending code checks with Substrate before replying, even when the background poll is mid-sleep, so the message sent right after approving connects instead of showing the same link.
- **A new code says why.** When a code expires or fails and the plugin starts another, the agent is told which earlier code ended and why, so it does not guess (for example, that the approval never reached Substrate). A grant that expires after approval is reported as a failed connection check, not as "expired before it was approved".
- **`--enable` really enables.** On current Hermes any `pyproject.toml` made the plugin a dependency workspace member: a non-interactive install (an agent running the command) refused the dependency question and left the plugin disabled, and enabling then failed because the repository's `[dev]` extras pin `pytest==9.0.3` against Hermes core's `pytest==9.1.1`. The plugin is standard-library only and now says so (`python_runtime: external`), so `hermes plugins install <repo URL> --enable` finishes enabled in one step.
- **Per-turn recall gets 2 seconds, not 0.5.** Half a second was below one authenticated round trip to the hosted server, so recall was almost always dropped. A chat turn's own model call takes seconds, so 2 seconds adds no felt delay.
- **Claude Code: no startup error.** Claude Code does not allow MCP tool hooks on `SessionStart` and showed "SessionStart:startup hook error" on every launch. The plugin declares none; the first turn marks the session start. Recall injection itself needed a server change (hook JSON), live on app.trysubstrate.co.
- **Claude Code: no helper noise, no dead end hook.** Claude Code fires `SubagentStop` for its own helpers too, so the compaction summary was stored in memory as an agent turn; real subagent results already arrive through the main agent's tool results, so the hook is gone. Claude Code also refuses MCP tool hooks on `SessionEnd`; that hook is gone and the server seals a session after 30 minutes idle.
- **Open-ended Hermes range.** Tested on Hermes 0.21.0 or newer. Newer versions no longer get a "tested on 0.21.x" note; only a Hermes older than 0.21.0 does. Nothing is ever refused.

## 0.8.0

Hermes install and sign-in fixed end to end.

- **Sign-in no longer fails with `invalid_response`.** v0.7.0 rejected the server's valid approval link whenever the plugin reached Substrate by an address other than `https://app.trysubstrate.co` (an old `SUBSTRATE_API_URL`, a stored address from an earlier install, a proxy or tailnet name). The server builds the link on its public address, so the whole login failed although the server had issued a code. The plugin now accepts the link when it is HTTPS (plain HTTP only for a loopback host), points at `/oauth/device`, and carries this grant's code, and tells the user which old setting to remove.
- **Connection check and memory tools match the current server.** The server answers `memory_search`, `memory_expand`, `memory_evidence`, `memory_remember` and `memory_forget` with `contract_version: 1` and offers `memory_shares` only when sharing is wired. v0.7.0 demanded `2` and all twelve tools, so even an approved login ended in `authenticated_health_check_failed` and memory calls in `invalid_response`. The plugin now accepts `1` or `2` for those tools and does not require `memory_shares` (it never calls it). `docs/mcp-contract.md` says so.
- **One-command install from the repository URL.** The repository root is now a Hermes plugin (`plugin.yaml` + `__init__.py`) that loads `plugins/substrate-hermes`: `hermes plugins install https://github.com/Substrate-memory/Substrate-memory-plugins --enable`. No more "not a valid plugin" warning. A root `onboard.py` gives one login path for both layouts.
- **Passes the Hermes install scan without an override.** Hermes 0.21.4 `plugin_guard` verdict for the repository root moved from CAUTION (89 findings, 1 high) to SAFE (0 high). The dynamic `__import__("os")` in `scripts/check_public_hygiene.py` is a normal import; docs and CI use `uv sync --frozen` + `.venv/bin/...` instead of `uv run`. Remaining medium findings are subprocess calls in tests and the optional Secret Service lookup (`secret-tool`).
- **Login in chat.** When the profile is not connected, the plugin starts the sign-in itself and gives the agent the link and code to show in chat. It finishes by itself after approval and the next turn says **Connected to Substrate as you@example.com.** (your account) (the server adds the account from its next release; older servers give **Connected to Substrate.**). Notices show the real `onboard.py` path, never `<plugin-dir>`.
- **Plain errors.** Every sign-in failure has a sentence saying what happened and what to do (`message`), next to the `error_class` code.
- **Universal version policy.** No package refuses a host version. Outside the tested range the user gets one friendly note (*Tested on Hermes 0.21.0-0.21.x; you are on X. It should work; tell us if not.*). The Hermes plugin shows it by itself; for the other hosts the installing agent follows the same rule. See `COMPATIBILITY.md#version-policy`.
- Verified end to end on a real Hermes 0.21.4 in an isolated profile: install by URL → enable → approval link → approve → memory write and recall.

## 0.7.0

One connection experience for every agent:

- The user pastes one request into their agent (`Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins`). The agent identifies its host and installs the right package: Claude Cowork and Claude Code → `plugins/substrate-claude`; ChatGPT Work, the Codex app and Codex CLI → `plugins/substrate-codex`; Hermes → `plugins/substrate-hermes`; any other MCP-capable agent → the thin `plugins/substrate-mcp` fallback, only when no package fits.
- Same flow everywhere: **Install → Sign in → Review → Approve connection → Connected to Substrate.** Success is reported only after an authenticated `memory_search`. No API keys, tokens, or secrets are ever requested.
- All hosts get Hermes-level features through one server-side MCP contract (v2): recall injected before every turn, automatic capture of every completed turn including bounded redacted tool calls and results, session boundaries, subagent capture, durable delivery, explicit remember/forget, and evidence.
- Durable delivery per host: Hermes keeps its profile-local write-ahead spool; Claude and Codex treat the host transcript as the spool — the server reports missing turns and the plugin sync command re-imports them with deterministic ids, so nothing is stored twice.
- Import past conversations is asked once, after connection: raw, redacted turns from that host's own transcripts, imported after the user confirms the session list. Nothing is written before confirmation; secrets are redacted client- and server-side.
- Known limits, stated honestly: Codex has no session-end hook (the server seals idle sessions after 30 minutes); Claude's SessionStart hook at launch runs before MCP connects; if a Cowork cloud session ends while Substrate is unreachable and is never reopened, its last turn is not recovered; plugin hooks in Codex must be trusted once via `/hooks`; Cowork needs a new session after install.
- Legacy: the August 2026 API-key plugins stay deprecated and their spools are not migrated; the `v0.5.0` and `v0.6.0` tags remain for rollback; the server keeps the Hermes `/api/v1` wire during the rollout.


## 0.6.0

Unified onboarding and install parity:

- Use one Substrate connection journey for Hermes and MCP: sign in, review the
  connection name, permissions and proof details, approve, then verify from the agent.
- Centralize installation and recovery guidance in `docs/installation.md`.
- Distinguish browser consent (**Connection approved**) from a verified client
  connection (**Connected to Substrate.**). No manual credential setup is added.
- Preserve the golden Hermes runtime and the thin remote MCP manifest.
- Add host mechanics, a self-check protocol, and Cowork's required new-session
  step before MCP tools appear.
- Ask once, after verification, whether to import past conversations; the agent
  extracts durable facts from history it can read and writes confirmed items with
  `memory_remember`. Same step for Hermes and MCP hosts.
- Correct `SECURITY.md`: the MCP endpoint and browser OAuth backend are deployed.
- Document Claude Code and Codex migration from deprecated August 2026 API-key
  plugins (`substrate_capture` and local spool paths); legacy spool data is not
  migrated and users never set `SUBSTRATE_API_KEY` by hand.

Client strategy:

- Replace the five host adapters with one thin Cowork-compatible
  `plugins/substrate-mcp` package. It contains a remote HTTP MCP manifest at
  `https://app.trysubstrate.co/mcp`, a generic best-effort usage skill, and
  installation docs only. It has no local MCP server, hooks, or automatic
  full-transcript guarantee.
- Preserve the complete golden Hermes `plugins/substrate` tree from v0.5.0,
  including its published v0.5.0 installation pin during the rollout.
- Ship deterministic Hermes and thin-MCP archives. The remote MCP endpoint and
  browser OAuth backend are deployed; a real Cowork installation was verified with
  an authenticated `memory_search` before publication.

## 0.5.0

- Durable v5 Hermes `substrate` plugin at content version `0.4.0`
  (`substrate.zip` rebuilt from the new `src/`-layout tree with a
  namespace-loader root scaffold): write-ahead spool with strict ACK
  retirement, `memory_remember`/`memory_forget` write tools, true session
  boundaries (`on_session_finalize`), session rotation
  (`on_session_reset`), parent-routed subagent capture
  (`subagent_start`/`subagent_stop`), and install-friendly device login
  (`onboard.py start`/`status`/`poll --json` plus the `setup.py` compat
  entry point) against the canonical `https://app.trysubstrate.co` origin.
  Pre-login capture stays pending (never quarantined) until browser
  approval; the tenant-scoped key is stored owner-only in the active
  profile. No API key is requested or pasted at any step.
- Keep the five host adapters (`claude-code`, `claude-cowork`, `codex`,
  `grok-bot`, `openclaw`) byte-identical to the `v0.4.0` release assets.
- Preserve the `v0.3.0` and `v0.4.0` tags and assets for rollback.

## 0.4.0

- First multi-host release of the plugin set (release version `0.4.0`, read from
  the root `VERSION` file): one immutable tag publishes six deterministic
  archives (`substrate.zip`, `claude-code.zip`, `claude-cowork.zip`, `codex.zip`,
  `grok-bot.zip`, `openclaw.zip`) plus a single `SHA256SUMS`.
- Ship the five new host adapters at manifest version `0.4.0`: `plugins/claude-code/`
  (Claude Code), `plugins/claude-cowork/` (Claude Cowork), `plugins/codex/`
  (Codex CLI), `plugins/grok-bot/` (Grok Build CLI / MCP-capable harness), and
  `plugins/openclaw/` (OpenClaw).
- Keep the Hermes reference plugin (`plugins/substrate/`) frozen at content
  version `0.3.0`: its bytes and its `substrate.zip` archive are identical to
  the `v0.3.0` release.
- Include the two live fixes found against checkouts without explicit home
  variables: `78c1549` credential-location parity (persist the token file and
  read the profile `.env` fallback so first-use onboarding does not loop) and
  `2b23b52` home resolution (route all vendored home resolution through the
  per-host `hosthome` modules so state lands in the host home, never the repo
  checkout).

## Unreleased

- Add self-contained host adapters with tool, redaction, envelope, onboarding,
  and fail-closed parity to the Hermes reference plugin (still `0.3.0`, still
  frozen under `plugins/substrate/`): `plugins/claude-code/`,
  `plugins/claude-cowork/`, `plugins/codex/`, `plugins/grok-bot/`, and
  `plugins/openclaw/`, each with its native manifest, `README.md`, `INSTALL.md`,
  and offline test file.
- Merge the Claude marketplace entries (`substrate-memory` for Claude Code,
  `substrate-cowork` for Cowork) into the root `.claude-plugin/marketplace.json`
  and add the root `.agents/plugins/marketplace.json` entry for the Codex
  Git-URL marketplace flow.
- Isolate the per-host test modules with `tests/_hostload.py` plus
  `tests/conftest.py` so the full suite is green in any collection order.
- Cover the new plugins in CI (full pytest suite, `node --check` for the OpenClaw
  adapter, JSON-parse validation of every manifest).

## 0.3.0

- First full release of the `substrate` plugin for Hermes 0.21.x: one-prompt install
  from the `v0.3.0` tag, self-contained RFC 8628 device onboarding with agent display
  names, `memory_search`/`memory_expand`/`memory_evidence`, live session completion
  markers, pinned public ISRG trust roots, and health-gated legacy cutover.
- Remove the legacy `substrate_wiki` provider, its installer/builder, migration
  benchmarks, release assets, and publication-policy machinery. The repository now
  contains only the current plugin, its tests, and a deterministic release builder.
- Replace the closed-inventory publication scanner with a dependency-free public
  hygiene check (`scripts/check_public_hygiene.py`).

- Add the current `substrate` plugin (`plugins/substrate`, now version 0.3.0) for Hermes 0.21.x: one-prompt `hermes plugins install` of the subdirectory, self-contained RFC 8628 device onboarding on first use (browser approval link, background polling, key stored privately in the active profile's `.env`), `memory_search`/`memory_expand`/`memory_evidence` tools, verified TLS with bundled public ISRG root anchors, and health-gated legacy cutover. The legacy `substrate_wiki` 2.0.x provider below remains only for existing Hermes 0.20.x installations.
- Let hosted onboarding validate only the history replay contract, so a setup-and-upload MVP does not need to advertise entity retrieval features.
- Add an explicit `SUBSTRATE_WIKI_ORIGIN` override for isolated v5 import tests; the hosted origin remains the default.

## 2.0.5

- Make browser approval completion return an explicit history-consent action to the installing agent.
- Keep blank or interrupted consent prompts pending instead of treating them as refusal.
- Always print the complete browser approval URL while the polling installer waits.
- Support the Substrate v5 history-only capability handshake and test origin.
- Rename the public source repository to `Substrate-memory-plugins`.

## 2.0.4

- Upload only completed user/assistant text; exclude tool calls, tool results, system messages,
  memory writes, session boundaries, provider scope, hashes, retention metadata, and duplicate
  envelope fields.
- Batch historical dialogue up to the request-size limit instead of posting one message per
  request, while retaining deterministic IDs and resumable checkpoints.

## 2.0.3

- Keep approved device polling active across transient hosted transport and edge failures.
- Use a 60-second OAuth request timeout and fixed content-free failure categories.

## 2.0.2

- Retry transient post-approval capability checks across one hosted tenant cold start.
- Preserve a content-free capability failure category without storing an unvalidated credential.

## 2.0.1

- Present the complete one-click device authorization URL in agent and headless prompts.
- Require a server-supplied complete URL to carry the issued one-time code; construct the correct hosted URL when that optional field is absent.

## 2.0.0

- Targets Hermes 0.20.x user-plugin discovery and native setup hooks.
- Installs and activates under `$HERMES_HOME/plugins/substrate_wiki`.
- Adds hosted RFC 8628 onboarding, tenant credential custody, revocation repair, and fixed-origin enforcement.
- Makes prior-history upload optional while keeping future capture automatic.
- Adds durable cross-platform history supervision, stable replay IDs, resumable checkpoints, and content-free progress.

## 1.5.0 — pending standalone source candidate

- When released and cut over with the companion Substrate-v2 change, establishes this public repository as the independently built plugin source.
- Preserves provider, configuration, state, and server protocol identities.
- Separates repository-native release assets from immutable imported releases.
- Moves privacy-safe migration benchmarks and plugin-owned verification out of Substrate-v2.
- Declares the permanent MIT/open versus held hosted-service boundary and DCO contribution terms.
- Explicitly records that local/no-server runtime and policy authorization are not implemented in this candidate.

All notable changes to the public `substrate_wiki` plugin are recorded here.

## 1.4.1 — imported release

- Preserves the existing OOM-safe `stream-v2` replay protocol and durable checkpoint behavior.
- Adds graceful import-service pause on systemd stop.
- Retains Hermes 0.18.2 compatibility and `entity-quality-v2` bounded memory-card recall.
- Imported byte-for-byte from Substrate-v2. Archive SHA-256: `877ccf9b0212792b699d9c98912a26980675a6050df3bd319e927639e3d901f1`.
- Original source commit: `a3953b0512bbb84fb62b48a75bab04cbcb845c78`.

Earlier immutable artifacts remain under `legacy-assets/` with their original provenance.