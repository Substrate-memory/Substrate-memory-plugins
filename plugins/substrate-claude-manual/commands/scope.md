---
description: Limit this session's Substrate memory reads to one Wiki page and the pages under it (or lift the limit with "off").
argument-hint: '<page | "Path/To Page" | p:handle | off> [question]'
allowed-tools:
  - mcp__plugin_substrate-claude-manual_substrate-memory__memory_search
  - mcp__plugin_substrate-claude-manual_substrate-memory__memory_expand
  - mcp__plugin_substrate-claude-manual_substrate-memory__memory_evidence
---

Arguments: $ARGUMENTS

Substrate has already applied this command on its server, from the line the
user typed. Do not call any tool to set or lift the scope; there is none. Read
the `[substrate]` line at the top of this turn's Substrate memory context and
act on it:

- **"now limited to the Wiki page …"**: confirm in one short sentence which
  page the session is scoped to. If the arguments contain a question after the
  page, answer it with `memory_search` (and `memory_expand` /
  `memory_evidence` for detail). Use only what those tools return; there is no
  automatic recall while the session is scoped.
- **"still being prepared"**: say the scope is being prepared and searches
  return nothing for a few seconds. If a question follows, search once; if it
  is empty, say the scope is not ready yet rather than that nothing exists.
- **"matches several Wiki pages"** or **"No Wiki page matches"**: show the
  listed pages and ask which one was meant. Nothing was changed; stop there.
- **"Scope lifted"**: confirm in one sentence, then answer any question as
  usual.
- **"not scoped"** or **"This session is scoped to …"**: report the status.
- **"Scoping is not available"**: say so, giving the reason exactly as that
  line states it (do not guess another), then answer any question as usual.
- **No `[substrate]` line at all**: the Substrate plugin is not connected in
  this session, so the scope could not be confirmed. Say so and suggest
  `/substrate-claude-manual:substrate-connect`.

Never pass `session_id` or `share` to a Substrate tool; the plugin sets the
session itself. A page title with spaces must be quoted:
`/substrate-claude-manual:scope "Projects/Big Idea" what changed?`.
