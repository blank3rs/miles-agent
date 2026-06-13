import asyncio
import hashlib
import json
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable

import structlog

from .config import AGENT_STATE_FILE, LOGS_DIR, MODEL
from .llm import UTILITY_MODEL, llm_create
from .persona import build_system_prompt
from .tools import TOOL_DEFINITIONS, TOOL_HANDLERS

log = structlog.get_logger()

# Kimi K2.6 has 262,144 token context window.
# Rough heuristic: 1 token ≈ 4 chars.
CTX_WINDOW_TOKENS = 262_144
COMPACT_AT_TOKENS = int(CTX_WINDOW_TOKENS * 0.50)   # ~131k — trigger
COMPACT_TO_TOKENS = int(CTX_WINDOW_TOKENS * 0.20)   # ~52k — keep recent verbatim
CHARS_PER_TOKEN   = 4

# Not a work limit — Miles runs as long as he wants and stops when he's done (or
# chooses to take a break and set a heartbeat). This is only a runaway backstop so a
# stuck loop can't burn the budget overnight. In-turn compaction keeps context healthy
# however long the turn runs.
MAX_TOOL_ROUNDS = 1000
NUDGE_AT_ROUND  = 990  # only fires if approaching the runaway backstop — never in normal work

WRAPUP_NUDGE = (
    "[system] You've been going a very long time on this single turn — that's unusual. "
    "Make sure you're not stuck in a loop. Land it now: update the ledger, set_heartbeat() "
    "if work remains, and reply with a brief status. Don't start anything new."
)

COMPACT_PROMPT = """You are compacting an autonomous CMO agent's conversation history so it can keep operating with full working context. This summary REPLACES the raw history, so anything you drop is gone forever — losing a specific (a name, a number, a commitment) is far worse than being verbose.

The history below is ordered oldest → newest. Recent events matter more.

Fill in this exact structure, keeping every concrete specific. Omit a section only if it's genuinely empty:

PEOPLE & ORGS: every person/company involved — name, role/company, current state of the relationship, what was last discussed.
COMMITMENTS & PROMISES: anything the agent committed to (or others committed to it), with who and by when.
ACTIONS TAKEN: emails sent, calls made, things scheduled, purchases, profile/account changes — with outcomes.
DECISIONS: what was decided and the reasoning; alternatives rejected.
OPEN THREADS / FOLLOW-UPS: what's still pending, blocked, or waiting on someone — be specific about next step.
SCHEDULED: active heartbeats, calls, deadlines, dates.
LESSONS / GOTCHAS: anything that broke and what fixed it; anything surprising.
ACTIVE NOW: the thread the agent was working on most recently — extra detail here.

Drop only: reasoning narration, redundant restatement, and pleasantries. Never drop a concrete fact to be brief. Plain prose under each label is fine; no markdown headers.

---
{history}
---

Compacted context:"""


def _estimate_tokens(messages: list[dict]) -> int:
    total_chars = sum(
        len(str(m.get("content") or "")) + len(str(m.get("role") or ""))
        for m in messages
    )
    return total_chars // CHARS_PER_TOKEN


class Agent:
    def __init__(self):
        self.model = MODEL
        self.history: deque = deque(maxlen=500)
        self.summary: str = ""
        self.broadcast: Callable | None = None
        self.is_running: bool = False
        # Set by inbox_watcher when Akshay emails mid-run; cleared after injection
        self._akshay_interrupt: dict | None = None
        self._load_state()

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

    # ── Durable state ──────────────────────────────────────────────────────────
    # Working memory (history + summary) survives restarts. The graph and soul
    # are reflective memory; this is the operational thread Miles was on.

    def _load_state(self) -> None:
        try:
            if AGENT_STATE_FILE.exists():
                data = json.loads(AGENT_STATE_FILE.read_text())
                self.history.extend(data.get("history", []))
                self.summary = data.get("summary", "")
                log.info("agent_state_loaded", messages=len(self.history))
        except Exception as e:
            log.warning("agent_state_load_failed", err=str(e))

    def _save_state(self) -> None:
        try:
            AGENT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = AGENT_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"history": list(self.history), "summary": self.summary}))
            tmp.replace(AGENT_STATE_FILE)  # atomic — a crash mid-write can't corrupt state
        except Exception as e:
            log.warning("agent_state_save_failed", err=str(e))

    # ── Public entry point ─────────────────────────────────────────────────────

    async def run(self, user_message: str, trigger: str = "user") -> str:
        self.is_running = True
        # A heads-up that arrived between turns is stale — the email it points to is
        # separately queued and will be (or is being) handled as its own turn. Drop it
        # so it can't be injected into an unrelated future turn.
        self._akshay_interrupt = None
        try:
            await self._emit("start", {"trigger": trigger, "message": user_message})
            self.history.append({"role": "user", "content": user_message})
            # Persist the trigger immediately so a deploy/kill mid-turn can't silently
            # lose the email or request we're about to work on.
            self._save_state()

            await self._maybe_compact()

            system_prompt = build_system_prompt()
            if self.summary:
                system_prompt += f"\n\n## Compressed history\n{self.summary}"

            messages = [{"role": "system", "content": system_prompt}] + list(self.history)

            result = await self._tool_loop(messages)

            self.history.append({"role": "assistant", "content": result})
            return result
        finally:
            self.is_running = False
            self._save_state()

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
        fields. Between-turn summarization is handled separately by _maybe_compact."""
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
        # over and over with no progress, a circuit breaker stops the turn. Matters more
        # now that 429s retry instead of dying — a stuck loop could otherwise run forever.
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

                # Tools in one round are independent — run them concurrently.
                # This is what makes "fire several subagents at once" actually parallel.
                tool_msgs = await asyncio.gather(
                    *[self._exec_tool(tc) for tc in msg.tool_calls]
                )
                messages.extend(tool_msgs)

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

    # ── Context compaction ────────────────────────────────────────────────────

    async def _maybe_compact(self):
        history_list = list(self.history)
        estimated = _estimate_tokens(history_list)

        if estimated <= COMPACT_AT_TOKENS:
            return

        await self._emit("status", {
            "status": "compacting",
            "message": f"Context at ~{estimated:,} tokens ({estimated*100//CTX_WINDOW_TOKENS}% of window) — compacting…",
        })

        # Keep the most recent messages worth ~COMPACT_TO_TOKENS; summarize the rest
        target_chars = COMPACT_TO_TOKENS * CHARS_PER_TOKEN
        chars_so_far = 0
        keep_from    = len(history_list)

        for i in range(len(history_list) - 1, -1, -1):
            msg_chars = len(str(history_list[i].get("content") or ""))
            if chars_so_far + msg_chars > target_chars:
                keep_from = i + 1
                break
            chars_so_far += msg_chars

        old_messages  = history_list[:keep_from]
        keep_messages = history_list[keep_from:]

        if not old_messages:
            return

        # Build history text — mark the bottom third as recent for the summarizer
        recent_cutoff = max(0, len(old_messages) - len(old_messages) // 3)
        lines = []
        for i, m in enumerate(old_messages):
            if not m.get("content"):
                continue
            prefix = "[RECENT] " if i >= recent_cutoff else ""
            lines.append(f"{prefix}{m['role'].upper()}: {str(m.get('content',''))[:600]}")
        history_text = "\n".join(lines)
        if self.summary:
            history_text = f"[Prior summary]\n{self.summary}\n\n[New messages]\n{history_text}"

        prompt = COMPACT_PROMPT.format(history=history_text)

        try:
            # Cheap utility model (gpt-4o-mini) — summarization doesn't need Kimi, and
            # routing it off-Kimi frees scarce quota for the actual work.
            resp = await llm_create(
                model=UTILITY_MODEL,
                messages=[
                    {"role": "system", "content": "You are a precise summarizer for an AI agent's context window."},
                    {"role": "user",   "content": prompt},
                ],
                max_tokens=4096,
            )
            new_summary = resp.choices[0].message.content or ""
        except Exception as e:
            await self._emit("status", {"status": "compact_failed", "message": f"Compaction failed: {e}"})
            # Fall back to hard truncation — better than crashing
            self.history = deque(keep_messages, maxlen=500)
            return

        self.summary = new_summary
        self.history = deque(keep_messages, maxlen=500)

        compressed_tokens = _estimate_tokens(list(self.history))
        await self._emit("status", {
            "status":  "compact_done",
            "message": f"Compacted to ~{compressed_tokens:,} tokens. Summary stored.",
        })

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
