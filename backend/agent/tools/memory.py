"""Journal + dreaming (sleep-time memory consolidation) + knowledge-graph search.

dream() is incremental — a cursor in data/dreams/{date}.json tracks how many journal
entries each date has already processed, so the 4-hourly cron never re-ingests events.
It also owns the dream sections of soul.md (Letta pattern: the sleep agent has write
authority over memory blocks; the waking agent just reads them).
"""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import structlog
from openai import OpenAI

from agent import runtime
from agent.config import AZURE_API_KEY, AZURE_ENDPOINT, DREAMS_DIR, JOURNAL_DIR, MODEL, SOUL_FILE

log = structlog.get_logger()

_DREAM_LENSES = [
    ("facts",         "List the specific facts and knowledge Miles learned today. Be concrete and exact."),
    ("people",        "Identify every person mentioned today. For each: who they are, what was discussed, what Miles now knows about them."),
    ("decisions",     "What decisions were made today? What was the reasoning? What alternatives were considered or rejected?"),
    ("tasks",         "What work was completed today? What's still open? What was blocked or unblocked?"),
    ("business",      "What business-relevant observations came up today? Competitive, market, product, or operational signals."),
    ("patterns",      "What themes or patterns appeared repeatedly across today's events? What keeps coming up?"),
    ("surprises",     "What was unexpected or surprising today? What didn't go as anticipated?"),
    ("gaps",          "What questions remain open? What does Miles not understand well enough? What needs research?"),
    ("relationships", "How did relationships with people, companies, or partners evolve today? Trust built or eroded?"),
    ("learnings",     "What technical fixes, workarounds, or tool discoveries happened today? For each: what broke, what was tried that failed, and exactly what worked. Write as a reference card — specific enough that future-Miles can search for it and know what to do without repeating the debugging."),
    ("narrative",     "Write an honest, first-person narrative of Miles's day. What mattered most? What is he thinking about going forward?"),
]

# Sections of soul.md that dream() owns and rewrites
_SOUL_SECTIONS = ("## What I'm learning", "## People I know", "## Things that matter right now")

_SOUL_PROMPT = """You maintain the dream-owned sections of Miles Kuncet's soul file — his persistent sense of self. This is long-term memory: treat it as APPEND-AND-REFINE, not a fresh rewrite. Rewriting from scratch each time silently erodes hard-won specifics ("context collapse") — don't do that.

Below are the current sections and today's dream analyses. Produce updated sections that fold in what today added or changed, under these rules:
- PRESERVE every specific, still-true item — names, numbers, concrete facts, lessons learned. Never drop a specific just to be concise.
- Only REMOVE an item if today's analyses make it contradicted or clearly obsolete.
- Merge true duplicates, tighten wording, group related points, and add what's genuinely new.
- Specifics beat tidiness; it's fine for a section to grow. Keep each under ~60 lines — if you'd exceed that, condense the vaguest items, never the most specific ones.
First person, plain prose or short lists.

Return exactly the three sections in markdown, each starting with its '## ' header, in this order, and nothing else:

## What I'm learning
## People I know
## Things that matter right now

Current sections:
{current}

Today's analyses ({date}):
{analyses}"""


def _client() -> OpenAI:
    return OpenAI(base_url=AZURE_ENDPOINT, api_key=AZURE_API_KEY)


async def journal_entry(event_type: str, content: str) -> str:
    """Log an event to today's journal — the raw material for dreaming."""
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "content": content,
        }
        with (JOURNAL_DIR / f"{today}.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
        return f"Logged [{event_type}]"
    except Exception as e:
        return f"[journal error] {e}"


def _extract_sections(soul: str) -> str:
    """Pull the current dream-owned sections out of soul.md for the update prompt."""
    parts = []
    for header in _SOUL_SECTIONS:
        m = re.search(rf"^{re.escape(header)}\s*\n(.*?)(?=^## |\Z)", soul, re.S | re.M)
        body = m.group(1).strip() if m else "(empty)"
        parts.append(f"{header}\n{body}")
    return "\n\n".join(parts)


def _parse_sections(text: str) -> dict[str, str]:
    """Split a model response into {header: body} for the known headers."""
    text = re.sub(r"^```(markdown)?\s*|\s*```$", "", text.strip())
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in _SOUL_SECTIONS:
            if current:
                sections[current] = "\n".join(buf).strip()
            current, buf = stripped, []
        elif current is not None:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return sections


def _replace_section(soul: str, header: str, body: str) -> str:
    pattern = re.compile(rf"^{re.escape(header)}\s*\n.*?(?=^## |\Z)", re.S | re.M)
    block = f"{header}\n\n{body}\n\n"
    if pattern.search(soul):
        return pattern.sub(lambda _: block, soul, count=1)
    return soul.rstrip() + "\n\n" + block


async def _update_soul(client: OpenAI, date_str: str, results: list[tuple[str, str]]) -> str:
    """Rewrite soul.md's dream sections from today's analyses. Returns a status line."""
    if not SOUL_FILE.exists():
        return "soul.md not found — skipped"
    try:
        soul = SOUL_FILE.read_text()
        analyses = "\n\n".join(
            f"### {name}\n{text}" for name, text in results if "[analysis failed" not in text
        )
        prompt = _SOUL_PROMPT.format(
            current=_extract_sections(soul), analyses=analyses, date=date_str
        )
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
            temperature=0.3,
        )
        sections = _parse_sections(resp.choices[0].message.content or "")
        if not sections:
            return "soul.md unchanged — could not parse model output"
        for header, body in sections.items():
            if body:
                soul = _replace_section(soul, header, body)
        SOUL_FILE.write_text(soul)
        return f"soul.md updated ({', '.join(h.lstrip('# ') for h in sections)})"
    except Exception as e:
        log.warning("soul_update_failed", err=str(e))
        return f"soul.md update failed: {e}"


async def dream(date_str: str = "") -> str:
    """Process new journal entries through parallel lens analyses, ingest into the
    knowledge graph, and rewrite soul.md's dream sections."""
    if not date_str:
        # At 4 AM the day's work is "yesterday" — back off 6h so the cron
        # dreams about the right date instead of a nearly-empty new day.
        date_str = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%d")

    journal_file = JOURNAL_DIR / f"{date_str}.jsonl"
    if not journal_file.exists():
        return f"(no journal entries for {date_str} — nothing to dream about)"

    entries = []
    for line in journal_file.read_text().strip().splitlines():
        try:
            entries.append(json.loads(line))
        except Exception:
            pass

    # Incremental cursor: skip entries already processed by a previous dream for this date
    dream_file = DREAMS_DIR / f"{date_str}.json"
    already_processed = 0
    if dream_file.exists():
        try:
            already_processed = int(json.loads(dream_file.read_text()).get("journal_entries", 0))
        except Exception:
            already_processed = 0
    new_entries = entries[already_processed:]

    if not new_entries:
        return f"(all {len(entries)} journal entries for {date_str} already dreamed — nothing new)"
    if len(entries) < 2:
        return f"(only {len(entries)} journal entry for {date_str} — skipping dream)"

    journal_text = "\n\n".join(
        f"[{e['ts']} | {e['type']}]\n{e['content']}" for e in new_entries
    )

    client = _client()

    async def _analyze(lens_name: str, lens_prompt: str) -> tuple[str, str]:
        try:
            resp = await asyncio.to_thread(
                client.chat.completions.create,
                model=MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"You are Miles Kuncet's reflective mind. Analyze these journal entries "
                            f"through exactly this lens: {lens_prompt}\n"
                            f"Be specific, concrete, first person. Date: {date_str}"
                        ),
                    },
                    {"role": "user", "content": f"Journal entries for {date_str}:\n\n{journal_text}"},
                ],
                max_tokens=600,
                temperature=0.3,
            )
            return lens_name, resp.choices[0].message.content.strip()
        except Exception as e:
            return lens_name, f"[analysis failed: {e}]"

    results: list[tuple[str, str]] = list(
        await asyncio.gather(*[_analyze(name, prompt) for name, prompt in _DREAM_LENSES])
    )

    # Ingest into Graphiti. Per-episode try/except so one failed extraction can't
    # abort the whole batch, and counts are logged — a silently-empty graph is how
    # the extraction bug went unnoticed for a full day.
    communities_built = False
    ingested = 0
    ingest_failed = 0
    if runtime.graphiti:
        from graphiti_core.nodes import EpisodeType
        try:
            ref_time = datetime.fromisoformat(new_entries[-1]["ts"])
        except Exception:
            ref_time = datetime.fromisoformat(f"{date_str}T04:00:00+00:00")
        for lens_name, analysis in results:
            if "[analysis failed" in analysis:
                continue
            try:
                await runtime.graphiti.add_episode(
                    name=f"dream_{date_str}_{lens_name}_{already_processed}",
                    episode_body=analysis,
                    source=EpisodeType.text,
                    source_description=f"nightly dream — {lens_name} — {date_str}",
                    reference_time=ref_time,
                    update_communities=True,
                )
                ingested += 1
            except Exception as e:
                ingest_failed += 1
                log.warning("graphiti_episode_failed", lens=lens_name, err=str(e))
        if ingested:
            try:
                await runtime.graphiti.build_communities()  # rebuild thematic clusters
                communities_built = True
            except Exception as e:
                log.warning("graphiti_communities_failed", err=str(e))
        log.info("graphiti_ingest", date=date_str, ingested=ingested, failed=ingest_failed)

    soul_status = await _update_soul(client, date_str, results)

    DREAMS_DIR.mkdir(parents=True, exist_ok=True)
    dream_file.write_text(
        json.dumps(
            {
                "date": date_str,
                "journal_entries": len(entries),   # cumulative — the incremental cursor
                "analyses": {name: text for name, text in results},
                "soul_status": soul_status,
                "dreamed_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )

    narrative = next((t for n, t in results if n == "narrative"), "")
    if not runtime.graphiti:
        graph_status = "not available (check OPENAI_API_KEY + FALKORDB_HOST)"
    elif ingested:
        graph_status = f"{ingested} memories stored"
        if communities_built:
            graph_status += ", communities rebuilt"
        if ingest_failed:
            graph_status += f", {ingest_failed} failed"
    else:
        graph_status = f"nothing stored ({ingest_failed} extraction failures)" if ingest_failed else "no new memories"
    lines = [
        f"Dream complete for {date_str} — {len(new_entries)} new journal entries processed"
        f" ({len(entries)} total for the day).",
        f"Graphiti: {graph_status}",
        f"Soul: {soul_status}",
        "",
        "## Today's narrative",
        narrative,
        "",
        "## Other lenses (truncated)",
    ]
    for name, text in results:
        if name != "narrative":
            lines.append(f"\n**{name}**: {text[:250]}{'...' if len(text) > 250 else ''}")
    lines.append(f"\nFull dream saved to /data/dreams/{date_str}.json")
    return "\n".join(lines)


async def search_memories(query: str, limit: int = 10) -> str:
    """Semantic search across the knowledge graph — facts, entities, communities."""
    if not runtime.graphiti:
        return "[memory search unavailable] Graphiti not initialized. Check OPENAI_API_KEY and FALKORDB_HOST in .env."
    try:
        lines = []
        # Combined hybrid search (edges + entity nodes + communities) first
        try:
            import dataclasses
            from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF
            cfg = dataclasses.replace(COMBINED_HYBRID_SEARCH_RRF, limit=limit)
            results = await runtime.graphiti._search(query, config=cfg)
            for edge in (results.edges or []):
                ts = getattr(edge, "created_at", None)
                date = ts.date() if ts else "?"
                lines.append(f"[fact|{date}] {getattr(edge, 'fact', str(edge))}")
            for node in (results.nodes or []):
                summary = getattr(node, "summary", "")
                if summary:
                    lines.append(f"[entity] {getattr(node, 'name', '?')}: {summary[:200]}")
            for c in (results.communities or []):
                summary = getattr(c, "summary", "")
                if summary:
                    lines.append(f"[community] {summary[:200]}")
            if lines:
                return "\n".join(lines)
        except Exception:
            pass
        # Fallback: basic edge search
        edges = await runtime.graphiti.search(query, num_results=limit)
        if not edges:
            return f"(no memories found for: {query!r})"
        for edge in edges:
            ts = getattr(edge, "created_at", None)
            date = ts.date() if ts else "?"
            lines.append(f"[{date}] {getattr(edge, 'fact', str(edge))}")
        return "\n".join(lines)
    except Exception as e:
        return f"[memory search failed] {e}"


async def retrieve_episodes(last_n: int = 20) -> str:
    """The N most recent episodic events — faster than semantic search for 'what was I just doing'."""
    if not runtime.graphiti:
        return "[memory] Graphiti not initialized."
    try:
        episodes = await runtime.graphiti.retrieve_episodes(
            reference_time=datetime.now(timezone.utc),
            last_n=last_n,
        )
        if not episodes:
            return "(no episodes found)"
        lines = []
        for ep in episodes:
            ts = getattr(ep, "created_at", None)
            date = ts.strftime("%Y-%m-%d %H:%M") if ts else "?"
            body = getattr(ep, "content", getattr(ep, "episode_body", ""))
            lines.append(f"[{date}] {getattr(ep, 'name', '?')}: {str(body)[:300]}")
        return "\n".join(lines)
    except Exception as e:
        return f"[retrieve_episodes failed] {e}"


HANDLERS = {
    "journal_entry":     journal_entry,
    "dream":             dream,
    "search_memories":   search_memories,
    "retrieve_episodes": retrieve_episodes,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "journal_entry",
            "description": "Log a significant event to today's journal. Called throughout the day to record emails, decisions, people met, things learned, discoveries. This is the raw material for dreaming and long-term memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "description": "Category of event: email, decision, task, person, discovery, observation, concern, idea",
                    },
                    "content": {
                        "type": "string",
                        "description": "What happened. Be specific — include names, context, and why it matters.",
                    },
                },
                "required": ["event_type", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dream",
            "description": "Process new journal entries into memories. Runs parallel analyses across 11 lenses (people, decisions, patterns, learnings, etc.), stores results in the knowledge graph, and rewrites the dream-owned sections of soul.md itself. Incremental — already-dreamed entries are skipped, so it's safe to call any time. The dream cron calls it every 4 hours.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_str": {
                        "type": "string",
                        "description": "Date to process in YYYY-MM-DD format. Defaults to the last ~day.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memories",
            "description": "Semantically search your knowledge graph — past experiences, facts learned, people met, decisions made. Returns edges (facts), entity summaries, and community-level summaries. Use before retrying any task you've done before.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for"},
                    "limit": {"type": "integer", "default": 10, "description": "Max results to return"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_episodes",
            "description": "Get the N most recent episodic events from your knowledge graph — what you were doing recently. Faster than search_memories for 'what was I working on'. Use on boot or after a long gap to reconnect with recent context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "last_n": {"type": "integer", "default": 20, "description": "How many recent episodes to retrieve"},
                },
                "required": [],
            },
        },
    },
]
