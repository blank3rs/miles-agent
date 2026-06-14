"""Tools the LIVE voice agent can call mid-call, so it isn't flying blind on a static
briefing. Read-only and safe — search his own memory, check the calendar, search the web.
No external actions (emails, money, commitments) happen on a call; those route back to
text-Miles.

Every tool is declared NON_BLOCKING: when Miles calls one, he keeps talking instead of
going silent while it runs. bridge.py runs the tool in the background and feeds the result
back with scheduling=WHEN_IDLE, so it folds into the conversation when he next pauses and he
continues right where he left off. This is what makes a mid-call web search feel natural
instead of dead air.
"""
import structlog
from google.genai import types

log = structlog.get_logger()

# Tools that may take a beat (web search, memory over the graph). The model is told via
# NON_BLOCKING that it can keep the conversation going while these run.
_NB = types.Behavior.NON_BLOCKING

VOICE_TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="search_memory",
                behavior=_NB,
                description=(
                    "Search your own memory — people you know, past conversations, facts, decisions, "
                    "what happened with a contact or company, lessons learned. Use this whenever the caller "
                    "references something and you need the details instead of guessing. It's your own knowledge, "
                    "so look things up freely; just use judgment about what you say out loud to whom. "
                    "Keep talking while it loads — a quick 'let me pull that up' is natural."
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
                behavior=_NB,
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
            types.FunctionDeclaration(
                name="search_web",
                behavior=_NB,
                description=(
                    "Search the web for current information — a company, a person, news, a fact you're "
                    "unsure of. Use it mid-conversation when something comes up you should know but don't. "
                    "It takes a few seconds, so say a quick 'hang on, let me check' and keep the conversation "
                    "going; the answer comes back to you and you pick up from there. Read-only — you're just looking."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description="The web search query",
                        )
                    },
                    required=["query"],
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
        if name == "search_web":
            from agent.tools.web import search_web
            res = await search_web(args.get("query", ""), max_results=5)
            return {"result": str(res)[:2000]}
        return {"result": f"(no such tool: {name})"}
    except Exception as e:
        log.warning("voice_tool_failed", tool=name, err=str(e))
        return {"result": f"(couldn't look that up right now: {e})"}
