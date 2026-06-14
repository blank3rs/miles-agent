"""Task ledger + working-state checkpoint — both backed by the one store (miles.db).

The ledger (add/update/list) is what Miles owes. set_focus writes the structured
checkpoint (current goal, next action, the task in flight) that makes a restart resume
cleanly — it's read back into every compiled context, so future-Miles always knows where
he was, without parsing a prose summary.
"""
from agent import store


def _format(t: dict) -> str:
    note = f" — {t['note']}" if t.get("note") else ""
    return f"[{t['id']}] ({t['status']}) {t['title']}{note}"


def open_tasks_summary() -> str:
    """Open/in-progress/blocked tasks as text. Used by boot injection — not a tool."""
    tasks = store.list_tasks()
    if not tasks:
        return "(task ledger empty)"
    return "\n".join(_format(t) for t in tasks)


async def add_task(title: str, notes: str = "") -> str:
    try:
        tid = store.add_task(title)
        if notes:
            store.update_task(tid, note=notes)
        return f"Task added: {_format(store.get_task(tid))}"
    except Exception as e:
        return f"[error adding task] {e}"


async def update_task(task_id: str, status: str = "", notes: str = "") -> str:
    valid = ("open", "in_progress", "blocked", "done", "cancelled")
    if status and status not in valid:
        return f"[error] status must be one of: {', '.join(valid)}"
    try:
        ok = store.update_task(
            task_id,
            status=status or None,
            note=notes or None,
        )
        if not ok:
            return f"(no task with id: {task_id} — use list_tasks to see ids)"
        return f"Task updated: {_format(store.get_task(task_id))}"
    except Exception as e:
        return f"[error updating task] {e}"


async def list_tasks(include_done: bool = False) -> str:
    try:
        tasks = store.list_tasks(include_done=include_done)
        if not tasks:
            return "(task ledger empty)"
        order = {s: i for i, s in enumerate(("in_progress", "blocked", "open", "done", "cancelled"))}
        tasks.sort(key=lambda t: (order.get(t.get("status"), 9), t.get("created_at", "")))
        return "\n".join(_format(t) for t in tasks)
    except Exception as e:
        return f"[error listing tasks] {e}"


async def set_focus(goal: str = "", next_action: str = "", active_task_id: str = "") -> str:
    """Update your working-state checkpoint so a restart picks up cleanly."""
    try:
        store.set_working_state(
            current_goal=goal or None,
            next_action=next_action or None,
            active_task_id=active_task_id or None,
        )
        ws = store.get_working_state()
        return (f"Focus set. Goal: {ws['current_goal'] or '(none)'} | "
                f"Next: {ws['next_action'] or '(none)'} | "
                f"Active task: {ws['active_task_id'] or '(none)'}")
    except Exception as e:
        return f"[error setting focus] {e}"


HANDLERS = {
    "add_task":    add_task,
    "update_task": update_task,
    "list_tasks":  list_tasks,
    "set_focus":   set_focus,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": "Add a task to your ledger. Use for any work that outlives the current turn — outreach threads, research, things you promised, blocked work waiting on someone. The ledger survives restarts and is shown to you in every turn.",
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
            "description": "Update a task's status or notes as work moves. Statuses: open, in_progress, blocked, done, cancelled. Always leave a note saying what happened — future-you reads it after a restart.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task id from add_task or list_tasks"},
                    "status":  {"type": "string", "enum": ["open", "in_progress", "blocked", "done", "cancelled"]},
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
            "description": "List tasks in your ledger (open, in_progress, blocked). The source of truth for open work — check it when picking what to do next.",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_done": {"type": "boolean", "default": False, "description": "Also show recently completed tasks"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_focus",
            "description": "Set your working-state checkpoint: what you're focused on right now, your next concrete action, and the task you're actively on. This is what lets you pick back up cleanly if you restart mid-thought — update it whenever your focus or next step changes. Pass only the fields you want to change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal":           {"type": "string", "description": "The thing you're driving at right now, one line"},
                    "next_action":    {"type": "string", "description": "The very next concrete step you'll take"},
                    "active_task_id": {"type": "string", "description": "Task id (from the ledger) you're actively working"},
                },
                "required": [],
            },
        },
    },
]
