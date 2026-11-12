#!/usr/bin/env python3
"""PreToolUse hook for the plugin's Substrate MCP tools (docs/mcp-contract.md section 7).

Session scopes (`/substrate-claude:scope DIR`) are enforced by the server per
host session, and the read tools carry no session of their own. This hook:

- memory_search, memory_expand, memory_evidence: sets `session_id` to this
  session's id (overwriting any the model passed) via `updatedInput`, which
  replaces the whole input, so the rest of `tool_input` is sent back unchanged,
  with permissionDecision "allow": these three are read-only, and without a
  decision Claude Code's auto mode refuses any call whose input a hook changed;
- memory_turn_context, memory_capture_tool, memory_capture_turn,
  memory_session_boundary: these are the plugin's own hook tools; a call the
  model makes is denied (exit 2). The plugin's mcp_tool hooks do not pass
  through PreToolUse, so they are unaffected;
- any other tool: passes through untouched.

Fails closed: any error, or no session_id in the hook input, exits 2 and the
call is blocked. hooks.json also sets "onFailure": "block", so a hook that
cannot start or times out blocks the call too. Standard library only, kept
small so it starts fast on every read.
"""
import json
import sys

READ_TOOLS = {"memory_search", "memory_expand", "memory_evidence"}
HOOK_TOOLS = {"memory_turn_context", "memory_capture_tool", "memory_capture_turn", "memory_session_boundary"}


def decide(payload):
    """(exit code, stdout text, stderr text) for one PreToolUse input."""
    tool = str(payload.get("tool_name") or "").rsplit("__", 1)[-1]
    if tool in HOOK_TOOLS:
        return 2, "", f"{tool} is called by the Substrate plugin's hooks, never directly."
    if tool not in READ_TOOLS:
        return 0, "", ""
    session_id = payload.get("session_id")
    tool_input = payload.get("tool_input")
    if not isinstance(session_id, str) or not session_id or not isinstance(tool_input, dict):
        return 2, "", "Substrate could not tell which session this read belongs to, so it was not sent."
    updated = {**tool_input, "session_id": session_id}
    return 0, json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                 "updatedInput": updated}}), ""


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook input is not an object")
        code, out, err = decide(payload)
    except Exception:  # noqa: BLE001 - fail closed on anything unexpected
        code, out, err = 2, "", "The Substrate session hook failed, so this call was not sent."
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    return code


if __name__ == "__main__":
    sys.exit(main())
