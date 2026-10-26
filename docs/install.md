# Install Substrate memory

You are an AI agent. The user wants this agent connected to Substrate memory.
Do each step yourself, in this turn. Stop only for what only the user can do:
a permission prompt, the browser approval, the import choice. Never ask for or
accept an API key, token or password. Keep messages to the user short.

## 1. Already connected?

If you have a Substrate `memory_search` tool from the Substrate plugin or
connector, call it with a short query. If it works (even empty), say
"Connected to Substrate." and go to step 4. Tools named `mcp__claude_ai_*` come
from a claude.ai connector and do not count: install the plugin anyway.

## 2. Install for your host

Work out which agent you are. Install one copy only.

**Claude Code.** Skip if `claude plugin list --json` shows `substrate-claude@…`.
```
claude plugin marketplace add Substrate-memory/Substrate-memory-plugins
claude plugin install substrate-claude@substrate-marketplace
```
Then say exactly: "Installed. Type /reload-plugins, then say continue." The
plugin carries on from the next message: sign-in, check, import offer.

**Codex (CLI or app).**
```
codex plugin marketplace add Substrate-memory/Substrate-memory-plugins
codex plugin add substrate-codex@substrate-marketplace
codex mcp add substrate-memory --url https://app.trysubstrate.co/mcp
codex mcp login substrate-memory
```
`login` prints the approval link: give it to the user, say they should choose
Approve connection, and wait for the command to return. Then run
`python3 <plugin>/scripts/codex_user_hooks.py install` (the plugin is under
`$CODEX_HOME/plugins/cache/*/substrate-codex/*/`, default `~/.codex`). Then say:
"Approve the Substrate hooks once in /hooks, then start a new session and say
continue."

**Hermes.**
```
hermes plugins install https://github.com/Substrate-memory/Substrate-memory-plugins --enable
python "$(dirname "$(hermes config path)")/plugins/substrate/onboard.py" start
```
Show the link and code. Run `onboard.py poll` in the background. It ends with
"Connected to Substrate as …". Ask before any gateway restart.

**Claude Cowork.** You cannot install plugins here. Give the user these clicks:
Settings → Plugins → + Add → Add from a repository → paste
`https://github.com/Substrate-memory/Substrate-memory-plugins` → Sync → search
"substrate" → turn on substrate-claude → Connect substrate-memory → Approve
connection. If the tools don't appear, start a new session.

**claude.ai, ChatGPT, Claude Desktop chat, Cursor, other apps with connectors.**
Give the user these clicks: Settings → Connectors → Add custom connector →
name "Substrate", URL `https://app.trysubstrate.co/mcp` → Add → Connect →
Approve connection → start a new chat.

**Any other MCP agent.** Add the remote server `https://app.trysubstrate.co/mcp`
(Streamable HTTP, OAuth) through your documented config, then sign in.

## 3. Approve

The user opens the link, signs in or creates an account (7 days free, no card)
and chooses Approve connection. Keep checking; never wait for "done". Say
"Connected to Substrate." only after `memory_search` succeeds.

## 4. Import (same reply)

**Plugin hosts (Claude Code, Codex, Hermes).** Offer once, with the real count
and date range the plugin reports. Say:
"I found N past conversations on this computer (T turns, FIRST to LAST). Import
them into Substrate? Import all / Let me pick / Not now"
Then follow the plugin's import: `/substrate-claude:substrate-import` in Claude
Code, `$substrate-import` in Codex, `onboard.py import --yes`, `--session <id>`
or `--no` in Hermes. Report progress until the summary line
`Imported N sessions (T turns): S stored, D duplicate, R rejected.` Not now:
record it and never ask again. Skip on Cowork and when nothing is readable.

**MCP hosts (claude.ai, ChatGPT, Cursor, other connectors).** Offer once:
"I can save what I already know about you to Substrate. Want me to?" On yes,
search your own memory and past chats for people, projects, decisions,
preferences and open items. Show the list. After the user confirms, call
`memory_import` with one document item: `kind` "document", a short `title`,
the list as markdown `text`, and `source` set to your app name + " memory".
If the server rejects the document, save the confirmed facts with
`memory_remember`, one call each. Never import secrets.

Conversation content never passes through you on the plugin path. Secrets are
redacted; repeats are ignored.

## 5. First question

End with one question for the user to try. After an import: "Ask me: What do you
know about my work?" With nothing imported: "Tell me something to remember, then
ask me about it in a new chat."
