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

from agent import runtime, store
from agent.config import DREAMS_DIR, WORKER_MODEL
from agent.llm import llm_create

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

# Dream-owned memory blocks, by the markdown header the model emits ↔ the store block label.
_SECTION_TO_BLOCK = {
    "## What I'm learning": "learning",
    "## People I know": "people",
    "## Things that matter right now": "matters_now",
}
_SOUL_SECTIONS = tuple(_SECTION_TO_BLOCK.keys())

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


async def journal_entry(event_type: str, content: str) -> str:
    """Log an event as an episode in the store — the raw material the dream consolidates."""
    try:
        store.add_episode(event_type, content)
        return f"Logged [{event_type}]"
    except Exception as e:
        return f"[journal error] {e}"


def _current_sections() -> str:
    """The dream-owned blocks as they stand now, formatted for the update prompt."""
    parts = []
    for header, label in _SECTION_TO_BLOCK.items():
        body = store.get_block(label).strip() or "(empty)"
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


async def _update_blocks(date_str: str, results: list[tuple[str, str]]) -> tuple[str, bool]:
    """Refine the dream-owned memory blocks in the store from today's analyses (Letta
    pattern: the sleep agent has write authority over memory; the waking agent only reads).
    Append-and-refine, never a fresh rewrite, so hard-won specifics don't erode.

    Returns (status, wrote) — wrote is True only if a block was actually set, so the caller
    knows whether consolidation truly landed before retiring the episodes."""
    try:
        analyses = "\n\n".join(
            f"### {name}\n{text}" for name, text in results if "[analysis failed" not in text
        )
        if not analyses.strip():
            return "blocks unchanged — no analyses", False
        prompt = _SOUL_PROMPT.format(current=_current_sections(), analyses=analyses, date=date_str)
        resp = await llm_create(
            model=WORKER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
            temperature=0.3,
        )
        sections = _parse_sections(resp.choices[0].message.content or "")
        if not sections:
            return "blocks unchanged — could not parse model output", False
        written = []
        for header, body in sections.items():
            label = _SECTION_TO_BLOCK.get(header)
            if label and body:
                store.set_block(label, body)
                written.append(label)
        return f"blocks updated ({', '.join(written) or 'none'})", bool(written)
    except Exception as e:
        log.warning("block_update_failed", err=str(e))
        return f"block update failed: {e}", False


async def dream(date_str: str = "") -> str:
    """Consolidate new episodes: parallel lens analyses → Graphiti index → refine the
    dream-owned memory blocks → mark the episodes consolidated. Reads the store's
    unconsolidated episodes, so it's incremental by construction and safe to call any
    time — the idle consolidator calls it, and it's also a manual tool."""
    episodes = store.unconsolidated_episodes(limit=300)
    if len(episodes) < 2:
        return f"(only {len(episodes)} new episode — skipping dream)"

    if not date_str:
        date_str = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%d")

    episode_text = "\n\n".join(
        f"[{e['created_at']} | {e['kind']}]\n{e['content']}" for e in episodes
    )

    async def _analyze(lens_name: str, lens_prompt: str) -> tuple[str, str]:
        try:
            resp = await llm_create(
                model=WORKER_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"You are Miles Kuncet's reflective mind. Analyze these recent events "
                            f"through exactly this lens: {lens_prompt}\n"
                            f"Be specific, concrete, first person. Date: {date_str}"
                        ),
                    },
                    {"role": "user", "content": f"Recent events:\n\n{episode_text}"},
                ],
                max_tokens=600,
                temperature=0.3,
            )
            msg = resp.choices[0].message
            # DeepSeek (a reasoning model) can return content=None with text in
            # reasoning_content — fall back to it so a lens never crashes on .strip().
            content = (msg.content or getattr(msg, "reasoning_content", "") or "").strip()
            return lens_name, content or "[analysis empty]"
        except Exception as e:
            return lens_name, f"[analysis failed: {e}]"

    results: list[tuple[str, str]] = list(
        await asyncio.gather(*[_analyze(name, prompt) for name, prompt in _DREAM_LENSES])
    )

    # Ingest the analyses into Graphiti — the recall INDEX, not the source of truth. Per-
    # episode try/except so one failed extraction can't abort the batch; counts are logged.
    communities_built = False
    ingested = 0
    ingest_failed = 0
    if runtime.graphiti:
        from graphiti_core.nodes import EpisodeType
        try:
            ref_time = datetime.fromisoformat(episodes[-1]["created_at"])
        except Exception:
            ref_time = datetime.now(timezone.utc)
        last_id = episodes[-1]["id"]
        for lens_name, analysis in results:
            if "[analysis failed" in analysis:
                continue
            try:
                await runtime.graphiti.add_episode(
                    name=f"dream_{date_str}_{lens_name}_{last_id}",
                    episode_body=analysis,
                    source=EpisodeType.text,
                    source_description=f"dream — {lens_name} — {date_str}",
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
        log.info("graphiti_ingest", ingested=ingested, failed=ingest_failed)

    block_status, blocks_written = await _update_blocks(date_str, results)

    # Only retire the episodes if consolidation actually LANDED somewhere — a block write
    # or a graph ingest. If both failed (LLM/parse error AND Graphiti down or every lens
    # errored), leave them unconsolidated so the next dream retries instead of silently
    # dropping the day's memory.
    if blocks_written or ingested > 0:
        store.mark_consolidated([e["id"] for e in episodes])
    else:
        log.warning("dream_landed_nowhere", episodes=len(episodes),
                    note="left unconsolidated for retry — neither blocks nor graph took the update")

    # Diagnostic dream log — a human-readable artifact, not authoritative state.
    try:
        DREAMS_DIR.mkdir(parents=True, exist_ok=True)
        (DREAMS_DIR / f"{date_str}.json").write_text(json.dumps({
            "date": date_str,
            "episodes": len(episodes),
            "analyses": {name: text for name, text in results},
            "block_status": block_status,
            "dreamed_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
    except Exception as e:
        log.warning("dream_log_failed", err=str(e))

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
        f"Dream complete — {len(episodes)} episodes consolidated.",
        f"Graphiti: {graph_status}",
        f"Blocks: {block_status}",
        "",
        "## Narrative",
        narrative,
        "",
        "## Other lenses (truncated)",
    ]
    for name, text in results:
        if name != "narrative":
            lines.append(f"\n**{name}**: {text[:250]}{'...' if len(text) > 250 else ''}")
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
            "description": "Consolidate new episodes into long-term memory. Runs parallel analyses across 11 lenses (people, decisions, patterns, learnings, etc.), indexes the results in the knowledge graph, and refines your dream-owned memory blocks (what you're learning / people you know / what matters now). Incremental — only un-consolidated episodes are processed — so it's safe to call any time. It also runs automatically whenever you're idle, so you rarely need to call it by hand.",
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
