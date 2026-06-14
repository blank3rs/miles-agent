"""The single source of truth: one SQLite file at /data/miles.db.

The whole rearchitecture rests on one idea (Letta / Anthropic context-engineering /
durable-execution): the STORE is the truth, the context window is a fresh projection
compiled from it each turn. So restart is a non-event — compile_context() runs the same
on a cold boot as on any other turn, and Miles picks up exactly where he left off.

What used to be scattered across seven places now lives here:
  memory_blocks   — identity / persona / dream-owned sections   (was soul.md + the prose summary)
  working_state   — the structured checkpoint that makes resume deterministic   (new)
  tasks           — the ledger                                   (was tasks.json)
  messages        — append-only conversation log                 (was the in-memory deque)
  episodes        — append-only raw events to consolidate        (was journal/*.jsonl)
  receipts        — every external action, for audit             (new)

Graphiti/FalkorDB is NOT here on purpose: it's a derived recall index over `episodes`,
rebuildable and never required for correctness or resume.

Everything is plain synchronous sqlite3 (local file, sub-millisecond ops) guarded by one
lock so it's safe to call from asyncio.to_thread on any worker thread.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from agent.config import MILES_DB

log = structlog.get_logger()

CHARS_PER_TOKEN = 4
# How much recent conversation to compile into the window. The rest stays in the log and
# is represented by working_state + the dream-owned blocks + on-demand memory search.
HISTORY_TOKEN_BUDGET = 52_000
_HISTORY_CHAR_BUDGET = HISTORY_TOKEN_BUDGET * CHARS_PER_TOKEN

# Dream-owned blocks the sleep-time agent maintains; the waking agent only reads them.
DREAM_BLOCKS = ("learning", "people", "matters_now")
# All blocks, with the char limit that keeps the prompt from bloating (Letta pattern).
_DEFAULT_BLOCKS: dict[str, int] = {
    "identity":          6000,   # who Miles is — the durable self (seeded from soul.md head)
    "working_narrative": 3000,   # a few lines of "where my head's at" — flavor, NOT the resume primitive
    "learning":          4000,   # dream-owned
    "people":            5000,   # dream-owned
    "matters_now":       3000,   # dream-owned
}

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    MILES_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MILES_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # concurrent reads, crash-safe writes
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _conn = conn
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_blocks (
            label      TEXT PRIMARY KEY,
            content    TEXT NOT NULL DEFAULT '',
            char_limit INTEGER NOT NULL DEFAULT 4000,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS working_state (
            id              INTEGER PRIMARY KEY CHECK (id = 1),
            current_goal    TEXT NOT NULL DEFAULT '',
            active_task_id  TEXT,
            completed_steps TEXT NOT NULL DEFAULT '[]',
            next_action     TEXT NOT NULL DEFAULT '',
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id              TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'open',
            detail          TEXT NOT NULL DEFAULT '',
            note            TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT,
            last_step       TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            role              TEXT NOT NULL,
            content           TEXT,
            tool_calls        TEXT,
            tool_call_id      TEXT,
            reasoning_content TEXT,
            trigger           TEXT,
            created_at        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS episodes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            kind         TEXT NOT NULL,
            content      TEXT NOT NULL,
            consolidated INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS receipts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            action        TEXT NOT NULL,
            target        TEXT,
            params_digest TEXT,
            decision      TEXT NOT NULL,
            reason        TEXT NOT NULL DEFAULT '',
            receipt_id    TEXT,
            created_at    TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_episodes_unconsolidated
            ON episodes (consolidated, id);
        """
    )
    # Singleton working_state row + default blocks, created once.
    conn.execute(
        "INSERT OR IGNORE INTO working_state (id, updated_at) VALUES (1, ?)", (_now(),)
    )
    for label, limit in _DEFAULT_BLOCKS.items():
        conn.execute(
            "INSERT OR IGNORE INTO memory_blocks (label, content, char_limit, updated_at) "
            "VALUES (?, '', ?, ?)",
            (label, limit, _now()),
        )
    conn.commit()


def init_db() -> None:
    """Open the DB and ensure the schema exists. Safe to call repeatedly."""
    with _lock:
        _connect()


# ── Memory blocks ────────────────────────────────────────────────────────────────

def get_block(label: str) -> str:
    with _lock:
        row = _connect().execute(
            "SELECT content FROM memory_blocks WHERE label = ?", (label,)
        ).fetchone()
        return row["content"] if row else ""


def set_block(label: str, content: str) -> None:
    """Write a block, clamped to its char limit so the prompt can't bloat."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT char_limit FROM memory_blocks WHERE label = ?", (label,)
        ).fetchone()
        limit = row["char_limit"] if row else 4000
        content = (content or "")[:limit]
        conn.execute(
            "INSERT INTO memory_blocks (label, content, char_limit, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(label) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at",
            (label, content, limit, _now()),
        )
        conn.commit()


def all_blocks() -> dict[str, str]:
    with _lock:
        rows = _connect().execute("SELECT label, content FROM memory_blocks").fetchall()
        return {r["label"]: r["content"] for r in rows}


# ── Working state (the checkpoint) ───────────────────────────────────────────────

def get_working_state() -> dict[str, Any]:
    with _lock:
        row = _connect().execute("SELECT * FROM working_state WHERE id = 1").fetchone()
        if not row:
            return {"current_goal": "", "active_task_id": None, "completed_steps": [], "next_action": ""}
        return {
            "current_goal": row["current_goal"],
            "active_task_id": row["active_task_id"],
            "completed_steps": json.loads(row["completed_steps"] or "[]"),
            "next_action": row["next_action"],
            "updated_at": row["updated_at"],
        }


def set_working_state(
    *,
    current_goal: str | None = None,
    active_task_id: str | None = None,
    completed_steps: list[str] | None = None,
    next_action: str | None = None,
) -> None:
    """Patch the checkpoint. Only the fields you pass change."""
    with _lock:
        cur = get_working_state()
        new = {
            "current_goal": current_goal if current_goal is not None else cur["current_goal"],
            "active_task_id": active_task_id if active_task_id is not None else cur["active_task_id"],
            "completed_steps": completed_steps if completed_steps is not None else cur["completed_steps"],
            "next_action": next_action if next_action is not None else cur["next_action"],
        }
        _connect().execute(
            "UPDATE working_state SET current_goal = ?, active_task_id = ?, completed_steps = ?, "
            "next_action = ?, updated_at = ? WHERE id = 1",
            (new["current_goal"], new["active_task_id"], json.dumps(new["completed_steps"]),
             new["next_action"], _now()),
        )
        _connect().commit()


# ── Tasks (the ledger) ───────────────────────────────────────────────────────────

_OPEN_STATUSES = ("open", "in_progress", "blocked")


def add_task(title: str, detail: str = "", idempotency_key: str | None = None) -> str:
    tid = uuid.uuid4().hex[:12]
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO tasks (id, title, status, detail, idempotency_key, created_at, updated_at) "
            "VALUES (?, ?, 'open', ?, ?, ?, ?)",
            (tid, title, detail, idempotency_key, _now(), _now()),
        )
        conn.commit()
    return tid


def update_task(
    task_id: str,
    *,
    status: str | None = None,
    note: str | None = None,
    last_step: str | None = None,
) -> bool:
    sets, vals = [], []
    if status is not None:
        sets.append("status = ?"); vals.append(status)
    if note is not None:
        sets.append("note = ?"); vals.append(note)
    if last_step is not None:
        sets.append("last_step = ?"); vals.append(last_step)
    if not sets:
        return False
    sets.append("updated_at = ?"); vals.append(_now())
    vals.append(task_id)
    with _lock:
        conn = _connect()
        cur = conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
        return cur.rowcount > 0


def get_task(task_id: str) -> dict[str, Any] | None:
    with _lock:
        row = _connect().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(include_done: bool = False) -> list[dict[str, Any]]:
    with _lock:
        if include_done:
            rows = _connect().execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        else:
            q = "SELECT * FROM tasks WHERE status IN (%s) ORDER BY created_at" % (
                ",".join("?" * len(_OPEN_STATUSES))
            )
            rows = _connect().execute(q, _OPEN_STATUSES).fetchall()
        return [dict(r) for r in rows]


# ── Messages (append-only conversation log) ──────────────────────────────────────

def append_message(msg: dict[str, Any], trigger: str | None = None) -> int:
    """Persist one chat message exactly as it goes to/comes from the model."""
    tool_calls = msg.get("tool_calls")
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO messages (role, content, tool_calls, tool_call_id, reasoning_content, trigger, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                msg.get("role", ""),
                msg.get("content"),
                json.dumps(tool_calls) if tool_calls else None,
                msg.get("tool_call_id"),
                msg.get("reasoning_content"),
                trigger,
                _now(),
            ),
        )
        conn.commit()
        return cur.lastrowid


def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": row["role"]}
    # content can legitimately be "" for a tool-calling assistant turn — keep it a string.
    msg["content"] = row["content"] if row["content"] is not None else ""
    if row["tool_calls"]:
        msg["tool_calls"] = json.loads(row["tool_calls"])
    if row["tool_call_id"]:
        msg["tool_call_id"] = row["tool_call_id"]
    if row["reasoning_content"]:
        msg["reasoning_content"] = row["reasoning_content"]
    return msg


def messages_since(min_id: int) -> list[dict[str, Any]]:
    """All messages with id >= min_id, oldest→newest. Used to hand a just-finished turn to
    the scribe (min_id = the turn's opening user message)."""
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM messages WHERE id >= ? ORDER BY id", (min_id,)
        ).fetchall()
    return [_row_to_message(r) for r in rows]


# Don't load the whole log to build a window. Page backwards in chunks until we've crossed
# the budget at a turn boundary. SAFETY caps a runaway (a single absurd turn) so we never
# scan unbounded — far above any real turn (MAX_TOOL_ROUNDS=1000).
_RECENT_PAGE = 500
_RECENT_SAFETY_ROWS = 20_000


def recent_turns(char_budget: int = _HISTORY_CHAR_BUDGET) -> list[dict[str, Any]]:
    """Most-recent WHOLE turns that fit the budget, oldest→newest.

    A turn starts at a 'user' message and runs until the next one. We only ever cut on a
    turn boundary (a user message), so an assistant tool_calls message is never separated
    from its tool results (which the API rejects). We page backwards rather than slicing a
    fixed row count: a single turn can run thousands of messages, and a flat LIMIT would
    start the window mid-turn and strip the opening instruction — corrupting the very
    resume case this exists to make clean. If one turn alone exceeds the budget, we keep
    that whole turn (its user anchor included) rather than cut into it.
    """
    out_rev: list[dict[str, Any]] = []  # newest→oldest
    chars = 0
    last_id: int | None = None
    scanned = 0
    seen_user = False  # True once we've captured the WHOLE most-recent turn (its user anchor)
    done = False
    while not done and scanned < _RECENT_SAFETY_ROWS:
        with _lock:
            if last_id is None:
                rows = _connect().execute(
                    "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (_RECENT_PAGE,)
                ).fetchall()
            else:
                rows = _connect().execute(
                    "SELECT * FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?",
                    (last_id, _RECENT_PAGE),
                ).fetchall()
        if not rows:
            break
        scanned += len(rows)
        last_id = rows[-1]["id"]
        for r in rows:
            m = _row_to_message(r)
            size = len(str(m.get("content") or "")) + len(str(m.get("reasoning_content") or ""))
            if m["role"] == "user":
                # An older turn's anchor that pushes us over budget → stop here (exclude it).
                # But the FIRST user we reach is the most-recent turn's own anchor: always keep
                # it, even if that one turn alone blows the budget, so the window is never empty
                # and never starts mid-turn.
                if seen_user and chars + size > char_budget:
                    done = True
                    break
                seen_user = True
            chars += size
            out_rev.append(m)
    out = list(reversed(out_rev))  # oldest→newest
    # Drop any leading fragment of an excluded older turn so the window starts at a user message.
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


# ── Episodes (raw events to consolidate) ─────────────────────────────────────────

def add_episode(kind: str, content: str) -> int:
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO episodes (kind, content, created_at) VALUES (?, ?, ?)",
            (kind, content, _now()),
        )
        conn.commit()
        return cur.lastrowid


def unconsolidated_episodes(limit: int = 200) -> list[dict[str, Any]]:
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM episodes WHERE consolidated = 0 ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def mark_consolidated(episode_ids: list[int]) -> None:
    if not episode_ids:
        return
    with _lock:
        conn = _connect()
        conn.executemany(
            "UPDATE episodes SET consolidated = 1 WHERE id = ?", [(i,) for i in episode_ids]
        )
        conn.commit()


# ── Receipts (audit) ─────────────────────────────────────────────────────────────

def _receipt_digest(action: str, target: str, decision: str, params_digest: str,
                    prev: str, created_at: str) -> str:
    h = hashlib.sha256()
    h.update("\x1f".join((action, target, decision, params_digest, prev, created_at)).encode())
    return h.hexdigest()


def add_receipt(
    action: str,
    *,
    target: str = "",
    params_digest: str = "",
    decision: str,
    reason: str = "",
) -> str:
    """Append a hash-chained receipt and return its digest. The whole chain step — read the
    previous digest, compute this one, insert — happens under one lock, so concurrent callers
    (voice tools + the text loop) can't read the same prev and fork the chain. created_at is in
    the preimage, so two otherwise-identical receipts still get distinct, ordered digests."""
    with _lock:
        conn = _connect()
        created = _now()
        prow = conn.execute(
            "SELECT receipt_id FROM receipts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev = (prow["receipt_id"] if prow and prow["receipt_id"] else "")
        digest = _receipt_digest(action, target, decision, params_digest, prev, created)
        conn.execute(
            "INSERT INTO receipts (action, target, params_digest, decision, reason, receipt_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (action, target, params_digest, decision, reason, digest, created),
        )
        conn.commit()
        return digest


def recent_receipts(limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM receipts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def message_count() -> int:
    with _lock:
        return _connect().execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]


def count_allowed_receipts(action: str, target: str, since_iso: str) -> int:
    """Time-bounded count of ALLOWED receipts for (action[, target]) since an ISO cutoff.
    A real 24h window, not 'within the last N rows' — so the cap holds even on a busy day."""
    with _lock:
        conn = _connect()
        if target:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM receipts WHERE action=? AND decision='allowed' "
                "AND target=? AND created_at >= ?", (action, target, since_iso)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM receipts WHERE action=? AND decision='allowed' "
                "AND created_at >= ?", (action, since_iso)).fetchone()
        return row["n"]


# ── Context compilation — the heart of clean resume ──────────────────────────────

def _heal_tool_calls(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Close any assistant tool_calls that have no matching tool result.

    A turn persists the assistant's tool_calls message BEFORE its tool results (so a crash
    can't lose the decision). If the process dies in that window, the log keeps an assistant
    tool_calls with no following tool messages — and the chat API hard-rejects (400) any
    history where a tool_calls message isn't answered for every tool_call_id. On the next
    boot that orphan would brick every turn. We synthesize a placeholder result for each
    unanswered id so the replayed history is always API-valid.
    """
    out: list[dict[str, Any]] = []
    i, n = 0, len(msgs)
    while i < n:
        m = msgs[i]
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            call_ids = [tc.get("id") for tc in m["tool_calls"] if tc.get("id")]
            answered: set[str] = set()
            j = i + 1
            while j < n and msgs[j].get("role") == "tool":
                out.append(msgs[j])
                answered.add(msgs[j].get("tool_call_id"))
                j += 1
            for cid in call_ids:
                if cid not in answered:
                    out.append({
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": "[no result — the previous run was interrupted before this tool returned]",
                    })
            i = j
            continue
        i += 1
    return out


def _format_tasks(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "(no open tasks)"
    lines = []
    for t in tasks:
        note = f" — {t['note']}" if t.get("note") else ""
        lines.append(f"- [{t['status']}] {t['title']}{note}")
    return "\n".join(lines)


def compile_context() -> dict[str, Any]:
    """Rebuild everything the model needs THIS turn from the store. Identical on a cold
    boot and on any normal turn — that sameness is what makes resume clean.

    Returns:
      system_context: a block to append to the static persona system prompt
      history:        recent whole turns, API-ready
      resume:         the structured checkpoint (active task + next action), for the
                      'first turn after a gap re-checks reality' logic in core
    """
    blocks = all_blocks()
    ws = get_working_state()
    open_tasks = list_tasks()

    active = get_task(ws["active_task_id"]) if ws.get("active_task_id") else None
    steps = ws.get("completed_steps") or []

    parts: list[str] = []
    if blocks.get("identity"):
        parts.append(f"## Who you are\n{blocks['identity'].strip()}")
    if blocks.get("working_narrative"):
        parts.append(f"## Where your head's at\n{blocks['working_narrative'].strip()}")

    work_lines = []
    if ws.get("current_goal"):
        work_lines.append(f"Goal: {ws['current_goal']}")
    if active:
        work_lines.append(f"Active task: {active['title']} (status {active['status']})")
        if active.get("last_step"):
            work_lines.append(f"Last step recorded: {active['last_step']}")
    if steps:
        work_lines.append("Steps done: " + "; ".join(steps[-8:]))
    if ws.get("next_action"):
        work_lines.append(f"Next action: {ws['next_action']}")
    if work_lines:
        parts.append("## What you're in the middle of right now\n" + "\n".join(work_lines))

    parts.append("## Open tasks (your ledger)\n" + _format_tasks(open_tasks))

    learned = "\n\n".join(
        f"### {label.replace('_', ' ').title()}\n{blocks[label].strip()}"
        for label in DREAM_BLOCKS
        if blocks.get(label, "").strip()
    )
    if learned:
        parts.append("## What you've learned\n" + learned)

    return {
        "system_context": "\n\n".join(parts),
        "history": _heal_tool_calls(recent_turns()),
        "resume": {
            "active_task": active,
            "next_action": ws.get("next_action", ""),
            "current_goal": ws.get("current_goal", ""),
        },
    }


def live_snapshot(max_chars: int = 1400) -> str:
    """A tight 'what Miles is doing right now' briefing for an inbound voice call —
    read from the same store the text loop uses, so voice is never out of date."""
    ws = get_working_state()
    open_tasks = list_tasks()
    active = get_task(ws["active_task_id"]) if ws.get("active_task_id") else None

    lines: list[str] = []
    if ws.get("current_goal"):
        lines.append(f"Right now you're focused on: {ws['current_goal']}.")
    if active:
        lines.append(f"Active task: {active['title']} ({active['status']}).")
    if ws.get("next_action"):
        lines.append(f"Your next move: {ws['next_action']}.")
    if open_tasks:
        top = "; ".join(t["title"] for t in open_tasks[:5])
        lines.append(f"Open on your plate: {top}.")
    recent = [r for r in recent_receipts(6) if r["decision"] == "allowed"]
    if recent:
        acts = "; ".join(f"{r['action']} {r['target']}".strip() for r in recent[:4])
        lines.append(f"Recently you: {acts}.")
    out = " ".join(lines).strip()
    return out[:max_chars]
