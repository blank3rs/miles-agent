"""Aggregated tool registry.

Each module defines HANDLERS (name → async handler returning str, never raising)
and DEFINITIONS (OpenAI function schemas). This package sums them into
TOOL_HANDLERS / TOOL_DEFINITIONS and re-exports the names server.py needs.
"""
from agent.tools import (
    browser,
    calendar_tools,
    contacts,
    dispatch,
    files,
    gmail,
    heartbeats,
    memory,
    secrets_store,
    skills,
    subagent,
    system,
    tasks,
    verification,
    vision,
    voice_call,
    web,
)
from agent.tools.heartbeats import cancel_heartbeat
from agent.tools.tasks import open_tasks_summary

_MODULES = (
    files, gmail, calendar_tools, web, browser, verification, vision,
    system, secrets_store, skills, memory, tasks, contacts, subagent, dispatch, voice_call, heartbeats,
)

TOOL_HANDLERS: dict = {}
TOOL_DEFINITIONS: list = []

for _m in _MODULES:
    for _name, _fn in _m.HANDLERS.items():
        if _name in TOOL_HANDLERS:
            raise RuntimeError(f"Duplicate tool name across modules: {_name}")
        TOOL_HANDLERS[_name] = _fn
    TOOL_DEFINITIONS.extend(_m.DEFINITIONS)

_def_names = [d["function"]["name"] for d in TOOL_DEFINITIONS]
if len(_def_names) != len(set(_def_names)):
    raise RuntimeError("Duplicate tool definition names")
if set(_def_names) != set(TOOL_HANDLERS):
    raise RuntimeError(
        f"Handler/definition mismatch: only-handlers={set(TOOL_HANDLERS) - set(_def_names)}, "
        f"only-defs={set(_def_names) - set(TOOL_HANDLERS)}"
    )

async def call_tool(name: str, **kwargs):
    """Invoke any registered tool by name. The bridge skills use to compose
    Miles's real tools (browser_task, scrape_url, send_email, …) into one
    reusable capability. Returns the tool's string result; never raises."""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"[unknown tool: {name}] Available: {sorted(TOOL_HANDLERS)}"
    try:
        return await handler(**kwargs)
    except TypeError as e:
        return f"[bad arguments for {name}] {e}"


__all__ = [
    "TOOL_HANDLERS",
    "TOOL_DEFINITIONS",
    "call_tool",
    "cancel_heartbeat",
    "open_tasks_summary",
]
