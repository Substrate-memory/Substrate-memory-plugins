---
name: substrate-memory
description: Fallback remote Substrate recall, explicit writes, and agent-driven history import for MCP hosts without a native package.
---

# Substrate memory usage (fallback)

This skill is the fallback for MCP hosts without a native Substrate package. Claude Cowork and Claude Code use `plugins/substrate-claude`; ChatGPT Work, the Codex app and Codex CLI use `plugins/substrate-codex`; Hermes uses `plugins/substrate-hermes`.

## Self-check before installing or reconnecting

Use this decision protocol before changing host configuration:

1. If the expected memory tools are present, run an authenticated `memory_search` smoke test. An empty result is valid. Report **Connected to Substrate.** only when the request succeeds.
2. If the plugin/server is configured but the tools are absent, say that it is installed but not wired into this session. Follow the host's documented session or reload action. Then ask the user to paste:

   ```text
   Verify my Substrate connection with memory_search and report Connected to Substrate.
   ```
3. If nothing is configured, use the host's documented installation interface.
4. If tools are present but authentication is required, show the exact browser URL. The user signs in, reviews the request, and chooses **Approve connection**. Finish in the agent and rerun the smoke test. **Connection approved** is not **Connected to Substrate.**

## Contract v2 tools

Use the remote Substrate MCP tools when they are available. Full schemas are defined in `docs/mcp-contract.md` (contract version 2).

- **Recall (best effort).** Before relying on a remembered fact, call `memory_search` with a focused query; use `memory_expand` and `memory_evidence` to check the source. Treat an empty result as no recalled context, not as proof that a fact is false.
- **Explicit writes (best effort).** Use `memory_remember` only when the user clearly asks to remember something or the intended durable write is unambiguous. Its arguments are `operation_id` (1–128 characters matching `[A-Za-z0-9._:-]+`), `text` (1–4096 UTF-8 bytes), optional `about` (at most 256 bytes), and optional `durability` (`durable`, `time_bounded`, or `transient`). Generate one operation ID per intended assertion and reuse the exact ID and payload only for retries. After a forget, use a new operation ID for a new assertion. Confirm the returned handle when the tool succeeds.
- **History import.** Where the host can read its own transcripts, replay confirmed redacted turns with `memory_import` in batches of at most 64 items. Items sent without an `event_id` get a deterministic id, so re-sending never stores twice. Check `memory_import_status` for what the server holds.
- Do not claim that every user turn or the full conversation transcript was captured. This plugin has no hooks and does not provide automatic transcript capture guarantees.
- Never send secrets, API keys, OAuth tokens, client secrets, passwords, or private credentials to memory tools. Redact them before any recall query or write.
- Use `memory_forget` only for an explicit retraction. It takes the same kind of `operation_id`, a lowercase-hex `m:` or `p:` handle, and an optional reason up to 1024 bytes. Forget suppresses normal recall; it does not physically erase retained evidence. Explicit expand/evidence may show history labelled invalidated; never use that history as a current fact. Never send tenant or account IDs; the server owns them.
- If authentication is required, follow the same setup sequence as every host: sign in, review the connection details and name, choose **Approve connection**, then return to the agent for verification. Show the exact browser URL. Only the user approves. **Connection approved** is not yet proof of a working client; report **Connected to Substrate.** only after an authenticated smoke call succeeds. Never ask for a pasted token or client secret.
- After the first successful smoke test, ask exactly once whether the user wants to import past conversations. List the sessions this agent can read on this host and import only what the user confirms. Never import secrets. If the user says no, do not ask again. If no history is accessible, say so and stop.
- If the host does not expose the expected MCP tools, say so plainly and do not invent a local server or fallback credential flow.
