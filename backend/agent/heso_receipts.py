"""Real HESO Action Receipts (the @hesohq SDK the audit.py docstring reserved a slot for).

Miles already mints a local hash-chained receipt for every consequential action
(agent/audit.py). This layer ALSO signs an offline-verifiable, Ed25519-signed HESO
ActionReceipt via the `heso` SDK (Rust engine, in-process) and best-effort pushes it to the
HESO cloud ledger — so HESO's own thesis runs on Miles himself.

RECORD-AND-SIGN mode: HESO observes, redacts PII, and signs every allowed action, but it does
NOT gate/block Miles here. The enforcing layers stay the local audit chain, the persona
security clause, and the in-loop safety gate. Enforcing mode — where HESO can suspend an action
for a human co-sign per the policy in <project_root>/heso.toml (delete / large payment already
require_approval there) — is a deliberate later flip, not on by default, because it can park a
real action waiting on a human.

Fully best-effort and isolated. If HESO_API_KEY is unset, heso.toml/identity are missing, the
key passphrase isn't provided, or any call fails, Miles runs exactly as before. Minting runs
off the hot loop on a single background thread, so a turn is never blocked or broken by HESO.

Enable in prod: scaffold the project on the data volume once (`heso init /data`), set
HESO_API_KEY (heso_live_...), HESO_KEY_PASSPHRASE (the identity key passphrase), and optionally
HESO_ENDPOINT (default https://cloud.heso.io). Without HESO_API_KEY this module is a no-op.
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


def mint(
    action: str,
    *,
    target: str = "",
    decision: str = "allowed",
    reason: str = "",
    params_digest: str = "",
) -> None:
    """Best-effort, non-blocking: sign a HESO ActionReceipt for one action and push it to the
    ledger. Returns immediately (work runs on the background thread); never raises."""
    if not _enabled or _pool is None:
        return
    try:
        _pool.submit(_mint_blocking, action, target, decision, reason, params_digest)
    except Exception as e:
        log.warning("heso_mint_dispatch_failed", action=action, err=str(e))


def _mint_blocking(action: str, target: str, decision: str, reason: str, params_digest: str) -> None:
    try:
        import heso
        import heso.cloud

        act = heso.Action(
            verb=_verb_for(action),
            tool_name=action,
            workflow=_WORKFLOW,
            account=_ACCOUNT,
            fields={
                "target": target or "",
                "decision": decision,
                "reason": reason or "",
                "params_digest": params_digest or "",
            },
        )
        outcome = heso.process(act)
        pushed = False
        try:
            heso.cloud.push_receipt(outcome.receipt or {})
            pushed = True
        except Exception as e:
            # Ledger unreachable — the receipt is still signed + chained locally; the outbox
            # retries on the next push. Not an error for Miles.
            log.info("heso_push_deferred", action=action, err=str(e))
        log.info("heso_receipt", action=action, kind=str(getattr(outcome, "kind", "")), pushed=pushed)
    except Exception as e:
        log.warning("heso_mint_failed", action=action, err=str(e))
