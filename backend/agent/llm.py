"""Resilient, TIERED LLM access for every model caller.

One entry point — llm_create — routes by a TIER alias to the right model + endpoint through
LiteLLM, which normalizes Azure AI Foundry's two surfaces (Claude's native /anthropic Messages
API and the /openai/v1 trio) into one OpenAI-shaped call. Callers keep the OpenAI message +
tool-call format, so the orchestrator can be Claude without rewriting core.py.

  orchestrator -> Claude Opus 4.8   (Foundry /anthropic)   — the main loop's brain (plans, delegates)
  worker       -> DeepSeek-V4-Pro   (Foundry /openai/v1)   — sub-agents, dreams (high volume, cheap)
  utility      -> gpt-4o-mini        (OpenAI) or worker     — scribe, style, classify
  worker_kimi  -> Kimi-K2.6          (AZURE_ENDPOINT)        — escalation + the failover brain

Resilience: each LANE (orchestrator / worker / utility) has its own concurrency semaphore so a
worker 429 storm can't starve Opus; retries cover 429/5xx AND connection/timeout errors; and if a
Foundry tier dies (outage, revoked key, sustained 429) the call FAILS OVER to Kimi so the loop
stays up instead of going dark. If FOUNDRY_* is unset the whole stack degrades to Kimi at import.
Spend is metered per-tier with a monthly circuit-breaker. The static key never reaches a log.
"""
import asyncio
import json
import os
import random
import time
from datetime import datetime, timezone

import litellm
import structlog

from .config import (
    AZURE_API_KEY,
    AZURE_ENDPOINT,
    DATA_DIR,
    FOUNDRY_API_KEY,
    FOUNDRY_ENDPOINT,
    MODEL,
)

log = structlog.get_logger()

litellm.drop_params = True        # silently drop a param a given provider doesn't support
litellm.suppress_debug_info = True

# Exceptions that mean "transient — retry", on top of the retryable HTTP statuses. LiteLLM wraps
# socket/DNS/TLS resets and read timeouts as these and they carry no .status_code, so a status-only
# check would never retry the most common always-on failures.
try:
    from litellm.exceptions import (
        APIConnectionError,
        InternalServerError,
        ServiceUnavailableError,
        Timeout,
    )
    _RETRYABLE_EXC: tuple = (APIConnectionError, Timeout, ServiceUnavailableError, InternalServerError)
except Exception:  # pragma: no cover — defensive across litellm versions
    _RETRYABLE_EXC = ()

_openai_key = os.getenv("OPENAI_API_KEY", "")


# ── Tier registry ────────────────────────────────────────────────────────────────
def _build_tiers() -> dict:
    """alias -> {model, api_base, api_key, lane}. lane selects the concurrency semaphore."""
    t: dict = {}
    if FOUNDRY_ENDPOINT and FOUNDRY_API_KEY:
        t["orchestrator"] = {
            "model": "azure_ai/" + os.getenv("ORCH_DEPLOYMENT", "claude-opus-4-8"),
            "api_base": FOUNDRY_ENDPOINT + "/anthropic", "api_key": FOUNDRY_API_KEY, "lane": "orch",
        }
        t["worker"] = {
            "model": "openai/" + os.getenv("WORKER_DEPLOYMENT", "DeepSeek-V4-Pro"),
            "api_base": FOUNDRY_ENDPOINT + "/openai/v1", "api_key": FOUNDRY_API_KEY, "lane": "work",
        }
    else:
        log.warning("foundry_unset_degrading_to_kimi")
        kimi = {"model": "openai/" + MODEL, "api_base": AZURE_ENDPOINT, "api_key": AZURE_API_KEY}
        t["orchestrator"] = {**kimi, "lane": "orch"}
        t["worker"] = {**kimi, "lane": "work"}
    # Kimi escalation/failover worker (existing endpoint).
    t["worker_kimi"] = {"model": "openai/" + MODEL, "api_base": AZURE_ENDPOINT, "api_key": AZURE_API_KEY, "lane": "work"}
    # Utility — cheap, on its OWN lane so scribe/style bursts can't eat worker capacity.
    if _openai_key:
        t["utility"] = {"model": "openai/gpt-4o-mini", "api_base": None, "api_key": _openai_key, "lane": "util"}
    else:
        t["utility"] = {**t["worker"], "lane": "util"}
    return t


_TIERS = _build_tiers()
UTILITY_MODEL = "utility"   # re-exported alias (scribe.py, style.py import this)

# Orchestrator prompt caching. The Claude-native Foundry /anthropic surface is the ONLY tier that
# honours cache_control; it's the model-string prefix every orchestrator deployment carries.
# Anthropic renders `tools` BEFORE `system`, so a breakpoint on system[0] caches (tools + persona
# prefix). The orchestrator now sends the FIXED full tool catalog (core.run's Opus branch), so that
# prefix is byte-stable across turns and reads at ~0.1x within the TTL. Gated by env so it can be
# flipped off without a redeploy if Foundry rejects it. Verify the real hit rate via the llm_cache
# log (cache_read vs cache_creation) rather than assuming it.
_CACHE_ELIGIBLE_PREFIX = "azure_ai/"
_ORCH_PROMPT_CACHE = os.getenv("ORCH_PROMPT_CACHE", "true").lower() == "true"

# Price each tier by its ALIAS, not the response model — a Foundry deployment id (which the
# operator can rename via ORCH_DEPLOYMENT) often isn't in any price table, and metering the
# orchestrator as if it were cheap is how a $300 cap becomes a $5k bill.
_PRICING = {                       # ($/token in, out)
    "claude-opus": (15.0e-6, 75.0e-6),
    "deepseek":    (0.5e-6,  1.5e-6),
    "grok":        (1.25e-6, 2.5e-6),
    "kimi":        (0.95e-6, 4.0e-6),
    "gpt-4o-mini": (0.15e-6, 0.60e-6),
}
_TIER_PRICE = {
    "orchestrator": _PRICING["claude-opus"],
    "worker":       _PRICING["deepseek"],
    "worker_kimi":  _PRICING["kimi"],
    "utility":      _PRICING["gpt-4o-mini"],
}

_MAX_ATTEMPTS = int(os.getenv("LLM_MAX_ATTEMPTS", "6"))
_ORCH_MAX_RETRY_SECS = float(os.getenv("LLM_ORCH_MAX_RETRY_SECS", "45"))  # then fail over, don't stall the consumer
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_LANE_CONCURRENCY = {
    "orch": int(os.getenv("LLM_ORCH_CONCURRENCY", "4")),
    "work": int(os.getenv("LLM_WORKER_CONCURRENCY", "8")),
    "util": int(os.getenv("LLM_UTILITY_CONCURRENCY", "4")),
}
_sems: dict = {}


def _sem(lane: str) -> asyncio.Semaphore:
    if lane not in _sems:
        _sems[lane] = asyncio.Semaphore(_LANE_CONCURRENCY.get(lane, 4))
    return _sems[lane]


# ── Secret scrubbing ─────────────────────────────────────────────────────────────
_SECRETS = [s for s in (FOUNDRY_API_KEY, AZURE_API_KEY, _openai_key) if s]


def _scrub(text: str) -> str:
    """Strip any known API key out of a string before it can reach a log or a re-raised error.
    The single static Foundry key fronts the whole resource and never rotates — one leaked line
    is a full compromise."""
    for s in _SECRETS:
        if s:
            text = text.replace(s, "[redacted-key]")
    return text


# ── Spend tracking ───────────────────────────────────────────────────────────────
_MONTHLY_CAP_USD = float(os.getenv("LLM_MONTHLY_USD_CAP", "600"))   # monthly LLM-API cost backstop
_SPEND_FILE = DATA_DIR / "llm_spend.json"


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _read_spend() -> dict:
    try:
        return json.loads(_SPEND_FILE.read_text())
    except Exception:
        return {}


def _record_spend(resp, alias: str) -> None:
    # Prefer LiteLLM's exact cost when it knows the model; otherwise price by TIER (fail-safe
    # expensive) from usage. Never default to "cheapest" — an unknown model bills as the orchestrator.
    usage = getattr(resp, "usage", None)
    # Cache accounting. With prompt caching on, the OpenAI-normalized prompt_tokens EXCLUDES
    # cached reads, so a cache-blind fallback both treats reads as free and skips the write
    # premium — under-billing against the cap. Read the two cache counts (anthropic names first,
    # OpenAI prompt_tokens_details.cached_tokens as fallback) and bill them explicitly below.
    cr = cw = 0
    if usage:
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        if cr == 0:
            details = getattr(usage, "prompt_tokens_details", None)
            cr = getattr(details, "cached_tokens", 0) or 0
        if alias == "orchestrator" or (_TIERS.get(alias, {}).get("model", "").startswith(_CACHE_ELIGIBLE_PREFIX)):
            log.info("llm_cache", cache_read=cr, cache_creation=cw,
                     prompt=getattr(usage, "prompt_tokens", 0) or 0)

    cost = 0.0
    try:
        cost = litellm.completion_cost(completion_response=resp) or 0.0
    except Exception:
        cost = 0.0
    if not cost and usage:
        pin, pout = _TIER_PRICE.get(alias, _PRICING["claude-opus"])  # unknown alias -> most expensive
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        # prompt_tokens may already exclude reads (Anthropic shape) or not (some surfaces); subtract
        # with a floor so a double-subtract can only mis-count the cheap 0.1x read, never the writes.
        uncached = max(0, prompt_tokens - cr)
        cost = (uncached * pin
                + cr * pin * 0.1
                + cw * pin * 1.25
                + (getattr(usage, "completion_tokens", 0) or 0) * pout)
    if not cost:
        return
    try:
        data = _read_spend()
        mk = _month_key()
        month = data.get(mk, {"usd": 0.0})
        before = month["usd"]
        month["usd"] = round(before + cost, 6)
        data[mk] = month
        _SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SPEND_FILE.write_text(json.dumps(data))
        if before < _MONTHLY_CAP_USD * 0.8 <= month["usd"]:
            log.warning("llm_spend_80pct", month=mk, usd=round(month["usd"], 2), cap=_MONTHLY_CAP_USD)
    except Exception:
        pass


def _over_cap() -> bool:
    if _MONTHLY_CAP_USD <= 0:
        return False
    return _read_spend().get(_month_key(), {}).get("usd", 0.0) >= _MONTHLY_CAP_USD


def spend_summary() -> str:
    usd = _read_spend().get(_month_key(), {}).get("usd", 0.0)
    return f"LLM spend this month: ${usd:.2f} / ${_MONTHLY_CAP_USD:.0f} cap"


class BudgetExceeded(RuntimeError):
    """Raised when the monthly LLM spend cap is hit — a runaway backstop."""


def _status_of(exc) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def _retryable(exc, status) -> bool:
    return status in _RETRYABLE_STATUS or isinstance(exc, _RETRYABLE_EXC)


def _retry_after_seconds(exc) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    for key in ("retry-after-ms",):
        v = headers.get(key)
        if v:
            try:
                return float(v) / 1000.0
            except ValueError:
                pass
    for key in ("retry-after", "x-ratelimit-reset"):
        v = headers.get(key)
        if v:
            try:
                return float(v)
            except ValueError:
                pass
    return None


def _strip_reasoning(messages: list[dict]) -> list[dict]:
    """reasoning_content is a Kimi-only round-trip field. For any other model (Opus, DeepSeek)
    it's noise at best and breaks the Anthropic translation at worst — Kimi-era history carries
    it, so drop it for non-Kimi tiers. tool_calls and every other field are kept."""
    if not any(m.get("reasoning_content") for m in messages):
        return messages
    return [{k: v for k, v in m.items() if k != "reasoning_content"} for m in messages]


def _maybe_add_cache_control(call: dict, model: str) -> None:
    """Attach an ephemeral cache breakpoint to the STABLE (first) system block — orchestrator
    tier only. Anthropic renders `tools` BEFORE `system` and caches the prefix up to the
    breakpoint, so tagging block[0] (persona + tool index) caches (tools + block[0]). That prefix
    is reused across the many tool ROUNDS of a single turn always, and across TURNS only when the
    next turn's tool set is byte-identical — which holds on this tier because the orchestrator now
    sends the FIXED full catalog (any tool add/remove/reorder would otherwise invalidate the whole
    prefix). So cross-turn reuse is real here, not universal magic; confirm it with the llm_cache
    log. The volatile block (block[1+], the recompiled context) is intentionally left untagged so
    we never pay the 1.25x write premium on content that never repeats. Copies the message dict and
    content list before writing the key so the durable in-turn `messages` list (persisted + reused
    across the tool loop) is never mutated — cache_control is a per-API-call directive and must not
    reach the DB. No-op for any non-orchestrator tier or a string-content system message."""
    if not (_ORCH_PROMPT_CACHE and model.startswith(_CACHE_ELIGIBLE_PREFIX)):
        return
    msgs = call.get("messages")
    if not msgs or msgs[0].get("role") != "system":
        return
    content = msgs[0].get("content")
    if not isinstance(content, list) or not content:
        return
    first = {**content[0], "cache_control": {"type": "ephemeral"}}
    new_content = [first] + content[1:]
    call["messages"] = [{**msgs[0], "content": new_content}] + msgs[1:]


async def _attempt_tier(alias: str, kwargs: dict):
    """Run one tier's call with retries (status + connection/timeout), per-lane concurrency,
    and a wall-time cap for the orchestrator so a 429 storm fails over instead of stalling the
    single consumer. Raises the last error if it can't succeed."""
    tier = _TIERS.get(alias) or _TIERS["worker"]
    if not tier.get("api_key"):
        raise RuntimeError(f"[llm] tier '{alias}' has no API key configured — check FOUNDRY_API_KEY / AZURE_API_KEY")

    call = dict(kwargs)
    call["model"] = tier["model"]
    if tier.get("api_base"):
        call["api_base"] = tier["api_base"]
    call["api_key"] = tier["api_key"]
    call["num_retries"] = 0  # we own retries
    call.setdefault("max_tokens", 8192)  # Foundry's Claude surface rejects calls without it
    # Foundry's Claude/Opus surface 400s on `temperature` ("deprecated for this model").
    # litellm.drop_params can't predict this provider-specific rejection, so strip it on the
    # orchestrator tier. Other tiers (DeepSeek/gpt-4o-mini/Kimi) still honor temperature.
    if tier["model"].startswith(_CACHE_ELIGIBLE_PREFIX):
        call.pop("temperature", None)
    if "kimi" not in tier["model"].lower() and call.get("messages"):
        call["messages"] = _strip_reasoning(call["messages"])
    _maybe_add_cache_control(call, tier["model"])

    sem = _sem(tier["lane"])
    started = time.monotonic()
    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with sem:
                resp = await litellm.acompletion(**call)
            _record_spend(resp, alias)
            return resp
        except Exception as e:
            status = _status_of(e)
            last = attempt == _MAX_ATTEMPTS - 1
            if not _retryable(e, status) or last:
                raise
            delay = _retry_after_seconds(e)
            if delay is None:
                delay = min(2 ** attempt + random.uniform(0, 1), 30.0)
            # The orchestrator runs on the single consumer — don't let a 429 storm stall the
            # whole queue; bail to the failover path once we've spent the retry budget.
            if tier["lane"] == "orch" and (time.monotonic() - started) + delay > _ORCH_MAX_RETRY_SECS:
                raise
            log.warning("llm_retry", tier=alias, status=status,
                        err=type(e).__name__, attempt=attempt + 1, delay=round(delay, 1))
            await asyncio.sleep(delay)


async def llm_create(**kwargs):
    """Resilient tiered chat completion via LiteLLM. `model` is a TIER alias
    (orchestrator | worker | utility | worker_kimi); resolved to the real deployment + endpoint.
    On a Foundry availability/auth failure of the orchestrator or worker tier, fails over once to
    Kimi so the loop stays up. Raises BudgetExceeded at the monthly cap."""
    alias = kwargs.pop("model", "worker")
    if _over_cap():
        log.error("llm_budget_cap_reached", month=_month_key(), cap=_MONTHLY_CAP_USD)
        raise BudgetExceeded(f"Monthly LLM spend cap (${_MONTHLY_CAP_USD}) reached — pausing LLM calls.")
    try:
        return await _attempt_tier(alias, kwargs)
    except BudgetExceeded:
        raise
    except Exception as e:
        status = _status_of(e)
        # A 400 is a request problem (e.g. context overflow) — the caller handles that, don't
        # fail over (Kimi would 400 too). Anything else on a Foundry tier sheds to Kimi.
        can_failover = (
            status != 400
            and alias in ("orchestrator", "worker")
            and _TIERS.get("worker_kimi", {}).get("api_key")
            and _TIERS["worker_kimi"]["api_base"] != _TIERS[alias].get("api_base")
        )
        if can_failover:
            log.warning("foundry_failover_to_kimi", tier=alias, status=status, err=type(e).__name__)
            try:
                return await _attempt_tier("worker_kimi", kwargs)
            except Exception as e2:
                raise RuntimeError(_scrub(f"[LLM error] {alias} failed and Kimi failover failed: {e2}")) from None
        raise RuntimeError(_scrub(f"[LLM error] {type(e).__name__}: {e}")) from None
