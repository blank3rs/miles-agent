"""Shared, resilient LLM access for every model caller.

The Azure Kimi-K2.6 endpoint rate-limits hard. Without coordination the main tool
loop, every research sub-agent, and the compaction summarizer each hammer it
independently and trip 429s — and a single 429 used to end a whole turn, throwing
away every tool round it had done. This module centralizes:

  - a process-wide concurrency cap (one Semaphore shared by all async callers), so
    we never fan out more simultaneous requests than the quota tolerates,
  - retry with exponential backoff that honors Retry-After, so a transient 429/5xx
    is ridden out in place instead of killing the turn,
  - provider routing, so utility work (summaries, etc.) goes to a cheap OpenAI model
    (gpt-4o-mini) instead of competing for Kimi's scarce quota, and
  - spend tracking with a generous monthly circuit-breaker, so a runaway can't quietly
    burn the budget.

browser-use drives its own client (one browser at a time via a lock), so the true
ceiling is _MAX_CONCURRENCY async callers + 1 browser.
"""
import asyncio
import json
import os
import random
from datetime import datetime, timezone

import structlog
from openai import OpenAI

from .config import AZURE_API_KEY, AZURE_ENDPOINT, DATA_DIR, MODEL

log = structlog.get_logger()

# Kimi (Azure) is the default reasoning brain. OpenAI handles cheap utility calls and
# only exists if a key is set. max_retries=0 because retries are handled here.
_azure_client = OpenAI(base_url=AZURE_ENDPOINT, api_key=AZURE_API_KEY, max_retries=0)
_openai_key = os.getenv("OPENAI_API_KEY", "")
_openai_client = OpenAI(api_key=_openai_key, max_retries=0) if _openai_key else None

# Cheap model for utility work (compaction summaries, classification). Falls back to
# the main model if no OpenAI key is configured.
UTILITY_MODEL = os.getenv("UTILITY_MODEL", "gpt-4o-mini") if _openai_client else MODEL

# Higher now that 429s are ridden out with backoff instead of killing a turn — so many
# parallel sub-agents genuinely run at once instead of single-filing through a tight gate.
# It's still a safety valve against a 429 storm; tune via LLM_MAX_CONCURRENCY.
_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "6"))
_MAX_ATTEMPTS    = int(os.getenv("LLM_MAX_ATTEMPTS", "6"))
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Blended $/token (input, output). Generous monthly cap is a runaway backstop, not a
# tight budget — set LLM_MONTHLY_USD_CAP lower to tighten.
_PRICING = {
    "gpt-4o-mini": (0.15e-6, 0.60e-6),
    "gpt-4o":      (2.50e-6, 10.0e-6),
    "Kimi":        (0.95e-6, 4.00e-6),  # prefix-matched
}
_MONTHLY_CAP_USD = float(os.getenv("LLM_MONTHLY_USD_CAP", "300"))
_SPEND_FILE = DATA_DIR / "llm_spend.json"

_semaphore: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    # Created lazily so it binds to the running server loop, not import time.
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    return _semaphore


def _client_for(model: str) -> OpenAI:
    if model.startswith(("gpt-", "o1", "o3", "o4")) and _openai_client:
        return _openai_client
    return _azure_client


def _price(model: str) -> tuple[float, float]:
    for prefix, p in _PRICING.items():
        if model.startswith(prefix):
            return p
    return _PRICING["Kimi"]


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _read_spend() -> dict:
    try:
        return json.loads(_SPEND_FILE.read_text())
    except Exception:
        return {}


def _record_spend(model: str, usage) -> None:
    if usage is None:
        return
    pin, pout = _price(model)
    cost = (getattr(usage, "prompt_tokens", 0) or 0) * pin + (getattr(usage, "completion_tokens", 0) or 0) * pout
    try:
        data = _read_spend()
        mk = _month_key()
        month = data.get(mk, {"usd": 0.0})
        before = month["usd"]
        month["usd"] = round(before + cost, 6)
        data[mk] = month
        _SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SPEND_FILE.write_text(json.dumps(data))
        # Warn once when crossing 80% of the cap.
        if before < _MONTHLY_CAP_USD * 0.8 <= month["usd"]:
            log.warning("llm_spend_80pct", month=mk, usd=round(month["usd"], 2), cap=_MONTHLY_CAP_USD)
    except Exception:
        pass


def _over_cap() -> bool:
    if _MONTHLY_CAP_USD <= 0:
        return False
    return _read_spend().get(_month_key(), {}).get("usd", 0.0) >= _MONTHLY_CAP_USD


def _status_of(exc) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def _retry_after_seconds(exc) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    ms = headers.get("retry-after-ms")
    if ms:
        try:
            return float(ms) / 1000.0
        except ValueError:
            pass
    secs = headers.get("retry-after")
    if secs:
        try:
            return float(secs)
        except ValueError:
            pass
    return None


class BudgetExceeded(RuntimeError):
    """Raised when the monthly LLM spend cap is hit — a runaway backstop."""


async def llm_create(**kwargs):
    """Resilient chat.completions.create: provider routing + shared concurrency cap +
    backoff on 429/5xx + spend tracking.

    Routes by `model` (gpt-* → OpenAI, else Kimi/Azure). Retryable failures (429/5xx)
    retry up to _MAX_ATTEMPTS with Retry-After-aware backoff. Non-retryable errors
    (e.g. 400) raise immediately. Raises BudgetExceeded if the monthly cap is hit."""
    model = kwargs.get("model", MODEL)
    if _over_cap():
        log.error("llm_budget_cap_reached", month=_month_key(), cap=_MONTHLY_CAP_USD)
        raise BudgetExceeded(f"Monthly LLM spend cap (${_MONTHLY_CAP_USD}) reached — pausing LLM calls.")
    client = _client_for(model)
    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with _sem():
                resp = await asyncio.to_thread(client.chat.completions.create, **kwargs)
            _record_spend(model, getattr(resp, "usage", None))
            return resp
        except Exception as e:
            status = _status_of(e)
            if status not in _RETRYABLE_STATUS or attempt == _MAX_ATTEMPTS - 1:
                raise
            delay = _retry_after_seconds(e)
        if delay is None:
            delay = min(2 ** attempt + random.uniform(0, 1), 30.0)
        log.warning("llm_retry", model=model, status=status, attempt=attempt + 1, delay=round(delay, 1))
        await asyncio.sleep(delay)


def spend_summary() -> str:
    """Human-readable current-month LLM spend (for a status tool/endpoint)."""
    usd = _read_spend().get(_month_key(), {}).get("usd", 0.0)
    return f"LLM spend this month: ${usd:.2f} / ${_MONTHLY_CAP_USD:.0f} cap"
