"""The single autonomy safety check, owned in one place so it can't drift.

Both the hot-loop dispatcher (core._exec_tool) and the skills/subagent bypass path
(tools.call_tool) delegate here, so a side-effecting ACTION is gated identically no matter
how it's reached. The check is a cheap utility-model yes/no rubric (policy.SAFETY_RUBRIC) that
runs ONLY for ACTION tools the policy flags (policy.safety_needs_check) — local-state actions
short-circuit with no LLM call. It FAILS OPEN: any classifier error/timeout allows the action,
so a utility outage never bricks the loop. This complements, never replaces, the persona
money/legal/external clause and the in-handler self-gates.
"""
from __future__ import annotations

import structlog

from agent import policy
from agent.llm import UTILITY_MODEL, llm_create

log = structlog.get_logger()

# Returned to the model as the tool result when an action is held. Points back at the standing
# rule (email Akshay for money/legal/external) so the brain can re-route rather than just retry.
_BLOCK_REASON = (
    "[blocked — safety] This action was held by the autonomy safety check. If it involves real "
    "money, legal risk, or an external commitment, email Akshay first per your standing rule; "
    "otherwise restate it more specifically or confirm intent before trying again."
)


async def is_safe_action(name: str, params: dict) -> tuple[bool, str]:
    """(True, '') to allow, (False, reason) to hold. Only ever runs the classifier for ACTION
    tools that policy.safety_needs_check flags; everything else is allowed for free. Fails OPEN
    on any exception so a utility-model outage degrades to current behavior (the persona clause
    and in-handler gates remain the live protection)."""
    if not policy.safety_needs_check(name):
        return True, ""
    try:
        resp = await llm_create(
            model=UTILITY_MODEL,
            messages=[
                {"role": "system", "content": policy.SAFETY_RUBRIC},
                {"role": "user", "content": policy.summarize_params(name, params)},
            ],
            max_tokens=4,
            temperature=0,
        )
        ans = (resp.choices[0].message.content or "").strip().lower()
        if ans.startswith("safe"):
            return True, ""
        return False, _BLOCK_REASON
    except Exception as e:
        log.warning("safety_gate_failed_open", tool=name, err=str(e))
        return True, ""
