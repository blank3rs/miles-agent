"""One-time importer: fold the old scattered state into miles.db, then never again.

Sources (each optional — on a fresh box none may exist):
  agent_state.json  → messages (history) + the 'working_narrative' block (summary)
  tasks.json        → tasks
  soul.md           → 'identity' block (head) + dream-owned blocks (learning/people/matters_now)
  journal/*.jsonl   → episodes (marked consolidated; they were already dreamed)

Idempotent: drops a .migrated_to_sqlite sentinel and no-ops if it's present. Run with
`python -m agent.migrate` (or call migrate() at startup).
"""
from __future__ import annotations

import json
import re

import structlog

from agent import store
from agent.config import (
    AGENT_STATE_FILE,
    DATA_DIR,
    JOURNAL_DIR,
    SOUL_FILE,
    TASKS_FILE,
)

log = structlog.get_logger()

_SENTINEL = DATA_DIR / ".migrated_to_sqlite"

# soul.md dream-section header → store block label
_SOUL_SECTION_TO_BLOCK = {
    "## What I'm learning": "learning",
    "## People I know": "people",
    "## Things that matter right now": "matters_now",
}


def _import_agent_state() -> str:
    if not AGENT_STATE_FILE.exists():
        return "agent_state.json: none"
    try:
        data = json.loads(AGENT_STATE_FILE.read_text())
    except Exception as e:
        return f"agent_state.json: unreadable ({e})"

    n = 0
    for msg in data.get("history", []):
        if not isinstance(msg, dict) or not msg.get("role"):
            continue
        store.append_message(msg)
        n += 1

    summary = (data.get("summary") or "").strip()
    if summary:
        # The old prose summary becomes flavor narrative, NOT the resume primitive.
        store.set_block("working_narrative", summary)
    return f"agent_state.json: {n} messages, summary={'yes' if summary else 'no'}"


def _import_tasks() -> str:
    if not TASKS_FILE.exists():
        return "tasks.json: none"
    try:
        rows = json.loads(TASKS_FILE.read_text())
    except Exception as e:
        return f"tasks.json: unreadable ({e})"

    n = 0
    for t in rows:
        if not isinstance(t, dict) or not t.get("title"):
            continue
        tid = store.add_task(t["title"], detail="")
        status = t.get("status") or "open"
        note = t.get("notes") or t.get("note") or ""
        if status != "open" or note:
            store.update_task(tid, status=status, note=note)
        n += 1
    return f"tasks.json: {n} tasks"


def _import_soul() -> str:
    if not SOUL_FILE.exists():
        return "soul.md: none"
    text = SOUL_FILE.read_text()

    # Identity = everything before the first dream-owned section.
    first = len(text)
    for header in _SOUL_SECTION_TO_BLOCK:
        m = re.search(rf"^{re.escape(header)}\s*$", text, re.M)
        if m:
            first = min(first, m.start())
    identity = text[:first].strip()
    if identity:
        store.set_block("identity", identity)

    imported = ["identity"] if identity else []
    for header, label in _SOUL_SECTION_TO_BLOCK.items():
        m = re.search(rf"^{re.escape(header)}\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
        if m and m.group(1).strip():
            store.set_block(label, m.group(1).strip())
            imported.append(label)
    return f"soul.md: blocks {', '.join(imported) or '(none)'}"


def _import_journal() -> str:
    if not JOURNAL_DIR.exists():
        return "journal: none"
    n = 0
    for f in sorted(JOURNAL_DIR.glob("*.jsonl")):
        for line in f.read_text().strip().splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            eid = store.add_episode(e.get("type", "note"), e.get("content", ""))
            store.mark_consolidated([eid])  # already dreamed in the old world
            n += 1
    return f"journal: {n} episodes (pre-consolidated)"


def migrate(force: bool = False) -> str:
    """Import old state into miles.db. No-op if already migrated unless force=True."""
    store.init_db()
    if _SENTINEL.exists() and not force:
        return "already migrated (sentinel present) — skipping"

    lines = [
        _import_soul(),         # identity first, so blocks are seeded before messages reference them
        _import_agent_state(),
        _import_tasks(),
        _import_journal(),
    ]
    try:
        _SENTINEL.write_text(store._now())
    except Exception as e:
        log.warning("migrate_sentinel_failed", err=str(e))
    summary = "Migrated to miles.db:\n  " + "\n  ".join(lines)
    log.info("migration_complete", detail=summary)
    return summary


if __name__ == "__main__":
    print(migrate())
