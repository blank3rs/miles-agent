"""One chokepoint for actions that touch the outside world: enforce policy + rate limits,
and write a tamper-evident receipt to the store. This is HESO's own thesis turned on Miles
himself — every consequential action is logged and chained, so the trail can be audited.

Receipts live in the one store (miles.db). Each receipt carries a digest that chains it to
the one before it (BLAKE3-style hash chain, the same idea HESO ships), so a receipt can't be
silently altered or removed without breaking the chain. Full HESO Action Receipt minting (the
@hesohq SDK) can slot in later where `mint_external` is called — the local chain stands on its
own until then.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from agent import store

log = structlog.get_logger()


def _params_digest(params: dict[str, Any] | None) -> str:
    if not params:
        return ""
    try:
        return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]
    except Exception:
        return ""


def record(
    action: str,
    *,
    target: str = "",
    decision: str = "allowed",
    reason: str = "",
    params: dict[str, Any] | None = None,
) -> str:
    """Append a hash-chained receipt for an action. Returns the receipt digest. The chain
    itself (read-prev + insert) is computed atomically in store.add_receipt, so concurrent
    callers can't fork it."""
    digest = ""
    try:
        digest = store.add_receipt(action, target=target, params_digest=_params_digest(params),
                                   decision=decision, reason=reason)
    except Exception as e:
        log.warning("receipt_write_failed", action=action, err=str(e))
    log.info("action_receipt", action=action, target=target, decision=decision, reason=reason)
    return digest


def within_rate_limit(action: str, target: str, max_per_day: int) -> bool:
    """True if (action,target) is under its 24h ALLOWED-count cap. A real time window via a
    bounded COUNT query, so the cap holds even on a high-volume day (no row-count truncation)."""
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    return store.count_allowed_receipts(action, target, since) < max_per_day


# ── Web-CLI policy ───────────────────────────────────────────────────────────────
# Driving LinkedIn write/social actions from a server is the single highest ban-risk move
# in 2026 (HeyReach was permabanned for cloud automation — and a datacenter IP is the tell,
# regardless of headless vs headful). Running headful lowers detection a lot, but it doesn't
# change the IP, so reads are autonomous and writes/social actions on gated sites still route
# to Akshay to run on a real device / residential IP.
# Known aliases/hostnames for the gated site. NOTE: this is a substring guardrail — a user
# who crystallizes a LinkedIn adapter under an unrelated one-word name (e.g. "li") could slip
# a write past it. It's a safety net for honest mistakes, not a hard boundary; the real
# protection is that LinkedIn writes should run on a residential device, not this server.
_GATED_SITES = ("linkedin", "lnkd.in", "lnkd")
_WRITE_VERBS = (
    "invite", "connect", "message", "send", "post", "comment", "like", "follow",
    "endorse", "react", "share", "dm", "apply", "submit", "withdraw", "accept", "reply",
)
# Word-boundary match so "send-message", "Connect", "post:" etc. are all caught, not just
# bare tokens. (A guardrail, not a hard security boundary — see module docstring.)
_WRITE_VERB_RE = re.compile(r"\b(" + "|".join(_WRITE_VERBS) + r")\b", re.IGNORECASE)


def gate_web_action(command: str) -> str | None:
    """Return a block reason if this opencli command is a gated write, else None."""
    c = command or ""
    if not any(site in c.lower() for site in _GATED_SITES):
        return None
    if _WRITE_VERB_RE.search(c):
        return (
            "[blocked — policy] Write/social actions on LinkedIn don't run from the server "
            "(automating them from a datacenter IP is the top account-ban trigger). Reads are "
            "fine; for an invite/message/post, hand it to Akshay to do on his own device, or "
            "have him approve it explicitly. This protects the account."
        )
    return None
