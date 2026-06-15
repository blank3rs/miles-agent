"""Aggregated tool registry.

Each module defines HANDLERS (name → async handler returning str, never raising)
and DEFINITIONS (OpenAI function schemas). This package sums them into
TOOL_HANDLERS / TOOL_DEFINITIONS and re-exports the names server.py needs.
"""
from agent.tools import (
    browser,
    calendar_tools,
    code_exec,
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
    web_cli,
)
from agent.tools.heartbeats import cancel_heartbeat
from agent.tools.tasks import open_tasks_summary

_MODULES = (
    files, gmail, calendar_tools, web, web_cli, browser, verification, vision,
    system, code_exec, secrets_store, skills, memory, tasks, contacts, subagent, dispatch, voice_call, heartbeats,
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


def _first_sentence(text: str) -> str:
    """First sentence of a tool description for the always-resident index.
    Splits on the first sentence-ending '. ' so mid-sentence abbreviations and
    em-dashes survive; falls back to the whole (single-sentence) string."""
    text = " ".join(text.split())
    head, sep, _ = text.partition(". ")
    return head + "." if sep else text


# Always-resident one-line index of every tool, derived in registry order so it
# stays in lockstep with the parity-checked TOOL_DEFINITIONS (no parallel list).
# Static (descriptions don't vary per turn) — safe to live in the cached prefix.
TOOL_SUMMARIES = "\n".join(
    f"- {d['function']['name']}: {_first_sentence(d['function']['description'])}"
    for d in TOOL_DEFINITIONS
)

async def call_tool(name: str, **kwargs):
    """Invoke any registered tool by name. The bridge skills use to compose
    Miles's real tools (browser_task, scrape_url, send_email, …) into one
    reusable capability. Returns the tool's string result; never raises."""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"[unknown tool: {name}] Available: {sorted(TOOL_HANDLERS)}"
    # Same gates as the hot loop (core._exec_tool), so the skills/subagent bypass paths that never
    # reach _exec_tool are gated identically. Lazy imports keep the package import-cycle free.
    #
    # Deterministic precondition gate FIRST (mirrors _exec_tool's order): a world-state floor the
    # tool can't self-check — e.g. make_call only when no call is live — so composing make_call
    # through a skill/subagent during an active call is bounced here too, not just on the hot loop.
    import asyncio

    from agent import heso_receipts, policy
    pred = policy.PRECONDITIONS.get(name)
    if pred:
        block = pred(kwargs)
        if block:
            return block
    # HESO governance gate for side-effecting ACTION tools (same authority as the hot loop).
    # Reads/research tools aren't ACTION, so they're never gated. Fails open if HESO is off.
    if policy.tool_kind(name) == policy.TOOL_KIND.ACTION:
        decision, reason = await asyncio.to_thread(heso_receipts.gate, name, kwargs)
        if decision in ("block", "suspend"):
            return reason
    try:
        return await handler(**kwargs)
    except TypeError as e:
        return f"[bad arguments for {name}] {e}"


__all__ = [
    "TOOL_HANDLERS",
    "TOOL_DEFINITIONS",
    "TOOL_SUMMARIES",
    "call_tool",
    "cancel_heartbeat",
    "open_tasks_summary",
]
