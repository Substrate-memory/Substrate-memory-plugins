# Substrate memory

Substrate is the user's shared AI memory, reached through the `substrate` MCP server. It holds facts about the user's work, people and projects, each linked to its source.

- Search Substrate memory (`memory_search`) before answering questions about the user's work, people or projects, and name the source of what you use.
- Save a fact or decision (`memory_remember`) when the user asks you to keep it. Forget it (`memory_forget`) when they ask.
- Never store secrets such as passwords, API keys or tokens.
- Gemini CLI has no capture hooks here, so nothing is saved automatically: only what the user asks to keep.
