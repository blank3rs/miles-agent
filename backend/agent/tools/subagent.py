"""Real subagent: its own tool loop over a restricted research toolset.

The filesystem-output pattern from Anthropic's harness posts: the subagent writes
its full deliverable to data/reports/ and the main agent gets a short summary plus
the path — deep work without flooding the coordinator's context. Multiple
run_subagent calls in one round execute concurrently (core.py gathers tool calls).
"""
import json
import re
from datetime import datetime, timezone

import structlog

from agent.config import REPORTS_DIR, WORKER_MODEL
from agent.llm import llm_create

log = structlog.get_logger()

_MAX_ROUNDS = 15

# Research-only toolset: the subagent can look things up and read, never act
# externally (email, browser, money) — those stay with the main agent.
_TOOL_NAMES = ("search_web", "exa_search", "scrape_url", "read_pdf", "read_file", "read_heso_file")

_WRITE_REPORT_DEF = {
    "type": "function",
    "function": {
        "name": "write_report",
        "description": "Write your full deliverable to a report file. Call this once, when your work is complete, with everything you found or produced. After it succeeds, reply with a short summary.",
        "parameters": {
            "type": "object",
            "properties": {
                "title":   {"type": "string", "description": "Short title for the report — becomes the filename"},
                "content": {"type": "string", "description": "The complete deliverable in markdown"},
            },
            "required": ["title", "content"],
        },
    },
}

_SYSTEM_PROMPT = """You are a focused execution agent working for Miles Kuncet, CMO of HESO. You have research tools: web search, page scraping, PDF and file reading.

Complete the assigned task thoroughly:
- Search and read until you have what the task needs. Don't stop at the first result.
- When your work is complete, call write_report(title, content) ONCE with the full deliverable — everything you found, structured in markdown, specific and actionable.
- After write_report succeeds, reply with a 3-6 sentence summary of your key findings or output. That summary is all the coordinator sees, so put the decisions and surprises in it, not just 'report written'."""


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "report"


def _research_tools() -> tuple[dict, list]:
    """Assemble the restricted toolset from the sibling modules' registries."""
    from agent.tools import files, web

    handlers: dict = {}
    definitions: list = []
    for module in (web, files):
        for name, fn in module.HANDLERS.items():
            if name in _TOOL_NAMES:
                handlers[name] = fn
    for module in (web, files):
        definitions.extend(d for d in module.DEFINITIONS if d["function"]["name"] in _TOOL_NAMES)
    return handlers, definitions


async def run_subagent(task: str, context: str = "", output_format: str = "") -> str:
    """Dispatch a research sub-agent to run in the background; return immediately with a task id.
    Research sub-agents run concurrently (no browser), so you can fire several at once. Each
    result comes back to Miles as a new turn when done."""
    from agent.tools.dispatch import dispatch
    tid = dispatch("research", task, _run_subagent_impl(task, context, output_format))
    return (
        f'Dispatched research sub-agent [{tid}] — "{task[:70]}". Running in the background; the full '
        "report comes back to you as a new turn when it's done. Fire more if you have independent research "
        "to run in parallel, and keep working in the meantime. check_tasks() for status."
    )


async def _run_subagent_impl(task: str, context: str = "", output_format: str = "") -> str:
    try:
        handlers, definitions = _research_tools()
        definitions = definitions + [_WRITE_REPORT_DEF]

        report_paths: list[str] = []

        async def write_report(title: str, content: str) -> str:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            path = REPORTS_DIR / f"{_slug(title)}_{ts}.md"
            path.write_text(content)
            report_paths.append(str(path))
            return f"Report written: {path} ({len(content):,} chars)"

        handlers = {**handlers, "write_report": write_report}

        system = _SYSTEM_PROMPT
        if output_format:
            system += f"\n\nReport format requested by the coordinator:\n{output_format}"

        messages: list[dict] = [{"role": "system", "content": system}]
        if context:
            messages.append({"role": "user", "content": f"Context:\n{context}"})
        messages.append({"role": "user", "content": f"Task:\n{task}"})

        summary = ""

        for round_num in range(_MAX_ROUNDS):
            resp = await llm_create(
                model=WORKER_MODEL,
                messages=messages,
                tools=definitions,
                tool_choice="auto",
                max_tokens=16384,
            )
            msg = resp.choices[0].message

            if not msg.tool_calls:
                summary = msg.content or ""
                break

            assistant_msg: dict = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            }
            rc = getattr(msg, "reasoning_content", None)
            if rc:
                assistant_msg["reasoning_content"] = rc
            messages.append(assistant_msg)

            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    params = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    params = {}
                handler = handlers.get(name)
                if handler:
                    try:
                        result = await handler(**params)
                    except TypeError as e:
                        result = f"[bad arguments for {name}] {e}"
                else:
                    result = f"[unknown tool: {name}] Available: {sorted(handlers)}"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})

            if round_num == _MAX_ROUNDS - 2:
                messages.append({
                    "role": "user",
                    "content": "[system] You're nearly out of tool rounds. Call write_report now with what you have, then summarize.",
                })

        if not summary:
            summary = "(subagent hit the round limit without a final summary)"

        if report_paths:
            paths = "\n".join(f"Full report: {p}" for p in report_paths)
            return f"{summary}\n\n{paths}\nRead it with read_file if you need the detail."

        # No report written — persist the summary itself so nothing is lost
        if len(summary) > 400:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            path = REPORTS_DIR / f"{_slug(task[:50])}_{ts}.md"
            path.write_text(summary)
            return f"{summary[:1500]}\n\nFull output: {path}"
        return summary

    except Exception as e:
        log.warning("subagent_failed", err=str(e))
        # A sustained rate-limit (429 after llm_create's retries) means nothing was
        # produced — flag it as retryable so Miles re-dispatches rather than acting
        # on an "error string" as if it were a finished report.
        if "429" in str(e) or "RateLimitReached" in str(e):
            return (f"[subagent could not run — endpoint was rate-limited, no report produced. "
                    f"Safe to re-dispatch this research in a few minutes.] {e}")
        return f"[subagent failed] {e}"


HANDLERS = {
    "run_subagent": run_subagent,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "run_subagent",
            "description": "Dispatch a real research sub-agent (its own research tools — web search, scraping, PDF/file reading — and its own context window) to run in the BACKGROUND. ASYNC: it returns a task id immediately and the full report comes back to you as a new turn when done, so you never wait. Research sub-agents run CONCURRENTLY (no browser), so fire several at once for independent chunks: deep research, multi-page scraping, long drafting, analysis. Each writes its report to /data/reports/. Keep working while they run; check_tasks() shows status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task":          {"type": "string", "description": "The task to complete. Be specific about what the report should contain."},
                    "context":       {"type": "string", "description": "Background the subagent needs — it knows nothing about your conversation"},
                    "output_format": {"type": "string", "description": "Optional structure for the report, e.g. 'table of name | role | email | source'"},
                },
                "required": ["task"],
            },
        },
    },
]
