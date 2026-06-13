"""Async sub-agent dispatch.

Miles dispatches a browser_task or run_subagent and gets a task id back immediately
instead of blocking his turn for minutes. The work runs as a background asyncio task;
when it finishes, the result is delivered back to Miles as a new turn (same pattern as
the phone-call transcript loop).

Browser tasks serialize on the single shared browser (browser._LOCK) — only one agent
drives the browser at a time. Research sub-agents have no such constraint and run
concurrently. Either way, Miles never waits — he keeps working while they run.

The registry is in-memory: in-flight tasks are lost on restart (Miles can re-dispatch).
"""
import asyncio
import uuid
from datetime import datetime, timezone

import structlog

from agent import runtime

log = structlog.get_logger()

# task_id -> {kind, label, status, result, started_at, finished_at}
_REGISTRY: dict[str, dict] = {}
_MAX_KEEP = 60

# Strong references to in-flight background tasks. asyncio only keeps WEAK references
# to tasks, so without this the GC can collect a long-running fire-and-forget task
# mid-execution — abandoning the work and never delivering a result.
_BG_TASKS: set = set()


def _trim() -> None:
    if len(_REGISTRY) <= _MAX_KEEP:
        return
    finished = [k for k, v in _REGISTRY.items() if v["status"] != "running"]
    finished.sort(key=lambda k: _REGISTRY[k].get("finished_at") or "")
    for k in finished[: len(_REGISTRY) - _MAX_KEEP]:
        _REGISTRY.pop(k, None)


async def _run_and_deliver(task_id: str, kind: str, label: str, coro) -> None:
    try:
        result = await coro
        status = "done"
    except Exception as e:
        result = f"[failed] {e}"
        status = "error"
        log.warning("dispatch_task_failed", task_id=task_id, kind=kind, err=str(e))

    entry = _REGISTRY.get(task_id)
    if entry:
        entry["status"] = status
        entry["result"] = str(result)
        entry["finished_at"] = datetime.now(timezone.utc).isoformat()

    if runtime.enqueue_task:
        runtime.enqueue_task({
            "type": "dispatch_result",
            "task_id": task_id,
            "kind": kind,
            "content": (
                f'Your background {kind} task [{task_id}] just finished — "{label[:80]}".\n\n'
                f"Result:\n{str(result)[:6000]}\n\n"
                f"Pick up whatever this was for and act on it. Other background tasks may still be running "
                f"(check_tasks)."
            ),
        })
    else:
        log.warning("dispatch_result_undelivered", task_id=task_id)


def dispatch(kind: str, label: str, coro) -> str:
    """Schedule coro to run in the background; return a short task_id immediately.
    The result is delivered back to Miles as a new turn when the task completes."""
    task_id = uuid.uuid4().hex[:8]
    _REGISTRY[task_id] = {
        "kind": kind,
        "label": label[:160],
        "status": "running",
        "result": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }
    _trim()
    t = asyncio.create_task(_run_and_deliver(task_id, kind, label, coro))
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return task_id


async def check_tasks() -> str:
    """List background tasks you've dispatched — what's still running and what's done."""
    if not _REGISTRY:
        return "(no background tasks dispatched)"
    lines = []
    for tid, e in sorted(_REGISTRY.items(), key=lambda kv: kv[1]["started_at"], reverse=True):
        tail = ""
        if e["status"] != "running" and e.get("result"):
            tail = " — " + str(e["result"])[:140].replace("\n", " ")
        lines.append(f"[{tid}] ({e['status']}) {e['kind']}: {e['label']}{tail}")
    return "\n".join(lines)


HANDLERS = {
    "check_tasks": check_tasks,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "check_tasks",
            "description": (
                "List the background sub-agent tasks you've dispatched (browser_task, run_subagent) — which are "
                "still running and which finished, with a snippet of each result. Their full results also come "
                "back to you automatically as new turns when they complete, so you usually don't need to poll — "
                "use this when you want a quick status of what's in flight."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
