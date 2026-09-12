---
name: substrate-memory
description: Best-effort remote Substrate recall and explicit memory writes for Cowork.
---

# Substrate memory usage

Use the remote Substrate MCP tools when they are available.

- Recall is best effort. Before relying on a remembered fact, call the
  available recall/search tool with a focused query. Treat an empty result as
  no recalled context, not as proof that a fact is false.
- Durable writes are best effort. Use `memory_remember` only when the user clearly
  asks to remember something or the intended durable write is unambiguous. Its
  arguments are `operation_id` (1–128 characters matching `[A-Za-z0-9._:-]+`),
  `text` (1–4096 UTF-8 bytes), optional `about` (at most 256 bytes), and
  optional `durability` (`durable`, `time_bounded`, or `transient`). Generate
  one operation ID per intended assertion and reuse the exact ID and payload
  only for retries. After a forget, use a new operation ID for a new assertion.
  Confirm the returned handle when the tool succeeds.
- Do not claim that every user turn or the full conversation transcript was
  captured. This plugin has no hooks and does not provide automatic transcript
  capture guarantees.
- Never send secrets, API keys, OAuth tokens, client secrets, passwords, or
  private credentials to memory tools. Redact them before any recall query or
  write.
- Use `memory_forget` only for an explicit retraction. It takes the same kind of
  `operation_id`, a lowercase-hex `m:` or `p:` handle, and an optional reason
  up to 1024 bytes. Forget suppresses normal recall; it does not physically erase
  retained evidence. Explicit expand/evidence may show history labelled invalidated;
  never use that history as a current fact. Never send tenant or account IDs; the server owns them.
- If authentication is required, follow the same setup sequence as Hermes:
  sign in, review the connection details and name, choose **Approve connection**,
  then return to the agent for verification. Show the exact browser URL. Only the
  user approves. **Connection approved** is not yet proof of a working client;
  report **Connected to Substrate.** only after an authenticated smoke call succeeds.
  Never ask for a pasted token or client secret.
- If the host does not expose the expected MCP tools, say so plainly and do
  not invent a local server or fallback credential flow.
