"""Shared tool classification and gating policy — pure metadata, no wiring yet.

Two orthogonal concerns live here, both keyed by tool name:

  TOOL_KIND  — what a tool *does* to the world. Used by receipt-based eviction
               (change-set C): an ACTION leaves a durable side effect (email sent,
               cron created, file written) and collapses to a one-line receipt once
               done; an EXPLORATORY result (search/read) is scratch we keep only
               while an open action still depends on it.

  PRECONDITIONS — a deterministic predicate over authoritative runtime/miles.db
               state, enforced at *dispatch* (change-set E), so it can't be talked
               around by the brain. A predicate returns None when the tool may run,
               or a short string explaining why it may not (surfaced back to the
               model so it can adapt rather than failing the turn).

Why destructive / external-comms tools are NOT in PRECONDITIONS
---------------------------------------------------------------
gmail (send_email) and web_cli already SELF-GATE inside their own handlers with
domain-specific logic that a generic predicate here would only duplicate or fight:
  - send_email enforces a per-recipient rolling anti-spam cap (Akshay exempt) and
    routes/withholds anything sensitive per the persona security rules.
  - web_cli enforces a per-site/day runaway cap and records an audit decision.
Those guards are unbypassable where they live (the handler), and the persona's
secrets / trust / external-comms / approval constraints are the source of truth for
*what* may go out. Re-encoding any of that here would risk drift from those verbatim
constraints. So PRECONDITIONS is reserved for state-machine gates that the tool
*cannot* check for itself — e.g. "is a call active right now" — and starts empty
apart from the make_call stub below (filled in by change-set E1).
"""
from typing import Callable, Optional

import structlog

log = structlog.get_logger()


class TOOL_KIND:
    """Side-effect class of a tool, for receipt-based graduated eviction (C)."""

    ACTION = "action"            # durable side effect: send, create, write, schedule, spend
    EXPLORATORY = "exploratory"  # read-only / search: result is scratch, evictable once unused


# Explicit per-tool side-effect class. Anything not listed defaults to EXPLORATORY
# via tool_kind() — read-only is the safe default for eviction (we never collapse a
# durable receipt we didn't record). Keep this in sync with TOOL_HANDLERS; the
# soft validation below flags drift at startup without ever raising.
_TOOL_KINDS: dict[str, str] = {
    # ── external comms / scheduling: durable, receiptable ──────────────────────
    "send_email": TOOL_KIND.ACTION,
    "make_call": TOOL_KIND.ACTION,
    "create_calendar_event": TOOL_KIND.ACTION,
    "respond_to_calendar_event": TOOL_KIND.ACTION,
    "set_heartbeat": TOOL_KIND.ACTION,
    "cancel_heartbeat": TOOL_KIND.ACTION,
    # ── task ledger mutations ──────────────────────────────────────────────────
    "add_task": TOOL_KIND.ACTION,
    "update_task": TOOL_KIND.ACTION,
    "set_focus": TOOL_KIND.ACTION,
    # ── filesystem / sandbox writes ────────────────────────────────────────────
    "edit_file": TOOL_KIND.ACTION,
    "write_sandbox_file": TOOL_KIND.ACTION,
    "install_package": TOOL_KIND.ACTION,
    "exec_sandboxed": TOOL_KIND.ACTION,
    # ── memory / journal writes ────────────────────────────────────────────────
    "journal_entry": TOOL_KIND.ACTION,
    "dream": TOOL_KIND.ACTION,
    # ── secrets store mutations (reads stay exploratory) ───────────────────────
    "store_secret": TOOL_KIND.ACTION,
    "delete_secret": TOOL_KIND.ACTION,
    # ── skills authoring / install ─────────────────────────────────────────────
    "create_skill": TOOL_KIND.ACTION,
    "download_github_skill": TOOL_KIND.ACTION,
    # ── browser side-effecting ─────────────────────────────────────────────────
    "reset_browser_profile": TOOL_KIND.ACTION,
    # Everything else (search_web, exa_search, scrape_url, web_cli, browser_task,
    # read_*, list_*, analyze_*, run_*, search/retrieve memory, search_facts, get_secret,
    # check/list tasks, find_free_slots, contact lookups, captcha, screenshots,
    # subagents) is read-only or scratch → EXPLORATORY by default.
    #
    # Note: search_facts is read-only over the local facts table → EXPLORATORY (correct default).
    # The fact reconciler (agent/facts.reconcile_facts) WRITES facts but is harness-internal memory
    # maintenance run by the scribe — like the scribe's episode/working_state writes, it is NOT a
    # tool, mints NO receipt, and needs NO _TOOL_KINDS entry here.
}


def tool_kind(name: str) -> str:
    """Side-effect class for a tool. Unknown / unlisted tools default to EXPLORATORY
    (safe for eviction: we only collapse to a receipt what we explicitly marked an action)."""
    return _TOOL_KINDS.get(name, TOOL_KIND.EXPLORATORY)


# A precondition is `(params) -> Optional[str]`: None means "may run", a string is the
# human-readable reason it may not (returned to the model so it can adapt). `params` is the
# model's parsed tool arguments; world-state is read live from `runtime` (call time, never
# import time — see runtime.py docstring) so the gate reflects the authoritative lease.
Precondition = Callable[[dict], Optional[str]]


def _make_call_precondition(params: dict) -> Optional[str]:
    """make_call places an OUTBOUND call. Block it while a call is already live (text-Miles
    is paused mid-call; placing another would race the voice bridge and the single-writer
    lease). Reads `runtime.is_call_active` at call time — imported here so server.py can
    inject it after startup; None until wired keeps current behavior."""
    from agent import runtime

    if runtime.is_call_active and runtime.is_call_active():
        return "[blocked — precondition] A call is already active; make_call only runs when no call is in progress."
    return None


# Only state-machine gates the tool cannot self-check belong here. See the module
# docstring for why destructive / external-comms tools (gmail, web_cli) are absent.
PRECONDITIONS: dict[str, Precondition] = {
    "make_call": _make_call_precondition,
}


# Single source for "which tools are side-effecting", derived from the receipt tagging above so
# the dry-consensus pre-pass and the governance gate can never drift from what mints a receipt.
# Governance of ACTION tools now runs through the HESO policy engine (agent/heso_receipts.gate),
# called by core._exec_tool / tools.call_tool before any side effect — that replaced the old
# utility-classifier autonomy gate. The persona money/legal/external clause and the in-handler
# self-gates (gmail anti-spam/sensitive-data, web_cli LinkedIn) remain.
ACTION_TOOLS: frozenset[str] = frozenset(n for n, k in _TOOL_KINDS.items() if k == TOOL_KIND.ACTION)


def validate_policy() -> None:
    """Soft, startup-time check that every key here names a real tool. Logs drift,
    never raises — a stale policy entry must not take the agent down. Imports the
    registry lazily (policy is imported by core, which imports the tools package, so
    a top-level import here would create a cycle) and degrades quietly if it can't."""
    try:
        from agent.tools import TOOL_HANDLERS
    except ImportError as e:
        log.warning("policy_validation_skipped", reason="tool_registry_unavailable", err=str(e))
        return

    registry = set(TOOL_HANDLERS)
    unknown_kinds = sorted(set(_TOOL_KINDS) - registry)
    unknown_preconds = sorted(set(PRECONDITIONS) - registry)
    if unknown_kinds:
        log.warning("policy_unknown_tool_kinds", names=unknown_kinds)
    if unknown_preconds:
        log.warning("policy_unknown_preconditions", names=unknown_preconds)
