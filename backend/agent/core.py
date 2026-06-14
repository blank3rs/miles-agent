import asyncio
import hashlib
import json
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable

import structlog

from agent import scribe, store
from agent.config import LOGS_DIR, MODEL
from agent.llm import llm_create
from agent.persona import build_system_prompt
from agent.tools import TOOL_DEFINITIONS, TOOL_HANDLERS

log = structlog.get_logger()

# Not a work limit — Miles runs as long as he wants and stops when he's done. This is only
# a runaway backstop so a stuck loop can't burn the budget overnight. In-turn compaction
# (_compact_tool_results) keeps context healthy however long a single turn runs; across
# turns, store.compile_context() rebuilds a bounded window from the source of truth.
MAX_TOOL_ROUNDS = 1000
NUDGE_AT_ROUND  = 990  # only fires if approaching the runaway backstop — never in normal work

WRAPUP_NUDGE = (
    "[system] You've been going a very long time on this single turn — that's unusual. "
    "Make sure you're not stuck in a loop. Land it now: update the ledger, set_heartbeat() "
    "if work remains, and reply with a brief status. Don't start anything new."
)


class Agent:
    """The waking agent. State is NOT held here — it lives in the store (miles.db). Each
    turn compiles a fresh context from the store and writes new messages straight back, so
    a restart mid-thought resumes cleanly: there is no separate 'load state' path, a cold
    boot compiles exactly like any other turn."""

    def __init__(self):
        self.model = MODEL
        self.broadcast: Callable | None = None
        self.is_running: bool = False
        # Set by inbox_watcher when Akshay emails mid-run; cleared after injection
        self._akshay_interrupt: dict | None = None
        # After a restart, warn Miles to re-check reality before acting on an in-flight task
        # (did the last step's side effect already land?). We inject it on the first of the
        # next few turns that actually has an in-flight task — not just the literal first turn,
        # which might be a trivial heartbeat that would waste the one-shot.
        self._boot_turns_left: int = 3
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

    # ── Public entry point ─────────────────────────────────────────────────────

    async def run(self, user_message: str, trigger: str = "user") -> str:
        self.is_running = True
        # A heads-up that arrived between turns is stale — the email it points to is
        # separately queued and handled as its own turn.
        self._akshay_interrupt = None
        try:
            await self._emit("start", {"trigger": trigger, "message": user_message})

            # Persist the trigger immediately — a deploy/kill mid-turn can't lose the
            # request we're about to work on, because it's already in the log. The id marks
            # where this turn begins, so the scribe can read exactly it afterwards.
            turn_start_id = store.append_message({"role": "user", "content": user_message}, trigger=trigger)

            ctx = store.compile_context()

            system_prompt = build_system_prompt()
            if ctx["system_context"]:
                system_prompt += "\n\n" + ctx["system_context"]

            # Within the first few turns after a restart, the first time Miles actually has an
            # in-flight task, tell him to verify before acting. Then stop (warned once); and if
            # the boot window passes with nothing in flight, stop too (no longer "just restarted").
            if self._boot_turns_left > 0:
                self._boot_turns_left -= 1
                active = ctx["resume"].get("active_task")
                if active and active.get("status") in ("in_progress", "blocked"):
                    system_prompt += self._resume_note(active)
                    self._boot_turns_left = 0

            messages = [{"role": "system", "content": system_prompt}] + ctx["history"]

            result = await self._tool_loop(messages)

            store.append_message({"role": "assistant", "content": result})

            # Hand the finished turn to the scribe — it records what happened (episodes +
            # an updated checkpoint) so Miles never has to journal or set focus by hand.
            # Background + best-effort: it must never slow the reply or break the turn.
            try:
                turn_msgs = store.messages_since(turn_start_id)
                asyncio.create_task(scribe.record_turn(trigger, turn_msgs))
            except Exception as e:
                log.warning("scribe_dispatch_failed", err=str(e))

            return result
        finally:
            self.is_running = False

    # ── Tool loop ──────────────────────────────────────────────────────────────

    @staticmethod
    def _compact_tool_results(
        messages: list[dict], max_chars: int = 600_000, keep_recent: int = 6
    ) -> list[dict]:
        """In-turn Tier-1 compaction (Claude-Code style): when a single turn's history
        balloons past max_chars, *clear* old bulk rather than truncate it. This is what
        lets a turn run for hundreds of rounds without overflowing the context window.

        Two kinds of bulk get shed from older messages (the most recent `keep_recent`
        stay fully intact so the model keeps its working set):
          - old tool results → replaced with a short marker
          - old assistant reasoning_content (Kimi's thinking, which accumulates fast) →
            dropped; it only needs to round-trip for the recent turns, not ancient ones
        Truncating mid-string corrupts JSON/HTML and still costs tokens, so we clear whole
        fields. This is the IN-turn safety net; across turns, store.compile_context bounds
        the window from the source of truth."""
        def _size(m):
            return len(str(m.get("content") or "")) + len(str(m.get("reasoning_content") or ""))
        total = sum(_size(m) for m in messages)
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
                if len(content) > 600:
                    marker = f"[old tool result cleared — was {len(content):,} chars]"
                    out[i] = {**m, "content": marker}
                    total -= len(content) - len(marker)
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

    async def _exec_tool(self, tool_call) -> dict:
        name = tool_call.function.name
        try:
            params = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            params = {}

        emit_params = (
            {**params, "value": "[redacted]"}
            if name in self._SECRET_PARAM_TOOLS and "value" in params
            else params
        )
        await self._emit("tool_call", {"tool": name, "params": emit_params})

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
        return {"role": "tool", "tool_call_id": tool_call.id, "content": result_str}

    @staticmethod
    def _fingerprint(tool_calls) -> str:
        parts = sorted((tc.function.name, tc.function.arguments or "") for tc in tool_calls)
        return hashlib.sha1(json.dumps(parts).encode()).hexdigest()[:16]

    async def _tool_loop(self, messages: list[dict]) -> str:
        # Out-of-model loop detection: if the model fires the exact same tool call(s)
        # over and over with no progress, a circuit breaker stops the turn.
        recent_fps: deque = deque(maxlen=6)
        loop_warned = False
        for round_num in range(MAX_TOOL_ROUNDS):
            # Inject a pending Akshay heads-up at the TOP of the round so it's never
            # stranded when the model lands on a final text reply (the common case).
            interrupt = self._akshay_interrupt
            if interrupt:
                self._akshay_interrupt = None
                messages.append({"role": "user", "content": self._interrupt_note(interrupt)})
                await self._emit("status", {"status": "interrupted", "message": "Akshay emailed — heads-up injected"})

            await self._emit("thinking", {})
            send_messages = self._compact_tool_results(messages)

            try:
                response = await llm_create(
                    model=self.model,
                    messages=send_messages,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="auto",
                    max_tokens=32768,
                )
            except Exception as e:
                # 429/5xx were already retried with backoff inside llm_create. A 400 here
                # is usually context overflow — trim hard and try once more.
                if "400" in str(e):
                    await self._emit("status", {"status": "retrying", "message": "400 — trimming context and retrying"})
                    try:
                        send_messages = self._compact_tool_results(messages, max_chars=200_000)
                        response = await llm_create(
                            model=self.model,
                            messages=send_messages,
                            tools=TOOL_DEFINITIONS,
                            tool_choice="auto",
                            max_tokens=32768,
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
                messages.extend(tool_msgs)
                name_by_id = {tc.id: tc.function.name for tc in msg.tool_calls}
                for tm in tool_msgs:
                    store.append_message(self._redact_tool_for_store(tm, name_by_id))

                # Near the round cap: tell the agent to land the turn instead of
                # silently dropping it at the limit.
                if round_num == NUDGE_AT_ROUND:
                    messages.append({"role": "user", "content": WRAPUP_NUDGE})
                    await self._emit("status", {"status": "wrapup_nudge", "message": f"Round {round_num + 1}/{MAX_TOOL_ROUNDS} — nudging wrap-up"})

            else:
                content = msg.content or ""
                await self._emit("response", {"content": content, "trigger": messages[1].get("content", "") if len(messages) > 1 else ""})
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
