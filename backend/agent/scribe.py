"""The scribe — the harness remembers, so the agent doesn't have to.

After every turn, Miles's job is done; the scribe quietly turns what just happened into
memory: a few salient episodes (the raw material the idle dream consolidates) and an
up-to-date working-state checkpoint (so a restart resumes cleanly). It runs in the
background on the cheap utility model, never blocks the reply, and never breaks a turn if
it fails.

This is the "agent doesn't worry about semantics" piece: journaling and focus-keeping are
automatic. Miles CAN still journal_entry() / set_focus() to emphasize something, but the
baseline works whether or not he ever thinks about it.
"""
from __future__ import annotations

import json
import re

import structlog

from agent import store
from agent.llm import UTILITY_MODEL, llm_create

log = structlog.get_logger()

_SYSTEM = """You are the memory scribe for Miles, an autonomous CMO/growth agent. You read ONE finished turn — what triggered it, which tools Miles used and what they returned, and his final reply — and capture only what's worth remembering for the future.

Return STRICT JSON, nothing else:
{
  "episodes": [{"kind": "<one of: decision, person, fact, discovery, action, observation, concern, learning>", "content": "<one specific sentence; keep names, numbers, outcomes>"}],
  "goal": "<one line: what Miles is driving at right now, or \\"\\" if unchanged/unclear>",
  "next_action": "<the single next concrete step, or \\"\\" if none or blocked>"
}

Rules:
- episodes: 0 to 5. Record durable things — a decision made and why, a person and what's now known about them, a fact learned, something that broke and exactly what fixed it, a real outcome (email sent and to whom, call result, signup completed). A routine read with no outcome → no episodes. Never invent; only what the turn shows.
- Be concrete and first-person ("I emailed Sarah at Acme the pricing"). No fluff, no restating the prompt.
- goal/next_action describe Miles's ongoing work after this turn, so a future restart picks up cleanly."""

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)
_VALID_KINDS = {"decision", "person", "fact", "discovery", "action", "observation", "concern", "learning"}


def _render_turn(msgs: list[dict]) -> str:
    lines: list[str] = []
    for m in msgs:
        role = m.get("role")
        if role == "user":
            lines.append(f"TRIGGER/INPUT: {str(m.get('content', ''))[:1800]}")
        elif role == "assistant":
            if m.get("tool_calls"):
                names = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
                lines.append(f"MILES used: {names}")
            if m.get("content"):
                lines.append(f"MILES: {str(m['content'])[:1800]}")
        elif role == "tool":
            lines.append(f"   -> {str(m.get('content', ''))[:400]}")
    return "\n".join(lines)


def _parse(raw: str) -> dict:
    raw = _FENCE.sub("", (raw or "").strip())
    try:
        return json.loads(raw)
    except Exception:
        # last-ditch: pull the outermost {...}
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


async def record_turn(trigger: str, turn_msgs: list[dict]) -> None:
    """Fire-and-forget: distill a finished turn into episodes + a refreshed checkpoint."""
    try:
        # Skip turns with no real substance (a bare greeting, an empty heartbeat).
        used_tools = any(m.get("tool_calls") for m in turn_msgs)
        transcript = _render_turn(turn_msgs)
        if not used_tools and len(transcript) < 240:
            return

        resp = await llm_create(
            model=UTILITY_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Trigger: {trigger}\n\nThe turn:\n{transcript}\n\nReturn the JSON."},
            ],
            max_tokens=900,
            temperature=0.2,
        )
        data = _parse(resp.choices[0].message.content or "")

        episodes = data.get("episodes") or []
        recorded = 0
        for ep in episodes[:5]:
            if not isinstance(ep, dict):
                continue
            content = str(ep.get("content") or "").strip()
            if not content:
                continue
            kind = str(ep.get("kind") or "observation").strip().lower()
            if kind not in _VALID_KINDS:
                kind = "observation"
            store.add_episode(kind, content)
            recorded += 1

        goal = str(data.get("goal") or "").strip()
        nxt = str(data.get("next_action") or "").strip()
        if goal or nxt:
            store.set_working_state(current_goal=goal or None, next_action=nxt or None)

        log.info("scribe_recorded", trigger=trigger, episodes=recorded,
                 goal=bool(goal), next_action=bool(nxt))
    except Exception as e:
        log.warning("scribe_failed", trigger=trigger, err=str(e))
