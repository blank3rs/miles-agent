"""Real HESO Action Receipts (the @hesohq SDK the audit.py docstring reserved a slot for).

HESO is the GOVERNANCE LAYER for Miles's side-effecting actions (the @hesohq SDK the audit.py
docstring reserved a slot for). The dispatcher (core._exec_tool / tools.call_tool) calls gate()
BEFORE running any ACTION tool: HESO's policy engine (Rust, in-process) authorizes it, signs an
offline-verifiable Ed25519 ActionReceipt, and pushes it to the HESO cloud ledger — so HESO's own
thesis runs on Miles himself.

ENFORCING mode: gate() returns allow / block / suspend. Floored verbs (payment / delete /
account_change / data_export) come back SUSPENDED — policy requires a human co-sign — and the
dispatcher refuses them, routing Miles to the email-Akshay escape valve. Everything mapped to
tool_call is allowed and runs. This REPLACES the old utility-classifier autonomy gate; the
persona security clause and the in-handler self-gates (gmail anti-spam, web_cli LinkedIn) remain.

FAIL OPEN by construction: if HESO_API_KEY is unset, heso.toml/identity are missing, the key
passphrase isn't provided, or heso.process errors, gate() returns "skip" and the action proceeds
— a HESO outage can never brick the loop. The verdict is a fast local Rust call; the ledger push
is offloaded to the background pool so the gate adds no network latency.

To actually RESUME a suspended action (vs route to email), enroll an approver in the HESO console
so a human can co-sign it to L1 — that browser/device step is out of band of this process.

Enable in prod: scaffold the project on the data volume once (`heso init /data`), set
HESO_API_KEY (heso_live_...), HESO_KEY_PASSPHRASE (the identity key passphrase), and optionally
HESO_ENDPOINT (default https://api.heso.ca). Without HESO_API_KEY this module is a no-op (skip).
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import structlog

from agent.config import DATA_DIR

log = structlog.get_logger()

_enabled = False
_pool: ThreadPoolExecutor | None = None
_WORKFLOW = os.getenv("HESO_WORKFLOW", "miles")
_ACCOUNT = os.getenv("HESO_ACCOUNT", "miles")


def _verb_for(action: str):
    """Map a Miles tool name to a HESO verb so heso.toml policy rules match. Defaults to
    tool_call (the broad 'agent did a thing' lane)."""
    import heso

    a = (action or "").lower()
    if a in ("store_secret", "delete_secret", "reset_browser_profile"):
        return heso.Verb.ACCOUNT_CHANGE
    if "delete" in a or "remove" in a:
        return heso.Verb.DELETE
    if "pay" in a or "invoice" in a or "purchase" in a or "charge" in a:
        return heso.Verb.PAYMENT
    return heso.Verb.TOOL_CALL


def init_heso() -> bool:
    """Resolve the heso project config + cloud creds. Returns True iff HESO receipting is live.
    Never raises — a failure just leaves Miles on the local audit chain alone."""
    global _enabled, _pool
    key = os.getenv("HESO_API_KEY")
    if not key:
        log.info("heso_disabled", reason="HESO_API_KEY unset")
        return False
    try:
        import heso
        import heso.cloud

        project_root = os.getenv("HESO_PROJECT_ROOT", str(DATA_DIR))
        heso.init(project_root=project_root, workflow=_WORKFLOW, account=_ACCOUNT)
        endpoint = os.getenv("HESO_ENDPOINT", "https://api.heso.ca")
        heso.cloud.configure(api_key=key, endpoint=endpoint)
        # One worker: serialize mints so concurrent in-turn actions can't race the signed chain.
        _pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="heso-mint")
        _enabled = True
        log.info("heso_enabled", endpoint=endpoint, project_root=project_root, workflow=_WORKFLOW)
        return True
    except Exception as e:
        log.warning("heso_init_failed", err=str(e))
        _enabled = False
        return False


def enabled() -> bool:
    return _enabled


def gate(name: str, params: dict) -> tuple[str, str]:
    """ENFORCING governance gate — run once per side-effecting ACTION BEFORE it executes. Returns
    (decision, reason):
      "allow"   → run the action (HESO policy allowed it; its signed receipt pushes in the bg)
      "block"   → HESO policy blocked it; `reason` is the message handed back to the model
      "suspend" → HESO requires a human co-sign (floored verbs: payment/delete/account_change/
                  data_export); refuse pending approval, `reason` routes to the email-Akshay valve
      "skip"    → HESO unconfigured or errored → caller allows (FAIL OPEN; never bricks the loop —
                  the persona clause + in-handler self-gates stay the live protection)
    The verdict (heso.process) is a fast local Rust call; the ledger push is offloaded to the
    background pool so the gate adds no network latency to the action. Never raises."""
    if not _enabled or _pool is None:
        return "skip", ""
    try:
        import heso

        act = heso.Action(
            verb=_verb_for(name),
            tool_name=name,
            workflow=_WORKFLOW,
            account=_ACCOUNT,
            fields={"target": str(params.get("to") or params.get("target") or "")},
        )
        outcome = heso.process(act)
        rcpt = outcome.receipt if isinstance(outcome.receipt, dict) else {}
        if rcpt.get("content", {}).get("action_hash"):
            _pool.submit(_push_async, rcpt, name)  # ledger push off the gate's hot path
        kind = outcome.kind
        if kind == heso.OutcomeKind.BLOCKED:
            log.info("heso_gate", action=name, decision="block")
            return "block", "[blocked — HESO policy] " + (
                getattr(outcome, "reason", "") or "This action is blocked by policy."
            )
        if kind == heso.OutcomeKind.SUSPENDED:
            log.info("heso_gate", action=name, decision="suspend")
            return "suspend", (
                "[needs approval — HESO] This is a governed action (payment / delete / "
                "account-change / data-export) that policy requires a human to co-sign before it "
                "runs. It's logged for approval. For anything involving real money, legal risk, or "
                "an external commitment, email Akshay per your standing rule — or ask him to "
                "approve or do it himself."
            )
        log.info("heso_gate", action=name, decision="allow")
        return "allow", ""
    except Exception as e:
        log.warning("heso_gate_failed_open", action=name, err=str(e))
        return "skip", ""


def _push_async(rcpt: dict, name: str) -> None:
    """Push one finalized receipt to the ledger off the gate path. Best-effort; never raises."""
    try:
        import heso.cloud

        heso.cloud.push_receipt(rcpt)
        log.info("heso_receipt", action=name, pushed=True)
    except Exception as e:
        log.info("heso_push_deferred", action=name, err=str(e))
