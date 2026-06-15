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

from agent.config import DATA_DIR, MILES_DB

log = structlog.get_logger()

# The inbox watermark file the inbox watcher advances (server._SEEN_HISTORY_FILE). Read as
# plain text and folded into the sleep-time briefing's staleness stamp — new mail bumps it,
# which invalidates a briefing computed before that mail arrived. Kept here so compile_context
# can recompute the stamp without a server import (store has no server dependency).
_INBOX_WATERMARK_FILE = DATA_DIR / "last_gmail_history.txt"


def _inbox_watermark() -> str:
    try:
        return _INBOX_WATERMARK_FILE.read_text().strip()
    except Exception:
        return ""

CHARS_PER_TOKEN = 4
# How much recent conversation to compile into the window. The rest stays in the log and
# is represented by working_state + the dream-owned blocks + on-demand memory search.
HISTORY_TOKEN_BUDGET = 52_000
_HISTORY_CHAR_BUDGET = HISTORY_TOKEN_BUDGET * CHARS_PER_TOKEN
# Cap how many over-budget older turns we pin verbatim just because they touch open work (B1).
# Without a cap, one long-running task (a deal worked across many heartbeats, each re-mentioning
# it) pins turns without bound and the "bounded" window grows past the model's context window.
# Past the cap, older open-work turns fall through to the rolling summary (B2) instead.
_MAX_PINNED_OPEN_TURNS = 6

# Dream-owned blocks the sleep-time agent maintains; the waking agent only reads them.
DREAM_BLOCKS = ("learning", "people", "matters_now")
# All blocks, with the char limit that keeps the prompt from bloating (Letta pattern).
_DEFAULT_BLOCKS: dict[str, int] = {
    "identity":          6000,   # who Miles is — the durable self (seeded from soul.md head)
    "working_narrative": 3000,   # a few lines of "where my head's at" — flavor, NOT the resume primitive
    "history_summary":   4000,   # harness-owned rolling summary of older turns dropped from the window (B1)
    "learning":          4000,   # dream-owned
    "people":            5000,   # dream-owned
    "matters_now":       3000,   # dream-owned
    "pre_reasoned":      1500,   # sleep-time anticipatory briefing — written idle, shown only while its stamp matches
}

# Sentinel that separates the staleness stamp from the briefing body inside the
# pre_reasoned block. The stamp is a hash of the briefing's inputs (open task ids +
# last message id + inbox watermark); the section is suppressed when state has moved
# past it. Keep on its own line so the split is unambiguous.
_PRE_REASONED_SEP = "\n---stamp---\n"

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
            tool_kind         TEXT,
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
            message_id    INTEGER,
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS facts (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            statement         TEXT NOT NULL,
            statement_key     TEXT NOT NULL,
            embedding         BLOB,
            valid_from        TEXT NOT NULL,
            valid_to          TEXT,
            recorded_at       TEXT NOT NULL,
            expired_at        TEXT,
            source_episode_id INTEGER,
            confidence        REAL NOT NULL DEFAULT 0.7
        );

        CREATE INDEX IF NOT EXISTS idx_episodes_unconsolidated
            ON episodes (consolidated, id);
        """
    )
    # CREATE TABLE IF NOT EXISTS won't alter an existing table on a live miles.db, so add the
    # new columns idempotently (in-store forward-only migration; do not touch agent/migrate.py).
    # A re-run where the column already exists raises OperationalError and is a no-op.
    for ddl in (
        "ALTER TABLE messages ADD COLUMN tool_kind TEXT",
        "ALTER TABLE receipts ADD COLUMN message_id INTEGER",
        # Partial-unique on the content-addressed key makes a re-ADD of a still-valid fact a
        # no-op (INSERT OR IGNORE), which is what gives reconciliation idempotency under retry.
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_valid_key ON facts (statement_key) "
        "WHERE valid_to IS NULL AND expired_at IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_facts_valid ON facts (valid_to, expired_at)",
        # Backs the params_digest-keyed dedup query (action_already_fired) used by the
        # escalation/retry boundary to detect 'this exact action already fired'.
        "CREATE INDEX IF NOT EXISTS idx_receipts_action_digest "
        "ON receipts (action, params_digest, created_at)",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
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


def get_history_summary() -> str:
    """The harness-owned rolling summary of older turns the budget dropped from the window.
    Stored as a memory_block so it's deterministic from the store (resume-safe). NOT dream-owned."""
    return get_block("history_summary")


def set_history_summary(content: str) -> None:
    """Write the rolling summary (clamped to its block char limit). Generation is async (B2);
    this is just the durable sink."""
    set_block("history_summary", content)


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

def append_message(
    msg: dict[str, Any], trigger: str | None = None, tool_kind: str | None = None
) -> int:
    """Persist one chat message exactly as it goes to/comes from the model."""
    tool_calls = msg.get("tool_calls")
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO messages (role, content, tool_calls, tool_call_id, reasoning_content, trigger, tool_kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg.get("role", ""),
                msg.get("content"),
                json.dumps(tool_calls) if tool_calls else None,
                msg.get("tool_call_id"),
                msg.get("reasoning_content"),
                trigger,
                tool_kind,
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
    # tool_kind is only present on tool-result rows (and only on newer ones, post-C2). Carry
    # it through so the eviction post-pass can target action results. Never sent to the model.
    try:
        tk = row["tool_kind"]
    except (IndexError, KeyError):
        tk = None  # an older SELECT that didn't project tool_kind
    if tk:
        msg["tool_kind"] = tk
    # Transient row id, used by the eviction post-pass to match a receipt to its tool row.
    # Stripped (with tool_kind) before the history is returned, so it never reaches the model.
    try:
        msg["_id"] = row["id"]
    except (IndexError, KeyError):
        pass
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

    B1: an older turn that the budget would exclude is kept verbatim ANYWAY if it touches
    open work — any open task id or the active_task_id appears in its text. That's the hard
    'never drop open/unfinished work' rule; the excluded non-open older turns are represented
    by the rolling history_summary (generated async in B2), not regenerated here.
    """
    # Open-work refs, loaded once: anything referencing these must survive eviction.
    open_ids = {t["id"] for t in list_tasks()}
    active_id = get_working_state().get("active_task_id")
    if active_id:
        open_ids.add(active_id)

    def _turn_touches_open(turn_rows: list[dict[str, Any]]) -> bool:
        if not open_ids:
            return False
        blob = " ".join(
            str(m.get("content") or "") + " " + str(m.get("reasoning_content") or "")
            for m in turn_rows
        )
        return any(tid in blob for tid in open_ids)

    out_rev: list[dict[str, Any]] = []  # newest→oldest
    chars = 0
    last_id: int | None = None
    scanned = 0
    seen_user = False  # True once we've captured the WHOLE most-recent turn (its user anchor)
    turn_start = 0     # index in out_rev where the current (still-open) turn's body began
    done = False
    pinned_open = 0    # (B1) count of over-budget open-work turns pinned verbatim, capped
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
                # An older turn's anchor that pushes us over budget → normally stop here
                # (exclude it). But the FIRST user we reach is the most-recent turn's own anchor:
                # always keep it, even if that one turn alone blows the budget, so the window is
                # never empty and never starts mid-turn. And (B1) keep an over-budget older turn
                # verbatim if it touches open work — the rows since this anchor (already in
                # out_rev) plus the anchor itself are that whole turn.
                if seen_user and chars + size > char_budget:
                    # (B1) keep an over-budget older turn verbatim if it touches open work —
                    # but only up to a cap, or one long-running task pins turns without bound
                    # and the window grows past the model's context window. Past the cap, this
                    # and everything older fall through to the rolling summary (turns_outside_budget).
                    if pinned_open < _MAX_PINNED_OPEN_TURNS and _turn_touches_open([m, *out_rev[turn_start:]]):
                        pinned_open += 1
                        chars += size
                        out_rev.append(m)
                        turn_start = len(out_rev)
                        continue
                    done = True
                    break
                seen_user = True
                turn_start = len(out_rev) + 1  # next row begins the next (older) turn's body
            chars += size
            out_rev.append(m)
    out = list(reversed(out_rev))  # oldest→newest
    # Drop any leading fragment of an excluded older turn so the window starts at a user message.
    while out and out[0]["role"] != "user":
        out.pop(0)
    return _collapse_receipted_actions(out)


def turns_outside_budget(
    char_budget: int = _HISTORY_CHAR_BUDGET, max_turns: int = 12
) -> list[list[dict[str, Any]]]:
    """The older turns the verbatim window (recent_turns) dropped, oldest→newest, for the
    async rolling summary (B2). Mirrors recent_turns' backward page-scan and turn boundaries
    so the two stay in lock-step: a turn is excluded exactly when its user anchor would push
    past the budget AND it does not touch open work (an open-work turn is kept verbatim in the
    window, so it must NOT also be summarized here). Capped at the most-recent `max_turns`
    excluded turns so the scribe folds a bounded slice each call — ancient ones are already
    represented by the previous summary it folds in.

    Each element is one whole turn (its user anchor + body rows), with the storage-only
    tool_kind/_id tags stripped. Returns [] when nothing fell outside the budget."""
    open_ids = {t["id"] for t in list_tasks()}
    active_id = get_working_state().get("active_task_id")
    if active_id:
        open_ids.add(active_id)

    def _touches_open(turn_rows: list[dict[str, Any]]) -> bool:
        if not open_ids:
            return False
        blob = " ".join(
            str(m.get("content") or "") + " " + str(m.get("reasoning_content") or "")
            for m in turn_rows
        )
        return any(tid in blob for tid in open_ids)

    excluded_rev: list[list[dict[str, Any]]] = []  # newest→oldest, each a whole turn
    out_rev: list[dict[str, Any]] = []
    chars = 0
    last_id: int | None = None
    scanned = 0
    seen_user = False
    turn_start = 0
    done = False
    pinned_open = 0       # mirror recent_turns' pin cap so the two agree on what's kept
    window_open = True    # False once recent_turns' verbatim window has closed
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
                if seen_user and chars + size > char_budget:
                    whole_turn = [m, *out_rev[turn_start:]]  # this anchor + its body
                    # Mirror recent_turns EXACTLY: it keeps an over-budget open-work turn verbatim
                    # only while under the pin cap AND while its window is still open; once a
                    # non-open over-budget turn is hit or the cap is reached, recent_turns stops
                    # and everything from there back is summarized here. window_open tracks that
                    # close so a turn is never kept in NEITHER the window nor the summary.
                    if window_open and pinned_open < _MAX_PINNED_OPEN_TURNS and _touches_open(whole_turn):
                        pinned_open += 1
                        chars += size
                        out_rev.append(m)
                        turn_start = len(out_rev)
                        continue
                    window_open = False
                    excluded_rev.append([{k: v for k, v in mm.items()
                                          if k not in ("tool_kind", "_id")}
                                         for mm in reversed(whole_turn)])
                    if len(excluded_rev) >= max_turns:
                        done = True
                        break
                    # keep scanning back so we collect a bounded run of older turns,
                    # but stop counting toward the budget (they're all excluded now)
                    out_rev = []
                    turn_start = 0
                    continue
                seen_user = True
                turn_start = len(out_rev) + 1
            chars += size
            out_rev.append(m)
    return list(reversed(excluded_rev))  # oldest→newest


def _collapse_receipted_actions(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Across-turn graduated eviction (C3): for a tool result whose originating call was an
    ACTION and which has an ALLOWED receipt linked to it, replace its content with the one-line
    receipt and drop nothing else — the row stays so its tool_call_id is still answered and the
    _heal_tool_calls invariant holds. Exploratory results stay verbatim at this stage (the
    open-work dependency join is a later refinement; collapsing action receipts is the 80%).

    Returns the same list with the storage-only `tool_kind`/`_id` tags stripped from every
    message — the chat API rejects unknown keys, so they must never reach the model."""
    linked = {
        mid: r
        for mid, r in recent_receipts_by_message().items()
        if r.get("decision") == "allowed"
    }
    out: list[dict[str, Any]] = []
    for m in msgs:
        kind = m.get("tool_kind")
        mid = m.get("_id")
        new = {k: v for k, v in m.items() if k not in ("tool_kind", "_id")}
        if m.get("role") == "tool" and kind == "action" and mid in linked:
            new["content"] = receipt_line(linked[mid])
        out.append(new)
    return out


def receipt_line(r: dict[str, Any]) -> str:
    """The one-line receipt string that replaces a receipted action's full tool transcript —
    in the compiled window (across-turn) and in-turn compaction alike, so both read identically."""
    target = (r.get("target") or "").strip()
    reason = (r.get("reason") or "").strip()
    rid = (r.get("receipt_id") or "")[:12]
    head = f"{r['action']} {target}".strip()
    return f"[receipt {head} — {r['decision']}: {reason} ({rid})]"


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


# ── Facts (bi-temporal knowledge) ────────────────────────────────────────────────
# Normalized atomic statements with valid-time (when true in the world) and transaction-time
# (when we learned/retracted it). Supersession is non-destructive — the old row keeps history.
# The reconciler (agent/facts.py) is the only writer of UPDATE/DELETE ops; it always goes
# through these fixed parameterized helpers, never authored SQL. statement_key is the
# content-addressed dedup key; computed here so store stays standalone-importable (no facts
# import, which would cycle: facts imports store).

def _fact_key(statement: str) -> str:
    """Content-addressed dedup key for a fact: sha256 of the normalized statement, truncated.
    Kept inline (not imported from agent.facts) so store has no dependency back on facts."""
    norm = " ".join((statement or "").lower().split()).rstrip(".,;:!?-—")
    return hashlib.sha256(norm.encode()).hexdigest()[:32]


def add_fact(
    statement: str,
    *,
    embedding: bytes | None,
    valid_from: str | None = None,
    source_episode_id: int | None = None,
    confidence: float = 0.7,
) -> int | None:
    """Insert a new currently-valid fact. INSERT OR IGNORE on the partial-unique statement_key
    so a re-ADD of a still-valid duplicate is a no-op (returns None). recorded_at = now;
    valid_from defaults to now; valid_to/expired_at stay NULL (currently valid + believed)."""
    statement = (statement or "").strip()
    if not statement:
        return None
    now = _now()
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT OR IGNORE INTO facts "
            "(statement, statement_key, embedding, valid_from, valid_to, recorded_at, expired_at, "
            "source_episode_id, confidence) VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?)",
            (statement, _fact_key(statement), embedding, valid_from or now, now,
             source_episode_id, confidence),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount else None


def expire_fact(fact_id: int) -> None:
    """Supersede a fact (the old row of an UPDATE): set BOTH valid_to and expired_at, so it
    reads as 'was true, no longer is, and we know why (replaced)'. Idempotent via the guard."""
    now = _now()
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE facts SET valid_to = ?, expired_at = ? WHERE id = ? AND expired_at IS NULL",
            (now, now, fact_id),
        )
        conn.commit()


def retract_fact(fact_id: int) -> None:
    """Retract a fact (a DELETE op): set ONLY expired_at, leaving valid_to NULL, so 'we stopped
    believing it' stays distinguishable from 'it was superseded'. Idempotent via the guard."""
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE facts SET expired_at = ? WHERE id = ? AND expired_at IS NULL",
            (_now(), fact_id),
        )
        conn.commit()


def currently_valid_facts(limit: int = 2000) -> list[dict[str, Any]]:
    """Facts that are still true and still believed (valid_to IS NULL AND expired_at IS NULL),
    newest-first. embedding comes back as raw bytes (or None). Bounded by `limit` so brute-force
    recall scans at most that many rows — reconciliation keeps the valid set small by design."""
    with _lock:
        rows = _connect().execute(
            "SELECT id, statement, embedding, recorded_at, confidence FROM facts "
            "WHERE valid_to IS NULL AND expired_at IS NULL ORDER BY recorded_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def fact_for_episode(episode_id: int) -> dict[str, Any] | None:
    """The most recent fact derived from a given episode, if any — lets the reconciler skip an
    episode it already turned into a fact (idempotency alongside the statement_key guard)."""
    with _lock:
        row = _connect().execute(
            "SELECT * FROM facts WHERE source_episode_id = ? ORDER BY id DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        return dict(row) if row else None


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
    message_id: int | None = None,
) -> str:
    """Append a hash-chained receipt and return its digest. The whole chain step — read the
    previous digest, compute this one, insert — happens under one lock, so concurrent callers
    (voice tools + the text loop) can't read the same prev and fork the chain. created_at is in
    the preimage, so two otherwise-identical receipts still get distinct, ordered digests.

    message_id links the receipt to the tool-result row it receipts (C3 eviction). It is NOT
    in the digest preimage — the hash chain stays byte-identical to existing receipts."""
    with _lock:
        conn = _connect()
        created = _now()
        prow = conn.execute(
            "SELECT receipt_id FROM receipts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev = (prow["receipt_id"] if prow and prow["receipt_id"] else "")
        digest = _receipt_digest(action, target, decision, params_digest, prev, created)
        conn.execute(
            "INSERT INTO receipts (action, target, params_digest, decision, reason, receipt_id, message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (action, target, params_digest, decision, reason, digest, message_id, created),
        )
        conn.commit()
        return digest


def recent_receipts(limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM receipts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def recent_receipts_by_message(limit: int = 200) -> dict[int, dict[str, Any]]:
    """{message_id: {action, target, decision, reason, receipt_id}} for the most-recent
    receipts that carry a message_id. The eviction post-pass in recent_turns() uses this to
    collapse a receipted action's full tool transcript down to its one-line receipt. Keyed by
    the tool-result message_id the receipt was linked to (see set_receipt_message_id)."""
    with _lock:
        rows = _connect().execute(
            "SELECT message_id, action, target, decision, reason, receipt_id FROM receipts "
            "WHERE message_id IS NOT NULL ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:  # newest-first → first write per message_id wins (the latest receipt)
        mid = r["message_id"]
        if mid not in out:
            out[mid] = {
                "action": r["action"],
                "target": r["target"],
                "decision": r["decision"],
                "reason": r["reason"],
                "receipt_id": r["receipt_id"],
            }
    return out


def set_receipt_message_id(receipt_id: str, message_id: int) -> None:
    """Back-link a receipt to the tool-result row it receipts, once that row has been
    persisted (the receipt is written inside the handler, before the row exists). Only sets
    when still NULL so a re-link can't clobber. message_id is NOT in the hash chain, so this
    leaves the receipt digest untouched."""
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE receipts SET message_id = ? WHERE receipt_id = ? AND message_id IS NULL",
            (message_id, receipt_id),
        )
        conn.commit()


def message_count() -> int:
    with _lock:
        return _connect().execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]


def max_message_id() -> int:
    """The id of the most recent message, or 0 if the log is empty. Used as one input to the
    sleep-time briefing's staleness stamp — any new turn bumps it, invalidating a stale
    pre-reasoned briefing built before that turn."""
    with _lock:
        row = _connect().execute("SELECT MAX(id) AS m FROM messages").fetchone()
        return row["m"] or 0


# ── Sleep-time pre-reasoned briefing (anticipatory, idle-computed) ────────────────

def pre_reasoned_stamp(inbox_watermark: str = "") -> str:
    """A short hash of the briefing's inputs: the open task ids + the last message id +
    the inbox watermark. The sleep-time coro writes this alongside the briefing; compile_context
    recomputes it and shows the briefing only if it still matches — so a briefing is surfaced only
    while the world it reasoned over is still current, and silently suppressed once anything moves.

    Reads the store under the same lock as everything else (the inbox watermark is passed in by
    the caller, which owns that file). Cheap — three deterministic reads, no model call."""
    open_ids = sorted(t["id"] for t in list_tasks())
    last_id = max_message_id()
    preimage = "\x1f".join(("|".join(open_ids), str(last_id), inbox_watermark or ""))
    return hashlib.sha256(preimage.encode()).hexdigest()[:16]


def set_pre_reasoned(briefing: str, stamp: str) -> None:
    """Store the sleep-time briefing stamped with the hash of the inputs it reasoned over.
    Body is clamped by set_block to the pre_reasoned char_limit; the stamp prefix is tiny."""
    set_block("pre_reasoned", f"{stamp}{_PRE_REASONED_SEP}{(briefing or '').strip()}")


def get_pre_reasoned() -> tuple[str, str]:
    """(stamp, briefing) for the stored sleep-time briefing, or ('', '') if none/malformed."""
    raw = get_block("pre_reasoned")
    if not raw or _PRE_REASONED_SEP not in raw:
        return "", ""
    stamp, _, body = raw.partition(_PRE_REASONED_SEP)
    return stamp.strip(), body.strip()


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


def action_already_fired(
    action: str, target: str, params_digest: str, since_iso: str
) -> dict[str, Any] | None:
    """The most recent ALLOWED receipt matching (action, params_digest[, target]) since a cutoff,
    or None. params_digest-keyed (distinct from count_allowed_receipts, which is time/target-only)
    so a retry after a crash or a confidence escalation can detect 'this EXACT action already went
    out' and skip re-execution. An empty params_digest never matches (legacy receipts carry '' and
    must not produce a false 'already fired'). Read under _lock like every other store query."""
    if not params_digest:
        return None
    with _lock:
        conn = _connect()
        if target:
            row = conn.execute(
                "SELECT * FROM receipts WHERE action=? AND decision='allowed' AND params_digest=? "
                "AND target=? AND created_at >= ? ORDER BY id DESC LIMIT 1",
                (action, params_digest, target, since_iso),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM receipts WHERE action=? AND decision='allowed' AND params_digest=? "
                "AND created_at >= ? ORDER BY id DESC LIMIT 1",
                (action, params_digest, since_iso),
            ).fetchone()
        return dict(row) if row else None


def allowed_receipts_since(since_iso: str) -> list[dict[str, Any]]:
    """Every ALLOWED receipt since an ISO cutoff, oldest→newest. Used by the confidence-escalation
    re-entry to inject a '[system] these actions already completed this turn' note so the
    orchestrator is grounded not to repeat them. Read under _lock like every other store query."""
    with _lock:
        rows = _connect().execute(
            "SELECT action, target, params_digest, decision, reason, receipt_id, created_at "
            "FROM receipts WHERE decision='allowed' AND created_at >= ? ORDER BY id",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]


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
    # Char weight per section, so the caller can attribute memory-context tokens to the
    # block they came from (instrumentation only — does not change content or order).
    section_chars: dict[str, int] = {
        "identity": 0,
        "working_narrative": 0,
        "working_state": 0,
        "history_summary": 0,
        "ledger": 0,
        "dream_blocks": 0,
        "facts": 0,
        "pre_reasoned": 0,
        "receipts": 0,
    }
    if blocks.get("identity"):
        part = f"## Who you are\n{blocks['identity'].strip()}"
        section_chars["identity"] = len(part)
        parts.append(part)
    if blocks.get("working_narrative"):
        part = f"## Where your head's at\n{blocks['working_narrative'].strip()}"
        section_chars["working_narrative"] = len(part)
        parts.append(part)

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
        part = "## What you're in the middle of right now\n" + "\n".join(work_lines)
        section_chars["working_state"] = len(part)
        parts.append(part)

    # The rolling summary of older turns the budget dropped from the verbatim window (B1).
    # The verbatim window in `history` still always starts at a user anchor; this is ABOUT
    # the turns before it, never a substitute for a user message.
    summary = get_history_summary().strip()
    if summary:
        part = "## Earlier this stretch (summary)\n" + summary
        section_chars["history_summary"] = len(part)
        parts.append(part)

    ledger = "## Open tasks (your ledger)\n" + _format_tasks(open_tasks)
    section_chars["ledger"] = len(ledger)
    parts.append(ledger)

    learned = "\n\n".join(
        f"### {label.replace('_', ' ').title()}\n{blocks[label].strip()}"
        for label in DREAM_BLOCKS
        if blocks.get(label, "").strip()
    )
    if learned:
        part = "## What you've learned\n" + learned
        section_chars["dream_blocks"] = len(part)
        parts.append(part)

    # Auto-injected facts (bi-temporal knowledge store). The accumulated, currently-valid facts
    # most relevant to what Miles is doing RIGHT NOW — seeded from working_state so they enter the
    # turn without him having to call search_facts. On THIS hot path recall runs keyword+recency
    # only (allow_embed=False) — pure-SQLite, no OpenAI round-trip — so compiling a turn can never
    # block the event loop on a network embed; cosine is reserved for the explicit search_facts
    # tool (which runs in asyncio.to_thread, where blocking is fine). facts is imported lazily
    # because facts imports store (cycle) and so store stays standalone-importable.
    seed_query = " ".join(
        s for s in (
            ws.get("current_goal") or "",
            ws.get("next_action") or "",
            (active or {}).get("title") or "",
        ) if s
    ).strip()
    if seed_query:
        try:
            from agent import facts as _facts

            fact_rows = _facts.recall_facts(seed_query, k=8, max_chars=1600, allow_embed=False)
            if fact_rows:
                fact_lines = "\n".join(f"- {r['statement']}" for r in fact_rows)
                part = "## What I know (facts)\n" + fact_lines
                section_chars["facts"] = len(part)
                parts.append(part)
        except Exception as e:  # never let recall break a turn's compile
            log.warning("facts_inject_failed", err=str(e))

    # Sleep-time anticipatory briefing (precomputed while idle on the worker tier). Shown ONLY
    # while its staleness stamp still matches current state — recompute the stamp from the open
    # ledger + last message id + the live inbox watermark and compare. If anything has moved
    # (a turn ran, a task changed, new mail arrived) the briefing reasoned over a stale world,
    # so it's suppressed rather than shown wrong. This NEVER substitutes for a user message; it's
    # a heads-up the brain may use or ignore.
    pr_stamp, pr_body = get_pre_reasoned()
    if pr_body and pr_stamp == pre_reasoned_stamp(_inbox_watermark()):
        part = "## Heads-up (precomputed while you were idle)\n" + pr_body
        section_chars["pre_reasoned"] = len(part)
        parts.append(part)

    # A short, durable trail of recent allowed actions — the one-line receipts that the
    # transcript itself has been collapsed down to (C3). Small (last ~6) on purpose; the full
    # audit chain lives in the receipts table, this is just enough for situational continuity.
    recent_acts = [r for r in recent_receipts(6) if r["decision"] == "allowed"]
    if recent_acts:
        receipts_part = "## Recent receipts (your last actions)\n" + "\n".join(
            receipt_line(r) for r in reversed(recent_acts)
        )
        section_chars["receipts"] = len(receipts_part)
        parts.append(receipts_part)

    return {
        "system_context": "\n\n".join(parts),
        "section_chars": section_chars,
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
