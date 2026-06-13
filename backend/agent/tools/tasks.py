"""Task ledger: durable record of open work in data/tasks.json.

The boot continuation injects open tasks so a restart never loses the work list;
the agent updates entries as work starts, blocks, and finishes.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from agent.config import TASKS_FILE

_STATUSES = ("open", "in_progress", "blocked", "done")
_DONE_RETENTION_DAYS = 14


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> list[dict]:
    try:
        if TASKS_FILE.exists():
            return json.loads(TASKS_FILE.read_text())
    except Exception:
        pass
    return []


def _save(tasks: list[dict]) -> None:
    # Prune long-done tasks so the ledger stays a working set, not an archive
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_DONE_RETENTION_DAYS)).isoformat()
    tasks = [t for t in tasks if t.get("status") != "done" or t.get("updated_at", "") > cutoff]
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TASKS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tasks, indent=2))
    tmp.replace(TASKS_FILE)


def _format(t: dict) -> str:
    note = f" — {t['notes']}" if t.get("notes") else ""
    return f"[{t['id']}] ({t['status']}) {t['title']}{note}"


def open_tasks_summary() -> str:
    """Open/in-progress/blocked tasks as text. Used by server boot injection — not a tool."""
    pending = [t for t in _load() if t.get("status") != "done"]
    if not pending:
        return "(task ledger empty)"
    return "\n".join(_format(t) for t in pending)


async def add_task(title: str, notes: str = "") -> str:
    try:
        tasks = _load()
        task = {
            "id": str(uuid.uuid4())[:8],
            "title": title,
            "status": "open",
            "notes": notes,
            "created_at": _now(),
            "updated_at": _now(),
        }
        tasks.append(task)
        _save(tasks)
        return f"Task added: {_format(task)}"
    except Exception as e:
        return f"[error adding task] {e}"


async def update_task(task_id: str, status: str = "", notes: str = "") -> str:
    if status and status not in _STATUSES:
        return f"[error] status must be one of: {', '.join(_STATUSES)}"
    try:
        tasks = _load()
        task = next((t for t in tasks if t["id"] == task_id), None)
        if task is None:
            return f"(no task with id: {task_id} — use list_tasks to see ids)"
        if status:
            task["status"] = status
        if notes:
            task["notes"] = notes
        task["updated_at"] = _now()
        _save(tasks)
        return f"Task updated: {_format(task)}"
    except Exception as e:
        return f"[error updating task] {e}"


async def list_tasks(include_done: bool = False) -> str:
    try:
        tasks = _load()
        if not include_done:
            tasks = [t for t in tasks if t.get("status") != "done"]
        if not tasks:
            return "(task ledger empty)"
        order = {s: i for i, s in enumerate(("in_progress", "blocked", "open", "done"))}
        tasks.sort(key=lambda t: (order.get(t.get("status"), 9), t.get("created_at", "")))
        return "\n".join(_format(t) for t in tasks)
    except Exception as e:
        return f"[error listing tasks] {e}"


HANDLERS = {
    "add_task":    add_task,
    "update_task": update_task,
    "list_tasks":  list_tasks,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": "Add a task to your ledger. Use for any work that outlives the current turn — outreach threads, research, things you promised, blocked work waiting on someone. The ledger survives restarts and is shown to you at boot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short imperative title, e.g. 'Follow up with Sarah at Vanta'"},
                    "notes": {"type": "string", "description": "Context: where things stand, next step, links"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": "Update a task's status or notes as work moves. Statuses: open, in_progress, blocked, done. Always leave a note saying what happened — future-you reads it after a restart.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task id from add_task or list_tasks"},
                    "status":  {"type": "string", "enum": ["open", "in_progress", "blocked", "done"]},
                    "notes":   {"type": "string", "description": "What happened / current state / next step"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "List tasks in your ledger (open, in_progress, blocked). The source of truth for open work — check it at boot and when picking what to do next.",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_done": {"type": "boolean", "default": False, "description": "Also show recently completed tasks"},
                },
                "required": [],
            },
        },
    },
]
