import asyncio
import hashlib
import json
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable

import structlog

from agent import audit, policy, safety, scribe, store
from agent.config import AKSHAY_EMAIL, LOGS_DIR, ORCHESTRATOR_MODEL, WORKER_MODEL
from agent.llm import UTILITY_MODEL, BudgetExceeded, llm_create
from agent.persona import build_system_prompt
from agent.tools import TOOL_DEFINITIONS, TOOL_HANDLERS

log = structlog.get_logger()

# Not a work limit — Miles runs as long as he wants and stops when he's done. This is only
# a runaway backstop so a stuck loop can't burn the budget overnight. In-turn compaction
# (_compact_tool_results) keeps context healthy however long a single turn runs; across
# turns, store.compile_context() rebuilds a bounded window from the source of truth.
MAX_TOOL_ROUNDS = 1000
NUDGE_AT_ROUND  = 990  # only fires if approaching the runaway backstop — never in normal work

# Orchestrator (Opus on Foundry) is a 200k-token window and Foundry counts the output
# reservation against it — so size the output budget to what's left, and keep in-turn growth
# under a char ceiling that leaves room for the tool catalog + system prompt + output.
CHARS_PER_TOKEN     = 4
ORCH_CTX_TOKENS     = int(os.getenv("ORCH_CTX_TOKENS", "200000"))
_OUTPUT_RESERVE_MAX = 32768
_TOOLS_SYSTEM_MARGIN_TOKENS = 6000   # tools=TOOL_DEFINITIONS + headroom, not counted in messages

WRAPUP_NUDGE = (
    "[system] You've been going a very long time on this single turn — that's unusual. "
    "Make sure you're not stuck in a loop. Land it now: update the ledger, set_heartbeat() "
    "if work remains, and reply with a brief status. Don't start anything new."
)

# ── Per-turn tool promotion (D2) ─────────────────────────────────────────────
# A small safe core is ALWAYS promoted so the brain is never stranded: it can always
# inspect/advance its work, listen, schedule itself, and reach Akshay. Everything else
# is promoted by keyword/intent over the user message + open-work signals.
_CORE_TOOLS = frozenset({
    "list_tasks", "update_task", "set_heartbeat",
    "search_memories", "send_email", "read_emails",
})

# External/irreversible ACTION tools whose re-fire on a confidence-escalation re-run would be a
# real duplicate side effect (a second email, a second calendar event). The escalation dedup guard
# in _exec_tool consults store.action_already_fired for exactly these — and ONLY when escalated, so
# a normal turn that legitimately repeats an action is untouched (F4).
_ESCALATION_DEDUP_TOOLS = frozenset({
    "send_email", "create_calendar_event", "respond_to_calendar_event", "make_call",
})

# intent group → (trigger keywords, tool names). Keywords are matched as substrings against
# the lowercased user message + working-state signals. A group fires wholesale (cheap at 52
# tools; no embeddings). Groups overlap on purpose — a turn can pull several. New tools land
# in a group here so promotion stays in lockstep with the registry (validated at startup).
_TOOL_GROUPS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "email": (
        ("email", "inbox", "reply", "respond", "message", "mail", "send", "draft", "forward"),
        ("send_email", "read_emails"),
    ),
    "calendar": (
        ("calendar", "meeting", "schedule", "invite", "event", "availability", "free slot",
         "book", "rsvp", "appointment", "call time", "reschedule"),
        ("list_calendar_events", "create_calendar_event", "respond_to_calendar_event",
         "find_free_slots"),
    ),
    "research": (
        ("search", "research", "look up", "find out", "google", "web", "scrape", "url",
         "website", "article", "news", "pdf", "read about", "browse"),
        ("search_web", "exa_search", "scrape_url", "read_pdf", "web_cli"),
    ),
    "browser": (
        ("browser", "log in", "login", "sign in", "click", "fill", "form", "captcha",
         "screenshot", "page", "portal", "dashboard", "navigate", "automate the site"),
        ("browser_task", "reset_browser_profile", "take_screenshot", "analyze_screenshot",
         "solve_captcha", "read_sms"),
    ),
    "files": (
        ("file", "directory", "folder", "read the", "write", "edit", "document", "report",
         "save to", "sandbox", "heso file"),
        ("read_heso_file", "read_file", "write_sandbox_file", "edit_file",
         "list_heso_directory", "list_sandbox_directory"),
    ),
    "code": (
        ("code", "script", "run python", "shell", "command", "install", "package",
         "compute", "execute", "terminal"),
        ("run_python", "run_shell", "install_package", "exec_sandboxed"),
    ),
    "secrets": (
        ("secret", "credential", "password", "api key", "token", "login detail"),
        ("store_secret", "get_secret", "list_secret_keys", "delete_secret"),
    ),
    "skills": (
        ("skill", "capability", "teach yourself", "build a tool", "github skill"),
        ("create_skill", "run_skill", "list_skills", "download_github_skill"),
    ),
    "memory": (
        ("remember", "recall", "memory", "journal", "note to self", "what happened",
         "history", "episode", "log it", "reflect", "dream", "fact", "know about", "who is"),
        ("journal_entry", "dream", "retrieve_episodes", "search_facts"),
    ),
    "tasks": (
        ("task", "todo", "to-do", "ledger", "focus", "track", "follow up", "follow-up",
         "remind me", "next step", "plan"),
        ("add_task", "set_focus", "check_tasks"),
    ),
    "contacts": (
        ("contact", "phone number", "find someone", "lead", "prospect", "signalhire",
         "person at", "reach out to"),
        ("signalhire_credits", "signalhire_find_contact"),
    ),
    "voice": (
        ("call", "phone", "dial", "ring", "voice", "speak to", "talk to them"),
        ("make_call",),
    ),
    "heartbeats": (
        ("heartbeat", "wake me", "check back", "later", "schedule myself", "recurring",
         "cron", "in an hour", "tomorrow", "ping me"),
        ("cancel_heartbeat", "list_heartbeats"),
    ),
    "vision": (
        ("image", "screenshot", "video", "look at this", "analyze the picture", "photo",
         "see the", "visual"),
        ("take_screenshot", "analyze_image", "analyze_screenshot", "analyze_video"),
    ),
    "subagent": (
        ("subagent", "delegate", "parallel", "fan out", "background task", "spawn",
         "run in parallel", "kick off"),
        ("run_subagent",),
    ),
}

# Cost control: the premium orchestrator (Opus) runs only the turns that warrant it; the rest
# run on the cheap worker. A tiny utility-model triage decides the ambiguous (external) cases.
_IMPORTANCE_PROMPT = (
    "You triage incoming work for an autonomous CMO agent and decide if it needs the TOP-TIER "
    "model. Answer 'important' for: a real decision or judgment call; a serious reply from a "
    "customer, partner, investor, or press; anything about money, pricing, contracts, legal, or a "
    "deal moving; or anything high-stakes. Answer 'routine' for: a bounce or auto-reply, a "
    "newsletter or notification, cold-outreach legwork, scheduling, research, or admin. "
    "Reply with exactly one word: important or routine."
)

# ── Reliability gates (2 + 3) — both default OFF behind env flags ─────────────────
# Confidence escalation: when the cheap worker lands a final text answer to an external,
# important-ish trigger, a utility judge scores it and, if low-confidence, re-runs the loop
# ONCE on the orchestrator. Dry planning-consensus: for high-stakes ACTION turns, sample only
# the first model response K times WITHOUT executing any tool, vote the plan, and on
# disagreement inject a re-ground note before the loop executes exactly once.
ENABLE_CONFIDENCE_ESCALATION = os.getenv("ENABLE_CONFIDENCE_ESCALATION", "0") == "1"
ENABLE_DRY_CONSENSUS         = os.getenv("ENABLE_DRY_CONSENSUS", "0") == "1"
_DRY_CONSENSUS_K             = int(os.getenv("DRY_CONSENSUS_K", "3"))

_CONFIDENCE_PROMPT = (
    "You judge whether an autonomous CMO agent's reply to the request below is confidently "
    "complete and correct, or whether it looks uncertain, evasive, half-finished, or likely to be "
    "wrong. Answer 'confident' if the reply clearly and fully addresses the request. Answer "
    "'unsure' if it hedges, says it couldn't do something, leaves the task obviously incomplete, or "
    "reads like a guess. Reply with exactly one word: confident or unsure."
)

# The re-ground note injected when the dry-consensus samples disagree on the first move — nudges
# the model to re-check live state before committing to a single concrete action (never executes).
_DRY_REGROUND_NOTE = (
    "[system] Before acting, you considered several different first moves and they disagreed — "
    "re-ground in the actual current state (read the inbox / re-check the page, file, or ledger) "
    "and then choose the single safest concrete action."
)


class Agent:
    """The waking agent. State is NOT held here — it lives in the store (miles.db). Each
    turn compiles a fresh context from the store and writes new messages straight back, so
    a restart mid-thought resumes cleanly: there is no separate 'load state' path, a cold
    boot compiles exactly like any other turn."""

    def __init__(self):
        self.model = ORCHESTRATOR_MODEL   # the main loop runs on the orchestrator tier (Opus)
        self.broadcast: Callable | None = None
        self.is_running: bool = False
        # Set by inbox_watcher when Akshay emails mid-run; cleared after injection
        self._akshay_interrupt: dict | None = None
        # After a restart, warn Miles to re-check reality before acting on an in-flight task
        # (did the last step's side effect already land?). We inject it on the first of the
        # next few turns that actually has an in-flight task — not just the literal first turn,
        # which might be a trivial heartbeat that would waste the one-shot.
        self._boot_turns_left: int = 3
        # The tool schemas promoted (sent as the volatile `tools=` field) for the current turn.
        # None means "no promotion in effect" (the rejection gate is inert), so any path that
        # reaches _tool_loop without run() having promoted still sees the full registry.
        self._promoted_names: set[str] | None = None
        # Reliability gates (set per turn in run()): _escalate_eligible marks the risky case
        # 'we ran cheap on something external' so the final-text seam can judge+escalate once;
        # _escalated is the hard single-escalation guard; _turn_important marks a high-stakes turn
        # so dry planning-consensus only fires where it's worth the extra samples.
        self._escalate_eligible: bool = False
        self._escalated: bool = False
        self._turn_important: bool = False
        # Turn-start ISO timestamp (set per turn in run()): the since-cutoff the escalation dedup
        # guard uses so a re-run can't replay an external action already fired this turn (F4).
        self._turn_started_iso: str = ""
        store.init_db()

    def interrupt_for_akshay(self, email: dict) -> None:
        """Called from inbox_watcher when Akshay emails while the agent is busy.

        This is only a heads-up injected mid-turn so Miles wraps up; the email is
        ALWAYS also enqueued as its own turn, so it can never be dropped, and the
        injected note never triggers a duplicate reply."""
        self._akshay_interrupt = email

    @staticmethod
    def _interrupt_note(email: dict) -> str:
        return (
            f"\n\n[heads-up — Akshay just emailed]\n"
            f"Subject: {email.get('subject', '')}\n\n"
            f"It's already queued and you'll handle it as its own turn right after this one. "
            f"Wrap up what you're doing cleanly so you can get to it — don't reply to it here."
        )

    @staticmethod
    def _resume_note(active_task: dict) -> str:
        return (
            "\n\n## You just restarted — re-check reality before acting\n"
            f"Your active task was: {active_task['title']} (status {active_task['status']}). "
            f"Last step recorded: {active_task.get('last_step') or '(none)'}.\n"
            "Background work (browser_task, run_subagent) does NOT survive a restart. Before you "
            "do anything that has a side effect, confirm the current state — read the inbox, check "
            "the relevant page or file, search_memories() — so you continue from where things "
            "actually are, not where you assumed. Don't redo a step that already completed, and "
            "don't re-send something that already went out."
        )

    # ── Model routing (cost control) ────────────────────────────────────────────

    async def _is_important(self, message: str) -> bool:
        """Cheap utility-model triage: does this external item warrant the premium model?
        Fails safe to routine (worker) so a classifier hiccup never burns Opus."""
        try:
            resp = await llm_create(
                model=UTILITY_MODEL,
                messages=[
                    {"role": "system", "content": _IMPORTANCE_PROMPT},
                    {"role": "user", "content": (message or "")[:2000]},
                ],
                max_tokens=4,
                temperature=0,
            )
            ans = (resp.choices[0].message.content or "").strip().lower()
            return ans.startswith("important") or ans.startswith("yes")
        except Exception:
            return False

    async def _is_safe_action(self, name: str, params: dict) -> tuple[bool, str]:
        """Autonomy safety gate for a side-effecting ACTION tool. Delegates to the single
        implementation in agent.safety so the hot loop and the call_tool bypass path can't drift.
        (True, '') allows; (False, reason) holds. Fails OPEN — a utility outage never blocks."""
        return await safety.is_safe_action(name, params)

    async def _score_answer(self, user_message: str, answer: str) -> bool:
        """Utility judge at the final-answer seam: True when the worker's reply is confidently
        complete/correct, False when it reads low-confidence. Fails to True (don't escalate) on any
        exception, so a judge hiccup never burns the premium tier."""
        try:
            resp = await llm_create(
                model=UTILITY_MODEL,
                messages=[
                    {"role": "system", "content": _CONFIDENCE_PROMPT},
                    {"role": "user", "content": (
                        f"Request:\n{(user_message or '')[:1500]}\n\nReply:\n{(answer or '')[:2500]}"
                    )},
                ],
                max_tokens=4,
                temperature=0,
            )
            ans = (resp.choices[0].message.content or "").strip().lower()
            return not ans.startswith("unsure")
        except Exception:
            return True

    async def _pick_tier(self, trigger: str, user_message: str) -> tuple[str, bool]:
        """Route the turn to a model by importance. Opus only for the founder and genuinely
        high-stakes work; everything else (heartbeats, routine outreach, bounces, research,
        dispatch results) runs on the cheap worker. Returns (tier, important) — `important` is the
        judge's verdict on an external trigger, so the reliability gates know a turn was high-stakes
        even when it was routed to the cheap worker."""
        t = (trigger or "").lower()
        important = False
        if t == "user" or (AKSHAY_EMAIL and AKSHAY_EMAIL.lower() in t):
            tier = ORCHESTRATOR_MODEL                      # the founder is always top-tier
            important = True
        elif t.startswith(("email:", "call:")):
            important = await self._is_important(user_message)
            tier = ORCHESTRATOR_MODEL if important else WORKER_MODEL
        else:
            tier = WORKER_MODEL                            # heartbeats, dispatch results, etc.
        log.info("turn_tier", tier=tier, trigger=trigger, important=important)
        await self._emit("status", {"status": "tier", "message": tier})
        return tier, important

    # ── Per-turn tool promotion (D2) ────────────────────────────────────────────

    @staticmethod
    def _promote_tools(user_message: str, ctx: dict) -> tuple[list[dict], set[str]]:
        """Choose the relevant subset of TOOL_DEFINITIONS to inline as full schemas this turn.

        Keyword/intent gating over the user message + working-state/open-task signals (a
        plain rule map at 52 tools — no embeddings). The always-resident one-line tool index
        (D1) lives in the stable system prefix, so the brain still knows every tool exists; a
        call to an un-promoted tool is bounced by the rejection gate so it can retry, never
        failing the turn. The safe core is ALWAYS promoted so the brain is never stranded.

        Returns (promoted_defs, promoted_names). promoted_defs is a FILTERED COPY of the
        registry — never a mutation — so the parity asserts keep passing."""
        resume = ctx.get("resume") or {}
        signal = " ".join(
            str(s) for s in (
                user_message or "",
                resume.get("current_goal") or "",
                resume.get("next_action") or "",
                (resume.get("active_task") or {}).get("title") or "",
            )
        ).lower()

        names: set[str] = set(_CORE_TOOLS)
        for keywords, tools in _TOOL_GROUPS.values():
            if any(kw in signal for kw in keywords):
                names.update(tools)

        # Only promote names that are really in the registry (a typo'd group entry must not
        # advertise a tool the dispatcher can't run). Keeps promotion in lockstep with parity.
        names &= set(TOOL_HANDLERS)
        promoted_defs = [d for d in TOOL_DEFINITIONS if d["function"]["name"] in names]
        return promoted_defs, names

    # ── Public entry point ─────────────────────────────────────────────────────

    async def run(self, user_message: str, trigger: str = "user") -> str:
        self.is_running = True
        # A heads-up that arrived between turns is stale — the email it points to is
        # separately queued and handled as its own turn.
        self._akshay_interrupt = None
        # Turn-start ISO timestamp: the since-cutoff for the escalation dedup guard (F4), so a
        # re-run on the orchestrator can detect 'this exact external action already fired THIS turn'
        # via store.action_already_fired and not replay the side effect.
        self._turn_started_iso = datetime.now(timezone.utc).isoformat()
        try:
            await self._emit("start", {"trigger": trigger, "message": user_message})

            # Pick the model for THIS turn: premium (Opus) for the founder + high-stakes work,
            # cheap worker for everything routine. Keeps the always-on burn off the premium tier.
            self.model, self._turn_important = await self._pick_tier(trigger, user_message)
            # Reliability flags for this turn. Escalation is only eligible on the risky case: an
            # external trigger (email:/call:) that we routed to the CHEAP worker — a wrong cheap
            # answer there is exactly what we want to catch. _escalated guards single-shot.
            self._escalated = False
            self._escalate_eligible = (
                (trigger or "").lower().startswith(("email:", "call:"))
                and self.model == WORKER_MODEL
            )

            # Persist the trigger immediately — a deploy/kill mid-turn can't lose the
            # request we're about to work on, because it's already in the log. The id marks
            # where this turn begins, so the scribe can read exactly it afterwards.
            turn_start_id = store.append_message({"role": "user", "content": user_message}, trigger=trigger)

            ctx = store.compile_context()

            # Split the system message into a day-stable STABLE block (persona + D1 tool index)
            # and a VOLATILE block (everything recompiled this turn). Everything per-turn —
            # system_context AND the post-restart resume note — goes in `volatile`; the stable
            # prefix stays byte-identical within a day so the orchestrator's cache prefix hits.
            persona_prefix = build_system_prompt()
            volatile = ctx["system_context"] or ""

            # Within the first few turns after a restart, the first time Miles actually has an
            # in-flight task, tell him to verify before acting. Then stop (warned once); and if
            # the boot window passes with nothing in flight, stop too (no longer "just restarted").
            if self._boot_turns_left > 0:
                self._boot_turns_left -= 1
                active = ctx["resume"].get("active_task")
                if active and active.get("status") in ("in_progress", "blocked"):
                    volatile += self._resume_note(active)
                    self._boot_turns_left = 0

            messages = [
                {"role": "system", "content": self._build_system_blocks(persona_prefix, volatile)}
            ] + ctx["history"]

            # Choose the tool set for this turn by tier.
            #  • Orchestrator (Opus, cache-eligible): send the FULL catalog in deterministic
            #    registry order so the cached prefix (tools render BEFORE system in Anthropic
            #    order) is byte-stable cross-turn — the prompt cache actually hits. _promoted_names
            #    is the whole registry so the D2 rejection gate bounces nothing on Opus. The token
            #    margin already reserves for the full catalog.
            #  • Worker (DeepSeek, openai/ — no cache to bust): keep D2 keyword promotion to trim
            #    per-turn input tokens, plus promote-on-demand recovery in _exec_tool (F6).
            if self.model == ORCHESTRATOR_MODEL:
                promoted_defs = list(TOOL_DEFINITIONS)
                self._promoted_names = set(TOOL_HANDLERS)
            else:
                promoted_defs, self._promoted_names = self._promote_tools(user_message, ctx)

            sect = self._section_tokens(
                persona_prefix, ctx.get("section_chars", {}), ctx["history"], promoted_defs
            )
            log.info(
                "turn_compiled",
                tier=self.model,
                trigger=trigger,
                total_tokens=sum(sect.values()),
                promoted_tools=len(self._promoted_names),
                tool_catalog_full_tokens=len(json.dumps(TOOL_DEFINITIONS)) // CHARS_PER_TOKEN,
                **sect,
            )

            try:
                result = await self._tool_loop(messages, promoted_defs, user_message)
            except BudgetExceeded as e:
                # The monthly LLM-spend backstop tripped mid-turn. Land cleanly with a status the
                # log can carry, rather than letting the cap surface as an unhandled crash.
                result = f"[agent] Paused: {e}"
                await self._emit("status", {"status": "budget_paused", "message": str(e)})

            store.append_message({"role": "assistant", "content": result})

            # Hand the finished turn to the scribe — it records what happened (episodes +
            # an updated checkpoint) so Miles never has to journal or set focus by hand.
            # Background + best-effort: it must never slow the reply or break the turn.
            try:
                turn_msgs = store.messages_since(turn_start_id)
                asyncio.create_task(scribe.record_turn(trigger, turn_msgs))
                # Also fold the older turns the verbatim window just dropped into the rolling
                # summary (B2) — off the critical path, best-effort like record_turn.
                excluded = store.turns_outside_budget()
                if excluded:
                    asyncio.create_task(scribe.update_history_summary(excluded))
            except Exception as e:
                log.warning("scribe_dispatch_failed", err=str(e))

            return result
        finally:
            self.is_running = False
            # Clear promotion so the rejection gate is inert outside a turn — any path that
            # reaches _tool_loop without run() promoting first then sees the full registry.
            self._promoted_names = None

    # ── Tool loop ──────────────────────────────────────────────────────────────

    @staticmethod
    def _msg_size(m: dict) -> int:
        """Char weight of a message — content + reasoning + tool_call ARGUMENTS (a long
        write_report / email arg can be the bulk of a turn, so it must count)."""
        s = len(str(m.get("content") or "")) + len(str(m.get("reasoning_content") or ""))
        for tc in (m.get("tool_calls") or []):
            s += len(str(tc.get("function", {}).get("arguments") or ""))
        return s

    @staticmethod
    def _build_system_blocks(persona_prefix: str, volatile: str) -> list[dict]:
        """Render the system message as ordered OpenAI-shaped text blocks: the day-stable
        persona prefix (persona + the D1 one-line tool index) FIRST, then the per-turn volatile
        block (compiled system_context + any resume note) — never the other way round. No
        cache_control here; llm.py adds it to block[0] only for the orchestrator tier. Anthropic
        renders `tools` BEFORE `system`, so the cached prefix is (tools + this block[0]). It is
        reused across the many tool ROUNDS of a turn always, and across TURNS on the orchestrator
        tier — where the tool set is now the fixed full catalog (run()'s Opus branch), so the
        prefix is byte-stable day to day. (The worker tier sends a per-turn promoted subset and
        is openai/, so it isn't cache-eligible anyway.) The volatile block is dropped entirely when
        empty (a cold boot still gets a valid one-block system message), and per-turn data must
        never leak into block[0] or the cache silently never hits."""
        blocks = [{"type": "text", "text": persona_prefix}]
        if volatile:
            blocks.append({"type": "text", "text": volatile})
        return blocks

    @classmethod
    def _section_tokens(
        cls, persona: str, system_context_chars: dict, history: list[dict], promoted_defs: list[dict]
    ) -> dict:
        """Per-section token estimate for one compiled turn (instrumentation only). Sums the
        bare persona (which carries the always-resident one-line tool index), the memory-context
        sections store.compile_context already built, the PROMOTED tool schemas actually sent in
        tools= this turn (D2 — not the full catalog), and the history message sizes. Never any
        tool params/results. tool_def_tokens is what the turn really pays; the full-catalog delta
        is logged separately at the call site so the D2 saving is visible."""
        sc = system_context_chars or {}
        return {
            "persona_tokens": len(persona) // CHARS_PER_TOKEN,
            "identity_tokens": sc.get("identity", 0) // CHARS_PER_TOKEN,
            "working_narrative_tokens": sc.get("working_narrative", 0) // CHARS_PER_TOKEN,
            "working_state_tokens": sc.get("working_state", 0) // CHARS_PER_TOKEN,
            "history_summary_tokens": sc.get("history_summary", 0) // CHARS_PER_TOKEN,
            "ledger_tokens": sc.get("ledger", 0) // CHARS_PER_TOKEN,
            "dream_tokens": sc.get("dream_blocks", 0) // CHARS_PER_TOKEN,
            "facts_tokens": sc.get("facts", 0) // CHARS_PER_TOKEN,
            "tool_def_tokens": len(json.dumps(promoted_defs)) // CHARS_PER_TOKEN,
            "history_tokens": sum(cls._msg_size(m) for m in history) // CHARS_PER_TOKEN,
            "pre_reasoned_tokens": sc.get("pre_reasoned", 0) // CHARS_PER_TOKEN,
            "receipt_tokens": sc.get("receipts", 0) // CHARS_PER_TOKEN,
        }

    @classmethod
    def _output_budget(cls, messages: list[dict]) -> int:
        """How many output tokens we can safely ask for without prompt+output blowing the
        orchestrator's 200k window (Foundry counts the reservation against it)."""
        prompt_tokens = sum(cls._msg_size(m) for m in messages) // CHARS_PER_TOKEN + _TOOLS_SYSTEM_MARGIN_TOKENS
        return max(2048, min(_OUTPUT_RESERVE_MAX, ORCH_CTX_TOKENS - prompt_tokens))

    @classmethod
    def _compact_tool_results(
        cls,
        messages: list[dict],
        max_chars: int = 360_000,
        keep_recent: int = 6,
        receipts_by_tcid: dict | None = None,
    ) -> list[dict]:
        """In-turn Tier-1 compaction (Claude-Code style): when a single turn's history
        balloons past max_chars, *clear* old bulk rather than truncate it. This is what
        lets a turn run for hundreds of rounds without overflowing the context window.

        Two kinds of bulk get shed from older messages (the most recent `keep_recent`
        stay fully intact so the model keeps its working set):
          - old tool results → replaced with a short marker (or, for a receipted action, with
            its durable one-line receipt — see C3 graduated eviction)
          - old assistant reasoning_content (Kimi's thinking, which accumulates fast) →
            dropped; it only needs to round-trip for the recent turns, not ancient ones
        Truncating mid-string corrupts JSON/HTML and still costs tokens, so we clear whole
        fields. This is the IN-turn safety net; across turns, store.compile_context bounds
        the window from the source of truth.

        `receipts_by_tcid` maps a tool_call_id → its allowed receipt dict (gathered in the
        loop), kept OUT of the message dicts so no private key ever rides to the model."""
        receipts_by_tcid = receipts_by_tcid or {}
        total = sum(cls._msg_size(m) for m in messages)
        if total <= max_chars:
            return messages
        out = [dict(m) for m in messages]
        cutoff = max(1, len(out) - keep_recent)
        for i in range(1, cutoff):
            if total <= max_chars:
                break
            m = out[i]
            rc = m.get("reasoning_content")
            # Kimi 400s if a tool_calls assistant message lacks its reasoning_content,
            # so only drop reasoning_content from messages that have NO tool_calls.
            if rc and not m.get("tool_calls"):
                m = {k: v for k, v in m.items() if k != "reasoning_content"}
                out[i] = m
                total -= len(str(rc))
            if total > max_chars and m.get("role") == "tool":
                content = str(m.get("content") or "")
                # Graduated eviction (C3): a receipted action collapses to its durable one-line
                # receipt — more useful than the generic 'cleared' marker and the same string the
                # across-turn window will show — BEFORE the generic >600-char clearing.
                receipt = receipts_by_tcid.get(m.get("tool_call_id"))
                if receipt:
                    line = store.receipt_line(receipt)
                    if len(content) > len(line):
                        out[i] = {**m, "content": line}
                        total -= len(content) - len(line)
                elif len(content) > 600:
                    marker = f"[old tool result cleared — was {len(content):,} chars]"
                    out[i] = {**m, "content": marker}
                    total -= len(content) - len(marker)
            # A turn's bulk can live in a large tool-call argument (write_report, a long email
            # body) — shed those from old calls too; the durable log keeps the real call.
            if total > max_chars and m.get("tool_calls"):
                new_tcs, changed = [], False
                for tc in m["tool_calls"]:
                    args = str(tc.get("function", {}).get("arguments") or "")
                    if len(args) > 600:
                        marker = '{"_cleared": "large argument dropped to fit context"}'
                        new_tcs.append({**tc, "function": {**tc["function"], "arguments": marker}})
                        total -= len(args) - len(marker)
                        changed = True
                    else:
                        new_tcs.append(tc)
                if changed:
                    out[i] = {**out[i], "tool_calls": new_tcs}
        return out

    # Secret values must never reach the event log or the websocket feed —
    # the model still gets the real result, only the emitted copy is redacted.
    _SECRET_RESULT_TOOLS = frozenset({"get_secret"})
    _SECRET_PARAM_TOOLS  = frozenset({"store_secret"})

    @classmethod
    def _redact_assistant_for_store(cls, m: dict) -> dict:
        """Strip secret-bearing tool-call arguments (e.g. store_secret's value) before the
        message reaches the durable log. The live in-turn messages keep the real values, so
        this turn still works; the DB never holds the secret."""
        tcs = m.get("tool_calls")
        if not tcs or not any(tc["function"]["name"] in cls._SECRET_PARAM_TOOLS for tc in tcs):
            return m
        red = [
            {**tc, "function": {**tc["function"], "arguments": '{"redacted": true}'}}
            if tc["function"]["name"] in cls._SECRET_PARAM_TOOLS else tc
            for tc in tcs
        ]
        return {**m, "tool_calls": red}

    @classmethod
    def _redact_tool_for_store(cls, tm: dict, name_by_id: dict) -> dict:
        """Redact secret tool RESULTS (e.g. get_secret) before the durable log — so secrets
        are never persisted in miles.db and never reach the scribe's external utility model.
        The model still got the real value live, in-turn."""
        if name_by_id.get(tm.get("tool_call_id")) in cls._SECRET_RESULT_TOOLS:
            return {**tm, "content": "[redacted — secret value, not stored]"}
        return tm

    def _promote_on_demand(self, name: str) -> list[str]:
        """Grow the live promotion set to include a real-but-un-promoted tool the model just
        called (F6 lazy-load recovery, worker tier). Adds the tool itself plus every _TOOL_GROUPS
        group it belongs to (so a related follow-up call lands promoted too), intersected with the
        real registry so parity holds. Returns the names newly added (for the retry note). Caller
        rebuilds the per-round `tools` from self._promoted_names so the schema is actually sent
        next round."""
        if self._promoted_names is None:
            return []
        grow: set[str] = {name}
        for _kw, group_tools in _TOOL_GROUPS.values():
            if name in group_tools:
                grow.update(group_tools)
        grow &= set(TOOL_HANDLERS)
        added = sorted(grow - self._promoted_names)
        self._promoted_names.update(grow)
        return added

    async def _exec_tool(self, tool_call) -> dict:
        name = tool_call.function.name

        # After-model rejection gate (D2) + promote-on-demand recovery (F6): the brain saw the
        # one-line index (D1) and called a real-but-un-promoted tool. Instead of a dead-end error,
        # GROW the promotion set to include it (and its group) and tell the model to retry — the
        # loop rebuilds `tools` from self._promoted_names each round, so the newly-loaded schema is
        # sent next round (D1's "schemas load on demand" made real). String result, never raises.
        # Only active when run() set a promotion; on the orchestrator tier _promoted_names is the
        # full registry, so this never fires there.
        if self._promoted_names is not None and name not in self._promoted_names and name in TOOL_HANDLERS:
            added = self._promote_on_demand(name)
            await self._emit("status", {"status": "tool_loaded_on_demand", "message": name})
            return {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(
                    {"now_available": added, "note": "tool loaded — retry the call"}
                ),
            }

        try:
            params = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            params = {}

        # Escalation dedup guard (F4): on a confidence-escalation re-run (self._escalated), an
        # external/irreversible ACTION whose EXACT params already fired this turn must NOT re-fire —
        # the orchestrator re-reasons over prior tool results, it doesn't replay side effects. Use
        # the SAME digest the receipt writer uses, with the turn start as the since-cutoff, so a hit
        # is the precise 'this already went out'. Gated strictly on _escalated — a normal turn that
        # legitimately repeats an action is untouched. Short-circuits before the handler runs.
        if self._escalated and name in _ESCALATION_DEDUP_TOOLS:
            target = str(params.get("to") or params.get("target") or params.get("event_id") or "")
            prior = store.action_already_fired(
                name, target, audit._params_digest(params), self._turn_started_iso
            )
            if prior:
                await self._emit("status", {"status": "action_already_fired", "message": name})
                rid = (prior.get("receipt_id") or "")[:12]
                return {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": (
                        f"[already done this turn] {name} to {target or '(no target)'} already "
                        f"completed (receipt {rid}) — not repeating it — finalize."
                    ),
                }

        # Deterministic precondition gate (E): unbypassable world-state floor read at dispatch
        # (e.g. make_call only when no call is live). Composes with D — D blocks "not promoted
        # this turn", E blocks "not allowed in current world-state". String result, never raise.
        pred = policy.PRECONDITIONS.get(name)
        if pred:
            block = pred(params)
            if block:
                await self._emit("status", {"status": "tool_blocked", "message": name})
                return {"role": "tool", "tool_call_id": tool_call.id, "content": block}

        # Autonomy safety gate (default-on, fail-open): for a side-effecting ACTION tool, a cheap
        # utility-model rubric decides whether it's clearly safe to run autonomously BEFORE any
        # side effect fires. Runs before capture_receipts + the handler, so a held action never
        # executes; the block is recorded as a blocked receipt (name is ACTION in _TOOL_KINDS, so
        # the chain stays correct). Complements the persona clause and the in-handler self-gates.
        if policy.tool_kind(name) == policy.TOOL_KIND.ACTION:
            ok, reason = await self._is_safe_action(name, params)
            if not ok:
                audit.record(
                    name,
                    target=str(params.get("to") or params.get("target") or ""),
                    decision="blocked",
                    reason="safety gate",
                    params=params,
                )
                await self._emit("status", {"status": "tool_safety_blocked", "message": name})
                return {"role": "tool", "tool_call_id": tool_call.id, "content": reason}

        emit_params = (
            {**params, "value": "[redacted]"}
            if name in self._SECRET_PARAM_TOOLS and "value" in params
            else params
        )
        await self._emit("tool_call", {"tool": name, "params": emit_params})

        # Capture the receipts THIS call mints. Each tool in a round runs as its own gathered
        # task with its own context copy, so the box is private to this call — exact attribution
        # with no high-water racing between concurrent tools (e.g. two send_email in one round).
        minted = audit.capture_receipts()
        handler = TOOL_HANDLERS.get(name)
        if handler:
            try:
                result = await handler(**params)
            except TypeError as e:
                # Model passed wrong/missing params — feed the error back instead of dying
                result = f"[bad arguments for {name}] {e}"
        else:
            result = f"[unknown tool: {name}] Available: {sorted(TOOL_HANDLERS)}"

        result_str = str(result)
        if name in self._SECRET_RESULT_TOOLS:
            shown = "[redacted — secret values are never logged]"
        else:
            shown = result_str[:2000] + ("…" if len(result_str) > 2000 else "")
        await self._emit("tool_result", {"tool": name, "result": shown})
        tm: dict = {"role": "tool", "tool_call_id": tool_call.id, "content": result_str}
        # Carry the allowed receipt this action produced (if any) as a private hint: it lets
        # in-turn compaction collapse this row to the one-line receipt, and lets the persist
        # step back-link the receipt to the row's message_id. Stripped before the model sees it.
        if policy.tool_kind(name) == policy.TOOL_KIND.ACTION:
            for r in minted:
                if r["decision"] == "allowed":
                    tm["_receipt"] = r
                    break
        return tm

    @staticmethod
    def _fingerprint(tool_calls) -> str:
        parts = sorted((tc.function.name, tc.function.arguments or "") for tc in tool_calls)
        return hashlib.sha1(json.dumps(parts).encode()).hexdigest()[:16]

    async def _dry_plan_consensus(
        self, messages: list[dict], tools: list[dict], k: int = _DRY_CONSENSUS_K
    ) -> str | None:
        """Side-effect-free self-consistency on the RISKY first move of a high-stakes ACTION turn.

        Sample only the FIRST model response k times (with tools= but NEVER dispatching any
        tool_call), fingerprint each proposed call-set via _fingerprint, and tally. If a strict
        majority agree, return None — the real loop will produce that same plan and execute it
        EXACTLY ONCE through the normal path. On disagreement return the re-ground note to inject.
        Never runs a tool, never mints a receipt; a sample error is skipped (no fail-closed).

        This is the safe analog of K=5 self-consistency for a side-effecting agent: we vote the
        decision, not the execution."""
        votes: list[str] = []
        send = self._compact_tool_results(messages)
        budget = self._output_budget(send)
        for _ in range(max(1, k)):
            try:
                resp = await llm_create(
                    model=self.model,
                    messages=send,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0.7,
                    max_tokens=budget,
                )
            except BudgetExceeded:
                raise
            except Exception as e:
                log.warning("dry_consensus_sample_failed", err=str(e))
                continue
            msg = resp.choices[0].message
            # WORKER_MODEL (DeepSeek) can return content=None with text in reasoning_content; this
            # runs on the orchestrator (Opus) in practice, but mirror _tool_loop's handling anyway.
            if getattr(msg, "tool_calls", None):
                votes.append(self._fingerprint(msg.tool_calls))
            else:
                votes.append("TEXT")
        if not votes:
            return None  # all samples failed — fall through to the normal loop (fail open)
        top = max(set(votes), key=votes.count)
        agree = votes.count(top)
        await self._emit("status", {
            "status": "dry_consensus",
            "message": f"{agree}/{len(votes)} agreed",
        })
        log.info("dry_plan_consensus", votes=len(votes), agree=agree, plan=top)
        if agree > len(votes) // 2:
            return None
        return _DRY_REGROUND_NOTE

    async def _tool_loop(
        self, messages: list[dict], tools: list[dict] | None = None, user_message: str = ""
    ) -> str:
        # `tools` is the per-turn promoted schema subset (D2); fall back to the full registry
        # so any caller that doesn't promote (or a None passed in) still works exactly as before.
        # `user_message` is the ACTUAL current-turn trigger content (threaded from run()), used by
        # the confidence judge and the 'response' emit — NOT messages[1], which is the oldest
        # anchor in the compiled window, not this turn's request (F8).
        tools = TOOL_DEFINITIONS if tools is None else tools
        # Out-of-model loop detection: if the model fires the exact same tool call(s)
        # over and over with no progress, a circuit breaker stops the turn.
        recent_fps: deque = deque(maxlen=6)
        loop_warned = False
        # tool_call_id → its allowed receipt, so in-turn compaction can collapse a receipted
        # action to its one-line receipt without ever stashing a private key on a model message.
        receipts_by_tcid: dict[str, dict] = {}

        # Dry planning-consensus (gated, default OFF): for a high-stakes ACTION turn on the
        # orchestrator, vote the first move K times WITHOUT executing anything; on disagreement
        # inject one re-ground note so the model re-checks live state before committing. The normal
        # loop below still executes the agreed plan EXACTLY ONCE — this pre-pass never runs a tool.
        if (
            ENABLE_DRY_CONSENSUS
            and not self._escalated
            and self.model == ORCHESTRATOR_MODEL
            and self._turn_important
            and any(d["function"]["name"] in policy.ACTION_TOOLS for d in tools)
        ):
            try:
                note = await self._dry_plan_consensus(messages, tools)
                if note:
                    messages.append({"role": "user", "content": note})
            except BudgetExceeded:
                raise
            except Exception as e:
                log.warning("dry_consensus_failed", err=str(e))

        for round_num in range(MAX_TOOL_ROUNDS):
            # Inject a pending Akshay heads-up at the TOP of the round so it's never
            # stranded when the model lands on a final text reply (the common case).
            interrupt = self._akshay_interrupt
            if interrupt:
                self._akshay_interrupt = None
                messages.append({"role": "user", "content": self._interrupt_note(interrupt)})
                await self._emit("status", {"status": "interrupted", "message": "Akshay emailed — heads-up injected"})

            # Rebuild `tools` from the live promotion set each round so a tool promoted on demand
            # last round (F6) is actually sent this round. No-op on the orchestrator tier, where
            # _promoted_names is already the full registry and `tools` is the full catalog.
            if self._promoted_names is not None:
                tools = [d for d in TOOL_DEFINITIONS if d["function"]["name"] in self._promoted_names]

            await self._emit("thinking", {})
            send_messages = self._compact_tool_results(messages, receipts_by_tcid=receipts_by_tcid)

            try:
                response = await llm_create(
                    model=self.model,
                    messages=send_messages,
                    tools=tools,
                    tool_choice="auto",
                    max_tokens=self._output_budget(send_messages),
                )
            except BudgetExceeded:
                # Hard monthly-cap backstop — not a transient. Let it propagate so a confidence
                # escalation can fall back to the worker answer; run() turns it into a clean status.
                raise
            except Exception as e:
                # 429/5xx + connection errors were already retried (and failed over) inside
                # llm_create. A 400 here is usually context overflow — trim HARD, shrink the
                # output reservation, and try once more so a long turn doesn't fail closed.
                if "400" in str(e) or "context" in str(e).lower():
                    await self._emit("status", {"status": "retrying", "message": "400 — trimming context and retrying"})
                    try:
                        send_messages = self._compact_tool_results(
                            messages, max_chars=140_000, receipts_by_tcid=receipts_by_tcid
                        )
                        response = await llm_create(
                            model=self.model,
                            messages=send_messages,
                            tools=tools,
                            tool_choice="auto",
                            max_tokens=8192,
                        )
                    except Exception as e2:
                        err = f"[LLM error after retry] {e2}"
                        await self._emit("error", {"message": err})
                        return err
                else:
                    err = f"[LLM error] {e}"
                    await self._emit("error", {"message": err})
                    return err

            choice = response.choices[0]
            msg    = choice.message

            # Output hit the token cap mid-generation: don't treat a truncated reply as
            # final, and don't run possibly-malformed tool calls — continue it instead.
            # (Transient — not persisted to the log.)
            if choice.finish_reason == "length":
                await self._emit("status", {"status": "truncated", "message": "Hit token cap mid-reply — continuing"})
                messages.append({"role": "assistant", "content": msg.content or ""})
                messages.append({"role": "user", "content": (
                    "[system] Your previous reply was cut off at the output token limit before you "
                    "finished. Continue and finish now, more concisely. If you were mid tool call, "
                    "make the call now."
                )})
                continue

            if choice.finish_reason == "tool_calls" and msg.tool_calls:
                # Loop guard: count how often this exact call-set recurred recently.
                fp = self._fingerprint(msg.tool_calls)
                recent_fps.append(fp)
                repeats = list(recent_fps).count(fp)
                if repeats >= 4:
                    await self._emit("status", {"status": "loop_broken", "message": "Same action repeated with no progress — stopping the turn"})
                    return ("[agent] Stopped: I repeated the same action several times without making progress. "
                            "Saved state; I'll come at it differently on the next heartbeat.")
                if repeats == 3 and not loop_warned:
                    loop_warned = True
                    messages.append({"role": "user", "content": (
                        "[system] You've made the same tool call(s) three times with no new progress. Stop repeating it — "
                        "take a materially different approach, or if you're genuinely blocked, note what's blocking you in "
                        "the task ledger, set a heartbeat, and end the turn with a brief status."
                    )})
                    await self._emit("status", {"status": "loop_detected", "message": "Repeated identical action — nudging a different approach"})

                # Append as a plain dict — the SDK's pydantic object has no .get(),
                # which breaks _compact_tool_results on the next round.
                assistant_msg: dict = {"role": "assistant", "content": msg.content or ""}
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ]
                # Kimi thinking mode: reasoning_content must round-trip in multi-turn tool calls
                rc = getattr(msg, "reasoning_content", None)
                if rc:
                    assistant_msg["reasoning_content"] = rc
                messages.append(assistant_msg)
                # Persist as it happens (survives a mid-turn crash), with any secret tool-call
                # argument stripped so it's never at rest in the log.
                store.append_message(self._redact_assistant_for_store(assistant_msg))

                # Tools in one round are independent — run them concurrently.
                # This is what makes "fire several subagents at once" actually parallel.
                tool_msgs = await asyncio.gather(
                    *[self._exec_tool(tc) for tc in msg.tool_calls]
                )
                name_by_id = {tc.id: tc.function.name for tc in msg.tool_calls}
                for tm in tool_msgs:
                    # _exec_tool tags an action's allowed receipt as a private hint. Pop it off
                    # the dict that lands in `messages` so no private key ever reaches the model;
                    # keep it in receipts_by_tcid for in-turn collapse and to back-link the row.
                    receipt = tm.pop("_receipt", None)
                    if receipt:
                        receipts_by_tcid[tm["tool_call_id"]] = receipt
                    messages.append(tm)
                    kind = policy.tool_kind(name_by_id[tm["tool_call_id"]])
                    mid = store.append_message(
                        self._redact_tool_for_store(tm, name_by_id), tool_kind=kind
                    )
                    if receipt and receipt.get("receipt_id"):
                        store.set_receipt_message_id(receipt["receipt_id"], mid)

                # Near the round cap: tell the agent to land the turn instead of
                # silently dropping it at the limit.
                if round_num == NUDGE_AT_ROUND:
                    messages.append({"role": "user", "content": WRAPUP_NUDGE})
                    await self._emit("status", {"status": "wrapup_nudge", "message": f"Round {round_num + 1}/{MAX_TOOL_ROUNDS} — nudging wrap-up"})

            else:
                content = msg.content or ""

                # Confidence-gated escalation (gated, default OFF): the cheap worker just landed a
                # FINAL text answer (no more tool calls) on a risky external turn. A utility judge
                # scores the answer against THIS turn's actual request (user_message, threaded from
                # run() — not the stale messages[1] anchor, F8); if low-confidence, re-run the loop
                # ONCE on the orchestrator over the SAME messages (which already hold every prior
                # tool result — Opus reasons over them, it does NOT replay side effects: the F4
                # dedup guard hard-blocks any external action that already fired this turn, and an
                # explicit '[system] already completed' note grounds it first). Single-shot + budget-safe.
                if (
                    ENABLE_CONFIDENCE_ESCALATION
                    and not self._escalated
                    and self._escalate_eligible
                    and self.model == WORKER_MODEL
                    and content.strip()
                ):
                    if not await self._score_answer(user_message, content):
                        # Ground the orchestrator on what already happened this turn so it doesn't
                        # even try to repeat a side effect (the F4 guard is the hard backstop).
                        done = store.allowed_receipts_since(self._turn_started_iso)
                        if done:
                            lines = "\n".join(f"- {r['action']} → {r['target']}" for r in done)
                            messages.append({"role": "user", "content": (
                                "[system] These actions already completed this turn and must NOT be "
                                f"repeated; only finalize / correct course if needed:\n{lines}"
                            )})
                        self._escalated = True
                        self.model = ORCHESTRATOR_MODEL
                        # On the orchestrator tier the rejection gate must bounce nothing, so the
                        # promotion set becomes the full registry (matches run()'s Opus branch).
                        self._promoted_names = set(TOOL_HANDLERS)
                        await self._emit("status", {
                            "status": "confidence_escalation",
                            "message": "low-confidence worker answer — re-running on orchestrator",
                        })
                        log.info("confidence_escalation", trigger="final_text")
                        try:
                            return await self._tool_loop(messages, tools, user_message)
                        except BudgetExceeded:
                            log.warning("confidence_escalation_budget_exceeded")
                            return content

                await self._emit("response", {"content": content, "trigger": user_message})
                return content

        return (
            "[agent] Hit maximum tool call depth. Work state was saved to the task "
            "ledger if I updated it; continuing on the next heartbeat."
        )

    # ── Internals ──────────────────────────────────────────────────────────────

    async def _emit(self, event_type: str, data: dict):
        event = {"type": event_type, "ts": time.time(), **data}
        _log_event(event)
        if self.broadcast:
            await self.broadcast(event)

    def get_recent_logs(self, n: int = 100) -> list[dict]:
        if not LOGS_DIR.exists():
            return []
        events: list[dict] = []
        for log_file in sorted(LOGS_DIR.glob("*.jsonl"), reverse=True)[:3]:
            try:
                lines = log_file.read_text().strip().splitlines()
                for line in reversed(lines):
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        pass
                    if len(events) >= n:
                        break
            except Exception:
                pass
        return list(reversed(events[-n:]))


def _log_event(event: dict):
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOGS_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass
