"""Tools the LIVE voice agent can call mid-call, so it isn't flying blind on a static
briefing. Read-only and fast — search memory, check the calendar. No external actions
(emails, money, commitments) happen on a call; those route back to text-Miles.

Gemini Live emits a tool_call; bridge.py runs handle_voice_tool() and sends the result
back with session.send_tool_response(), and the model keeps talking with that context.
"""
import structlog
from google.genai import types

log = structlog.get_logger()

VOICE_TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="search_memory",
                description=(
                    "Search your own memory — people you know, past conversations, facts, decisions, "
                    "what happened with a contact or company, lessons learned. Use this whenever the caller "
                    "references something and you need the details instead of guessing. It's your own knowledge, "
                    "so look things up freely; just use judgment about what you say out loud to whom."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description="What to look up, e.g. 'Rifa Gowani Zima' or 'cold email results' or 'HESO pricing'",
                        )
                    },
                    required=["query"],
                ),
            ),
            types.FunctionDeclaration(
                name="check_calendar",
                description="Look at your upcoming calendar — meetings, calls, and availability over the next few days.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "days_ahead": types.Schema(
                            type=types.Type.INTEGER,
                            description="How many days ahead to look (default 7)",
                        )
                    },
                ),
            ),
        ]
    )
]


async def handle_voice_tool(name: str, args: dict) -> dict:
    """Run a voice tool and return a JSON-able payload for send_tool_response."""
    try:
        if name == "search_memory":
            from agent.tools.memory import search_memories
            res = await search_memories(args.get("query", ""), limit=5)
            return {"result": str(res)[:2000]}
        if name == "check_calendar":
            from agent.tools.calendar_tools import list_calendar_events
            res = await list_calendar_events(days_ahead=int(args.get("days_ahead", 7) or 7))
            return {"result": str(res)[:2000]}
        return {"result": f"(no such tool: {name})"}
    except Exception as e:
        log.warning("voice_tool_failed", tool=name, err=str(e))
        return {"result": f"(couldn't look that up right now: {e})"}
