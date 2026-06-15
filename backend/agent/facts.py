"""Bi-temporal facts: the part of Miles's memory that auto-enters a turn.

The store (miles.db) owns a `facts` table — normalized atomic statements with valid-time and
transaction-time, so supersession is non-destructive and history stays queryable. This module is
the brain over that table:

  embed_text()      — one OpenAI text-embedding-3-small@1024 vector for a statement (the SAME model
                      Graphiti uses, but decoupled from runtime.graphiti, so facts keep getting
                      embeddings whenever OPENAI_API_KEY is set even if FalkorDB/Graphiti is down).
  recall_facts()    — hybrid recall over currently-valid facts (cosine + keyword + recency). Pure
                      SQLite + numpy, synchronous, fast at the bounded fact count. Feeds BOTH the
                      search_facts tool and the auto-injected compile_context section.
  reconcile_facts() — the scribe's reconciliation pass: extract candidate statements from a finished
                      turn, embed them, retrieve the K nearest currently-valid facts, ask ONE
                      utility-model call for a Mem0-style op (ADD/UPDATE/DELETE/NOOP), and apply it
                      via FIXED parameterized SQL (the model never authors SQL). Best-effort, off the
                      hot loop, never raises.

Brute-force cosine (no FAISS/pgvector) is deliberate: active reconciliation collapses duplicates and
retracts stale facts, so the currently-valid set stays in the low hundreds for a single-person agent —
a numpy dot over a few-hundred-by-1024 matrix is sub-millisecond. numpy is guarded: if it's somehow
absent, recall degrades to keyword+recency and embeddings are skipped.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re

import structlog

from agent import store
from agent.llm import UTILITY_MODEL, llm_create

log = structlog.get_logger()

try:
    import numpy as _np
except Exception:  # numpy is present (graphiti-core[falkordb] + explicit dep); guard anyway
    _np = None

_EMBED_MODEL = "text-embedding-3-small"
_EMBED_DIM = 1024

# Hybrid recall weights. Confidence is stored but NOT used in scoring yet (a later pass can
# down-weight low-confidence facts). With no embedding (OPENAI_API_KEY unset) w_cos drops out and
# recall degrades to keyword+recency, re-normalized below.
_W_COS = 0.6
_W_KW = 0.25
_W_REC = 0.15
_RECENCY_HALFLIFE_DAYS = 30.0

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)
_WORD = re.compile(r"[a-z0-9]+")
# Candidate facts per turn, mirroring the scribe's episode cap (<=5).
_MAX_CANDIDATES = 5
# How many nearest currently-valid facts to show the op model as reconciliation context.
_K_NEAREST = 6


# ── Embedding ────────────────────────────────────────────────────────────────────

def embed_text(text: str) -> bytes | None:
    """A 1024-d text-embedding-3-small vector as float32 little-endian bytes, or None if
    OPENAI_API_KEY is unset / numpy is missing / the call errors. Never raises. Synchronous —
    called off the hot loop (scribe) or via asyncio.to_thread (tools)."""
    key = os.getenv("OPENAI_API_KEY")
    if not key or _np is None:
        return None
    text = (text or "").strip()
    if not text:
        return None
    try:
        from openai import OpenAI

        # Tight timeout: openai's default read timeout is ~600s, so a hung connection would
        # otherwise stall whatever called us (the search_facts tool path) for ten minutes.
        client = OpenAI(api_key=key, timeout=5.0)
        resp = client.embeddings.create(model=_EMBED_MODEL, input=text, dimensions=_EMBED_DIM)
        return _pack_vec(resp.data[0].embedding)
    except Exception as e:
        log.warning("fact_embed_failed", err=str(e))
        return None


def _pack_vec(v) -> bytes:
    return _np.asarray(v, dtype=_np.float32).tobytes()


def _unpack_vec(b: bytes):
    return _np.frombuffer(b, dtype=_np.float32)


def cosine(a, b) -> float:
    """Cosine similarity of two same-length float vectors, 0.0 on a degenerate input."""
    if a is None or b is None or len(a) != len(b):
        return 0.0
    denom = float(_np.linalg.norm(a)) * float(_np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(_np.dot(a, b) / denom)


# ── Normalization / keys ─────────────────────────────────────────────────────────

def normalize_statement(s: str) -> str:
    """Lowercase, collapse whitespace, strip trailing punctuation — the canonical form a
    statement_key is hashed from (NOT the display text; the original cased statement is stored)."""
    return " ".join((s or "").lower().split()).rstrip(".,;:!?-—")


def _statement_key(s: str) -> str:
    return hashlib.sha256(normalize_statement(s).encode()).hexdigest()[:32]


def _keyword_overlap(query: str, statement: str) -> float:
    """Jaccard-ish overlap of content words — fraction of query words present in the statement."""
    q = set(_WORD.findall((query or "").lower()))
    s = set(_WORD.findall((statement or "").lower()))
    if not q:
        return 0.0
    return len(q & s) / len(q)


def _recency_decay(recorded_at: str) -> float:
    """Exponential decay in [0, 1] from how long ago the fact was recorded (newest ≈ 1)."""
    try:
        from datetime import datetime, timezone

        ts = datetime.fromisoformat(recorded_at)
        age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
        return math.exp(-max(age_days, 0.0) * math.log(2) / _RECENCY_HALFLIFE_DAYS)
    except Exception:
        return 0.0


# ── Recall (hybrid: cosine + keyword + recency) ──────────────────────────────────

def recall_facts(
    query: str, k: int = 8, *, max_chars: int | None = None, allow_embed: bool = True
) -> list[dict]:
    """Top-k currently-valid facts by hybrid score. cosine (if an embedding exists for the query
    AND the fact), keyword overlap, and recency. When OPENAI_API_KEY is unset / numpy is missing,
    the cosine term drops out and the remaining weights re-normalize, so recall still works.

    allow_embed=False forces the pure keyword+recency path with NO network call — used on the
    per-turn compile hot path (compile_context) so a turn is never blocked on an OpenAI round-trip.
    The explicit search_facts tool keeps allow_embed=True (full cosine hybrid); it runs in
    asyncio.to_thread, so the blocking embed is fine there.

    Returns fact dicts (id, statement, recorded_at, confidence, score) sorted desc; if max_chars
    is set, the list is truncated so the joined statements stay under that budget. Synchronous;
    store reads happen under _lock inside currently_valid_facts."""
    query = (query or "").strip()
    if not query:
        return []
    rows = store.currently_valid_facts()
    if not rows:
        return []

    # Only pay for the query embed when it can actually contribute: caller allows it, numpy is
    # present, AND at least one currently-valid fact has a stored vector to compare against.
    q_emb = None
    if allow_embed and _np is not None and any(r.get("embedding") for r in rows):
        packed = embed_text(query)
        if packed is not None:
            q_emb = _unpack_vec(packed)

    use_cos = q_emb is not None
    if use_cos:
        w_cos, w_kw, w_rec = _W_COS, _W_KW, _W_REC
    else:
        # Re-normalize over the two surviving terms so scores stay comparable to the full path.
        total = _W_KW + _W_REC
        w_cos, w_kw, w_rec = 0.0, _W_KW / total, _W_REC / total

    scored: list[dict] = []
    for r in rows:
        cos = 0.0
        if use_cos and r.get("embedding"):
            cos = cosine(q_emb, _unpack_vec(r["embedding"]))
        kw = _keyword_overlap(query, r["statement"])
        rec = _recency_decay(r["recorded_at"])
        score = w_cos * cos + w_kw * kw + w_rec * rec
        scored.append({
            "id": r["id"],
            "statement": r["statement"],
            "recorded_at": r["recorded_at"],
            "confidence": r["confidence"],
            "score": score,
        })

    scored.sort(key=lambda d: d["score"], reverse=True)
    top = scored[:k]

    if max_chars is not None:
        out: list[dict] = []
        used = 0
        for d in top:
            cost = len(d["statement"]) + 3  # "- " prefix + newline
            if out and used + cost > max_chars:
                break
            out.append(d)
            used += cost
        return out
    return top


# ── Reconciliation (extract → embed → retrieve K → one op → fixed apply) ──────────

_EXTRACT_SYSTEM = """You extract durable, atomic FACTS from one finished turn of an autonomous CMO agent (Miles) — the kind worth remembering long-term so future turns don't re-learn them.

Return STRICT JSON, nothing else:
{"facts": ["<one normalized standalone statement>", ...]}

Rules:
- 0 to 5 facts. Each must be a single self-contained statement that stays true beyond this turn — a person and their role/company, a stable preference, a relationship, a commitment made, a decision that stands, a concrete attribute learned.
- Write each as a complete sentence understandable WITHOUT the turn ("Sarah Chen is VP of Engineering at Acme." not "she's the VP"). Resolve pronouns; keep names, numbers, companies, roles.
- NO transient events (an email was read, a search ran), NO speculation, NO restating the prompt. Only durable knowledge the turn actually establishes.
- If the turn establishes nothing durable, return {"facts": []}."""

_RECONCILE_SYSTEM = """You maintain a knowledge base of atomic facts for an autonomous agent. You are given ONE new candidate fact and the most similar EXISTING facts (each with a numeric id). Decide the single best operation so the base stays correct and deduplicated.

Return STRICT JSON, nothing else:
{"op": "ADD" | "UPDATE" | "DELETE" | "NOOP", "target_fact_id": <id or null>, "statement": "<text or null>"}

- ADD: the candidate is genuinely new knowledge not covered by any existing fact. target_fact_id null. statement = the candidate (cleaned/normalized).
- UPDATE: the candidate SUPERSEDES an existing fact (same subject+attribute, new value — e.g. a role change, a corrected number). target_fact_id = the existing fact it replaces. statement = the new combined fact.
- DELETE: the candidate makes an existing fact FALSE/RETRACTED with no replacement (a relationship ended, a fact was wrong). target_fact_id = the existing fact to retract. statement null.
- NOOP: the candidate is already captured by an existing fact (a duplicate or weaker restatement). target_fact_id may name the duplicate. statement null.

target_fact_id MUST be one of the ids shown or null. When unsure between ADD and UPDATE, prefer ADD."""


def _content_of(msg) -> str:
    """LLM message text, falling back to reasoning_content for the DeepSeek/utility path where
    content can be None (mirrors dream() / memory.py). Always a str."""
    return (getattr(msg, "content", None) or getattr(msg, "reasoning_content", "") or "").strip()


def _parse_json(raw: str) -> dict:
    raw = _FENCE.sub("", (raw or "").strip())
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


def _render_episodes(episode_rows: list[dict]) -> str:
    lines = []
    for e in episode_rows:
        kind = e.get("kind") or "observation"
        content = str(e.get("content") or "").strip()
        if content:
            lines.append(f"[{kind}] {content}")
    return "\n".join(lines)


async def _extract_candidates(episode_rows: list[dict]) -> list[str]:
    rendered = _render_episodes(episode_rows)
    if not rendered:
        return []
    resp = await llm_create(
        model=UTILITY_MODEL,
        messages=[
            {"role": "system", "content": _EXTRACT_SYSTEM},
            {"role": "user", "content": f"The turn's recorded episodes:\n{rendered}\n\nReturn the JSON."},
        ],
        max_tokens=600,
        temperature=0.1,
    )
    data = _parse_json(_content_of(resp.choices[0].message))
    out: list[str] = []
    for f in (data.get("facts") or [])[:_MAX_CANDIDATES]:
        s = str(f or "").strip()
        if s:
            out.append(s)
    return out


async def _decide_op(candidate: str, nearest: list[dict]) -> dict:
    """One utility-model call to choose ADD/UPDATE/DELETE/NOOP for a candidate against its K
    nearest currently-valid facts. Returns the parsed op dict (empty -> caller treats as NOOP)."""
    existing = "\n".join(f"  id={r['id']}: {r['statement']}" for r in nearest) or "  (none)"
    user = (
        f"Candidate fact:\n  {candidate}\n\n"
        f"Most similar existing facts:\n{existing}\n\n"
        "Return the JSON op."
    )
    resp = await llm_create(
        model=UTILITY_MODEL,
        messages=[
            {"role": "system", "content": _RECONCILE_SYSTEM},
            {"role": "user", "content": user},
        ],
        max_tokens=200,
        temperature=0,
    )
    return _parse_json(_content_of(resp.choices[0].message))


def _nearest_valid(candidate: str, cand_emb: bytes | None, k: int = _K_NEAREST) -> list[dict]:
    """The k currently-valid facts most similar to a candidate — by cosine if both have an
    embedding, else by keyword overlap, with recency as the final tiebreak. Used only as
    reconciliation context (the op model picks among these ids); never authors SQL."""
    rows = store.currently_valid_facts()
    if not rows:
        return []
    q_emb = _unpack_vec(cand_emb) if (cand_emb is not None and _np is not None) else None
    scored = []
    for r in rows:
        if q_emb is not None and r.get("embedding"):
            sim = cosine(q_emb, _unpack_vec(r["embedding"]))
        else:
            sim = _keyword_overlap(candidate, r["statement"])
        scored.append((sim, _recency_decay(r["recorded_at"]), r))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [r for _, _, r in scored[:k]]


async def reconcile_facts(episode_rows: list[dict]) -> int:
    """Turn a finished turn's episodes into reconciled facts. Best-effort and off the hot loop
    (called from the scribe): extract candidates, and for each — skip if already a valid duplicate
    (statement_key), embed it, retrieve the K nearest currently-valid facts, ask ONE utility-model
    op, then APPLY via fixed parameterized store.* calls (the model never authors SQL). An op whose
    target_fact_id isn't among the retrieved valid ids is downgraded (UPDATE->ADD, DELETE->NOOP) so
    a hallucinated id can never corrupt the base. Returns the count of facts applied. Never raises."""
    applied = 0
    try:
        candidates = await _extract_candidates(episode_rows)
    except Exception as e:
        log.warning("fact_extract_failed", err=str(e))
        return 0

    # source_episode_id links a fact to the episode it came from (idempotency alongside the key).
    src_episode_id = None
    for e in episode_rows:
        if e.get("id"):
            src_episode_id = e["id"]
            break

    for candidate in candidates:
        try:
            key = _statement_key(candidate)
            # Idempotent: a still-valid fact with this exact key already exists → nothing to do.
            if any(_statement_key(r["statement"]) == key for r in store.currently_valid_facts()):
                continue

            cand_emb = embed_text(candidate)
            nearest = _nearest_valid(candidate, cand_emb)
            valid_ids = {r["id"] for r in nearest}

            decision = await _decide_op(candidate, nearest)
            op = str(decision.get("op") or "NOOP").strip().upper()
            target = decision.get("target_fact_id")
            target = int(target) if isinstance(target, (int, float)) and not isinstance(target, bool) else None
            new_stmt = str(decision.get("statement") or "").strip() or candidate

            # Validate the model's target against the retrieved valid set; downgrade if it's bogus.
            if op in ("UPDATE", "DELETE") and target not in valid_ids:
                op = "ADD" if op == "UPDATE" else "NOOP"

            if op == "ADD":
                if store.add_fact(candidate, embedding=cand_emb, source_episode_id=src_episode_id) is not None:
                    applied += 1
            elif op == "UPDATE":
                # Insert the replacement FIRST; only expire the old fact if the new one actually
                # inserted. If new_stmt collides with a DIFFERENT still-valid fact (INSERT OR
                # IGNORE → None) or is empty, treat as NOOP and leave the target intact — never
                # strand an expired fact with no replacement (F5). On a retry of a successful
                # UPDATE the new row already exists, so add returns None and we skip expire; the
                # target was already expired, so net state is unchanged — still idempotent.
                new_emb = embed_text(new_stmt) if new_stmt != candidate else cand_emb
                new_id = store.add_fact(new_stmt, embedding=new_emb, source_episode_id=src_episode_id)
                if new_id is not None:
                    store.expire_fact(target)
                    applied += 1
            elif op == "DELETE":
                store.retract_fact(target)
                applied += 1
            # NOOP and any unknown op: skip (no write).
        except Exception as e:
            log.warning("fact_reconcile_op_failed", err=str(e))

    if applied:
        log.info("facts_reconciled", candidates=len(candidates), applied=applied)
    return applied
