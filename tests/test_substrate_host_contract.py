"""Host-API contract: registered hooks exist in the host and accept its kwargs.

Source of truth is the host repo (read-only, never imported — AST-parsed
so no host dependencies are needed). Pinned host revision: 375ce8e
(2026-09-01). Skipped when the host checkout is absent.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

HOST_REPO = Path("/home/substrateops/hermes-agent")
HOST_PLUGINS = HOST_REPO / "hermes_cli" / "plugins.py"

# Dispatch kwargs vendored from the host at the pinned revision
# (agent/turn_finalizer.py ~637/~833, hermes_cli/lifecycle.py,
# cli.py _notify_session_boundary, tools/delegate_tool.py ~2199/~3734).
HOST_HOOK_KWARGS: dict[str, dict[str, object]] = {
    "pre_llm_call": {
        "session_id": "s", "task_id": "t", "turn_id": "turn-1",
        "user_message": "hi", "conversation_history": [],
        "is_first_turn": True, "model": "m", "platform": "cli",
        "parent_session_id": "", "sender_id": "u",
        "telemetry_schema_version": 1,
    },
    "post_llm_call": {
        "session_id": "s", "task_id": "t", "turn_id": "turn-1",
        "user_message": "hi", "assistant_response": "there",
        "conversation_history": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "there"},
        ],
        "model": "m", "platform": "cli",
        "telemetry_schema_version": 1,
    },
    "on_session_end": {
        "session_id": "s", "task_id": "t", "turn_id": "turn-1",
        "completed": True, "failed": False, "interrupted": False,
        "turn_exit_reason": "text_response(stop)", "model": "m",
        "platform": "cli", "telemetry_schema_version": 1,
    },
    "on_session_finalize": {
        "session_id": "s", "platform": "cli", "reason": "shutdown",
        "telemetry_schema_version": 1,
    },
    "on_session_reset": {
        "session_id": "new", "platform": "cli", "reason": "new_session",
        "telemetry_schema_version": 1,
    },
    "subagent_start": {
        "parent_session_id": "parent-1", "parent_turn_id": "t",
        "parent_subagent_id": "ps", "child_session_id": "child-9",
        "child_subagent_id": "cs", "child_role": "research",
        "child_goal": "look something up",
        "telemetry_schema_version": 1,
    },
    "subagent_stop": {
        "parent_session_id": "parent-1", "parent_turn_id": "t",
        "child_session_id": "child-9", "child_role": "research",
        "child_summary": "done", "child_status": "ok",
        "tool_call_history": [], "duration_ms": 12,
        "telemetry_schema_version": 1,
    },
}


def _host_valid_hooks() -> set[str]:
    tree = ast.parse(HOST_PLUGINS.read_text(), filename=str(HOST_PLUGINS))
    for node in ast.walk(tree):
        value = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "VALID_HOOKS"
            for target in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "VALID_HOOKS"
            and node.value is not None
        ):
            value = node.value
        if value is not None and isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            return {
                element.value
                for element in value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    raise AssertionError("VALID_HOOKS not found in host plugins.py")


@pytest.fixture
def host_hooks():
    if not HOST_PLUGINS.is_file():
        pytest.skip("host checkout absent")
    return _host_valid_hooks()


class _Context:
    def __init__(self):
        self.hooks: dict[str, object] = {}
        self.tools: dict[str, object] = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, *, name, toolset, schema, handler, **kwargs):
        self.tools[name] = handler

    def register_system_prompt_section(self, *args, **kwargs):
        pass


def test_registered_hooks_exist_in_host(host_hooks, clean_session_state):
    from substrate import plugin

    ctx = _Context()
    plugin.register(ctx)
    assert set(ctx.hooks) <= host_hooks, (
        "registered unknown hooks: "
        + ", ".join(sorted(set(ctx.hooks) - host_hooks))
    )
    assert set(ctx.hooks) == set(HOST_HOOK_KWARGS)


def test_registered_hooks_accept_host_kwargs(host_hooks, fake_spool, clean_session_state):
    from substrate import plugin

    ctx = _Context()
    plugin.register(ctx)
    for name, kwargs in HOST_HOOK_KWARGS.items():
        callback = ctx.hooks[name]
        callback(**kwargs)  # must never raise on host-shaped payloads
    # Host-shaped post_llm_call captures one live turn; finalize adds the
    # end boundary; per-turn on_session_end adds nothing.
    assert [item["kind"] for item in fake_spool.items] == [
        "capture_turn",
        "capture_session",
    ]
    assert fake_spool.items[1]["envelope"]["payload"]["boundary"] == "end"
    # Reset rebound to the new session.
    assert plugin._SESSION_STATE["active"] == "new"
