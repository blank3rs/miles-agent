"""make_call: text-Miles places a phone call that voice-Miles answers as himself.

Text-Miles writes a full briefing (who, why, history, goal, what to say/avoid);
it's saved to CALLS_DIR/<call_id>.md and the call carries only the call_id. The
voice bridge loads that briefing into Gemini Live's system instruction, so the
agent on the line *is* this Miles for this call. The briefing has no size limit
(it's a file, not a Twilio parameter) — make it as rich as the call deserves.
"""
import asyncio
import uuid

from agent.config import CALLS_DIR, VOICE_PUBLIC_HOST, VOICE_STREAM_TOKEN
from agent.tools.verification import get_twilio_creds


async def make_call(to: str, purpose: str, briefing: str = "") -> str:
    if not VOICE_PUBLIC_HOST:
        return ("[make_call] VOICE_PUBLIC_HOST not set — Twilio needs a public wss (TLS) "
                "endpoint to stream call audio. Set it in .env once voice is deployed behind TLS.")

    try:
        sid, token, from_number = await asyncio.to_thread(get_twilio_creds)
    except Exception as e:
        return f"[make_call] could not read Twilio credentials: {e}"
    if not (sid and token and from_number):
        return ("[make_call] Twilio not set up. Need twilio_account_sid, twilio_auth_token, "
                "and twilio_phone_number in the keyring first.")

    call_id = uuid.uuid4().hex[:12]
    full_briefing = f"# Call purpose\n{purpose.strip()}\n\n{briefing.strip()}".strip()
    try:
        CALLS_DIR.mkdir(parents=True, exist_ok=True)
        (CALLS_DIR / f"{call_id}.md").write_text(full_briefing)
    except Exception as e:
        return f"[make_call] could not save briefing: {e}"

    def _place():
        from twilio.rest import Client
        from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

        resp = VoiceResponse()
        connect = Connect()
        stream = Stream(url=f"wss://{VOICE_PUBLIC_HOST}/voice/stream")
        stream.parameter(name="call_id", value=call_id)
        stream.parameter(name="to", value=to)
        stream.parameter(name="caller", value=to)  # the other party, so Miles recognizes known numbers
        stream.parameter(name="purpose", value=purpose[:300])
        if VOICE_STREAM_TOKEN:
            stream.parameter(name="token", value=VOICE_STREAM_TOKEN)
        connect.append(stream)
        resp.append(connect)

        client = Client(sid, token)
        call = client.calls.create(to=to, from_=from_number, twiml=str(resp))
        return call.sid

    try:
        twilio_call_sid = await asyncio.to_thread(_place)
    except Exception as e:
        return f"[make_call failed] {e}"

    return (
        f"Calling {to} now as Miles (call {twilio_call_sid}, briefing {call_id}). "
        f"Voice-Miles is briefed and on the line. When the call ends, the transcript and outcome "
        f"come back to you automatically as a new turn — you don't need to poll or set a heartbeat "
        f"for it. Keep working; act on it when it arrives."
    )


HANDLERS = {
    "make_call": make_call,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "make_call",
            "description": (
                "Place a phone call that you (Miles) answer in your own voice via Gemini Live. "
                "You hand off a full briefing and the voice agent becomes you for that call — it "
                "speaks as Miles, references the briefing, and sounds human. Write the briefing like "
                "you're prepping yourself: who you're calling and their role, the relationship and any "
                "history, the exact goal of THIS call, the 3-5 facts you'll need, what to offer, what to "
                "avoid or not commit to, and how you want it to end. The richer the briefing, the better "
                "the call — there's no size limit. Use for outreach, follow-ups, scheduling, quick checks. "
                "Anything sensitive (money, legal, credentials) still routes to Akshay, not said on a call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to":       {"type": "string", "description": "Phone number to call, E.164 format e.g. +14155551234"},
                    "purpose":  {"type": "string", "description": "One line: the goal of this call"},
                    "briefing": {"type": "string", "description": "The full context the voice agent should go in with — who, history, goal, key facts, what to say/avoid, desired outcome"},
                },
                "required": ["to", "purpose"],
            },
        },
    },
]
