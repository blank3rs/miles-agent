"""Strip the tells that out an email as AI-written, before it goes out.

Mechanical fixes (em-dashes, curly quotes, canned openers/closers) are deterministic
and always applied. Word/phrase-level tells that can't be safely auto-replaced —
'delve', 'leverage', the "it's not X, it's Y" construction — trigger ONE cheap rewrite
pass (gpt-4o-mini) that fixes exactly those while preserving meaning, length, and
Miles's direct voice. Clean emails skip the LLM entirely.
"""
import re

import structlog

from agent.llm import UTILITY_MODEL, llm_create

log = structlog.get_logger()

# Canned AI openers/closers — safe to strip outright.
_CANNED = [
    r"(?im)^\s*I hope this (email|message|note) finds you well[.,!]?\s*",
    r"(?im)^\s*(Certainly|Absolutely|Great question)[!,.]\s*",
    r"(?im)^\s*Thank you for reaching out[.,!]?\s*",
    r"(?im)\bI['’]?d be happy to\b",
    r"(?im)\bplease (don['’]?t hesitate|feel free) to reach out\b",
    r"(?im)^\s*I hope (this|that) helps[.!]?\s*$",
]

# AI-tell vocabulary (whole words) and structural patterns — detected, then rewritten.
_TELL_WORDS = [
    "delve", "leverage", "robust", "seamless", "seamlessly", "utilize", "showcase",
    "underscore", "testament", "tapestry", "synergy", "landscape", "embark", "foster",
    "elevate", "unleash", "streamline", "moreover", "furthermore", "additionally",
    "crucially", "pivotal", "game-changer", "cutting-edge", "revolutionary", "delving",
]
_TELL_PATTERNS = [
    (r"(?i)\bit['’]?s not (just )?[^.,;\n]+,\s*it['’]?s\b", "'it's not X, it's Y' construction"),
    (r"(?i)\bnot only [^.,;\n]+ but (also )?\b", "'not only X but Y' construction"),
    (r"(?i)\bno [a-z]+\. no [a-z]+\. just\b", "'No X. No Y. Just Z.' construction"),
]


def _mechanical(text: str) -> str:
    text = text.replace("—", ", ").replace("–", "-")          # em / en dash
    text = (text.replace("’", "'").replace("‘", "'")
                .replace("“", '"').replace("”", '"'))          # curly quotes
    for pat in _CANNED:
        text = re.sub(pat, "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def detect_tells(text: str) -> list[str]:
    hits = []
    low = text.lower()
    for w in _TELL_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", low):
            hits.append(w)
    for pat, label in _TELL_PATTERNS:
        if re.search(pat, text):
            hits.append(label)
    return hits


async def polish_email_body(body: str) -> str:
    """Return a cleaned body: always mechanical-scrubbed; one targeted LLM rewrite only
    if word/structure tells remain. Falls back to the scrubbed body on any failure."""
    cleaned = _mechanical(body)
    tells = detect_tells(cleaned)
    if not tells:
        return cleaned
    try:
        prompt = (
            "Rewrite this email so it reads like a sharp, busy founder wrote it fast — direct, plain, human. "
            "Remove these specific AI tells without changing the meaning, the facts, or the length much, and "
            f"keep the sender's voice: {', '.join(sorted(set(tells)))}. No em-dashes, no corporate filler, no "
            "\"it's not X, it's Y\" constructions, no throat-clearing openers. Return ONLY the rewritten email "
            f"body, nothing else.\n\n---\n{cleaned}\n---"
        )
        resp = await llm_create(
            model=UTILITY_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1400,
            temperature=0.4,
        )
        out = _mechanical((resp.choices[0].message.content or "").strip())
        # Guard against a mangled/empty rewrite — only accept if it's substantial.
        if out and len(out) > 0.4 * len(cleaned):
            log.info("email_polished", tells=len(tells))
            return out
    except Exception as e:
        log.warning("email_polish_failed", err=str(e))
    return cleaned
