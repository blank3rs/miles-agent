"""Twilio Media Streams <-> Gemini Live audio bridge for one call.

Flow:
  Twilio WS  --μ-law/8k--> transcode --PCM16/16k--> Gemini Live
  Gemini Live --PCM16/24k--> transcode --μ-law/8k--> Twilio WS

The briefing text-Miles wrote is loaded by call_id (carried in the Stream's
customParameters) and becomes the voice agent's system instruction, so the agent
that answers IS this specific Miles. Both sides of the call are transcribed live;
when the call ends the transcript is saved, summarized if long, and pushed back to
text-Miles as a turn so he can journal it and follow up.

Barge-in: when Gemini detects the caller talking over it, we send Twilio a `clear`
to flush buffered speech immediately.
"""
import asyncio
import base64
import json

import structlog

from agent import runtime
from agent.config import AZURE_API_KEY, AZURE_ENDPOINT, CALLS_DIR, MODEL, SOUL_FILE
from agent.voice.gemini_live import build_client, build_config, connect
from agent.voice.persona import build_voice_instruction
from agent.voice.transcode import gemini_to_twilio, twilio_to_gemini

log = structlog.get_logger()

_SUMMARIZE_OVER = 6000  # chars; longer transcripts get summarized for the handoff


def _load_briefing(call_id: str) -> str:
    if not call_id:
        return ""
    try:
        path = CALLS_DIR / f"{call_id}.md"
        if path.exists():
            return path.read_text()
    except Exception as e:
        log.warning("briefing_load_failed", call_id=call_id, err=str(e))
    return ""


def _render_transcript(segments: list[dict]) -> str:
    """Coalesce streamed transcription deltas into a clean dialogue."""
    lines: list[str] = []
    speaker = None
    buf = ""
    label = {"caller": "Them", "miles": "Miles"}
    for seg in segments:
        if seg["speaker"] == speaker:
            buf += seg["text"]
        else:
            if buf.strip():
                lines.append(f"{label[speaker]}: {buf.strip()}")
            speaker = seg["speaker"]
            buf = seg["text"]
    if buf.strip():
        lines.append(f"{label[speaker]}: {buf.strip()}")
    return "\n".join(lines)


def _summarize(transcript: str) -> str:
    """Summarize a long transcript with the main model; passthrough if short."""
    if len(transcript) <= _SUMMARIZE_OVER:
        return transcript
    try:
        from openai import OpenAI
        client = OpenAI(base_url=AZURE_ENDPOINT, api_key=AZURE_API_KEY)
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": (
                    "Summarize this phone call transcript for Miles's records. Keep: what was "
                    "decided, what each side committed to, any numbers/dates/names, open questions, "
                    "and the clear next step. Plain prose, tight. No preamble."
                )},
                {"role": "user", "content": transcript},
            ],
            max_tokens=700,
        )
        return (resp.choices[0].message.content or "").strip() or transcript[:_SUMMARIZE_OVER]
    except Exception as e:
        log.warning("call_summary_failed", err=str(e))
        return transcript[:_SUMMARIZE_OVER] + "\n\n[summary failed — transcript truncated]"


def _deliver_outcome(call_id: str, to: str, purpose: str, segments: list[dict]) -> None:
    """Save the transcript and push the call outcome back to text-Miles."""
    transcript = _render_transcript(segments)
    path_note = ""
    if call_id and transcript:
        try:
            CALLS_DIR.mkdir(parents=True, exist_ok=True)
            p = CALLS_DIR / f"{call_id}.transcript.md"
            p.write_text(f"# Call transcript\nTo: {to or '?'}\nPurpose: {purpose or '?'}\n\n{transcript}\n")
            path_note = f"\n\nFull transcript: backend/data/calls/{call_id}.transcript.md"
        except Exception as e:
            log.warning("transcript_save_failed", err=str(e))

    who = f"to {to}" if to else "(inbound)"
    if not transcript:
        # Inbound silent/spurious lines (health checks, hangups, voicemail) carry no
        # value — don't burn an LLM turn on them. For an OUTBOUND call we initiated,
        # a no-answer IS worth reporting so Miles knows the call didn't land.
        if not to:
            log.info("call_skipped_empty", call_id=call_id)
            return
        body = f"Your call {who} ended with no speech transcribed — no answer, voicemail, or a silent line."
    else:
        body = (
            f"A phone call {who} just ended.\n"
            f"Purpose: {purpose or '(inbound, no briefing)'}\n\n"
            f"What was said:\n{_summarize(transcript)}{path_note}\n\n"
            f"Journal anything worth remembering, update the task ledger, and follow up if needed."
        )

    if runtime.enqueue_task:
        runtime.enqueue_task({"type": "call", "to": to, "call_id": call_id, "content": body})
    else:
        log.warning("call_outcome_undelivered", reason="no enqueue hook", call_id=call_id)


async def run_call_bridge(twilio_ws) -> None:
    """Drive one phone call. twilio_ws is an already-accepted Starlette WebSocket."""
    from google.genai import types

    stream_sid: str | None = None
    call_id = ""
    to = ""
    purpose = ""
    token = ""
    caller = ""
    up_state = None
    down_state = None
    segments: list[dict] = []

    try:
        client = build_client()
    except Exception as e:
        log.warning("voice_no_client", err=str(e))
        await twilio_ws.close()
        return

    pending_media: list[bytes] = []
    try:
        while stream_sid is None:
            msg = json.loads(await twilio_ws.receive_text())
            ev = msg.get("event")
            if ev == "start":
                start = msg["start"]
                stream_sid = start["streamSid"]
                params = start.get("customParameters") or {}
                call_id = params.get("call_id", "")
                to = params.get("to", "")
                purpose = params.get("purpose", "")
                token = params.get("token", "")
                caller = params.get("caller", "")
            elif ev == "media":
                pending_media.append(base64.b64decode(msg["media"]["payload"]))
            elif ev == "stop":
                await twilio_ws.close()
                return
    except Exception as e:
        log.warning("voice_start_failed", err=str(e))
        try:
            await twilio_ws.close()
        except Exception:
            pass
        return

    # Gate the (paid) Gemini session on the shared token, now that we've read the
    # Stream's customParameters. 443 is world-open; only our own TwiML carries this.
    from agent.config import VOICE_STREAM_TOKEN
    if VOICE_STREAM_TOKEN and token != VOICE_STREAM_TOKEN:
        log.warning("voice_stream_rejected", reason="bad token in customParameters")
        await twilio_ws.close()
        return

    briefing = _load_briefing(call_id)
    try:
        soul = SOUL_FILE.read_text() if SOUL_FILE.exists() else ""
    except Exception:
        soul = ""
    config = build_config(build_voice_instruction(briefing, soul=soul, caller=caller))
    log.info("voice_call_start", stream_sid=stream_sid, call_id=call_id,
             briefed=bool(briefing), caller=caller, soul=bool(soul))

    try:
        async with connect(client, config) as session:
            try:
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(
                        text="The call just connected. Open it — greet them in one short, natural line as Miles."
                    )]),
                    turn_complete=True,
                )
            except Exception:
                pass

            async def twilio_to_gemini_pump():
                nonlocal up_state
                for chunk in pending_media:
                    pcm, up_state = twilio_to_gemini(chunk, up_state)
                    await session.send_realtime_input(audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))
                pending_media.clear()
                while True:
                    msg = json.loads(await twilio_ws.receive_text())
                    ev = msg.get("event")
                    if ev == "media":
                        pcm, up_state = twilio_to_gemini(base64.b64decode(msg["media"]["payload"]), up_state)
                        await session.send_realtime_input(audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))
                    elif ev == "stop":
                        return

            async def gemini_to_twilio_pump():
                nonlocal down_state
                # session.receive() ends its generator after each turn completes —
                # re-enter it for the life of the call so the conversation continues
                # past the first turn (otherwise the call drops after the greeting).
                while True:
                    received_any = False
                    async for response in session.receive():
                        received_any = True
                        # Live tool calls (search_memory / check_calendar) — run the
                        # read-only handler and hand the result back so Miles can answer
                        # with real context mid-call instead of guessing.
                        tcall = getattr(response, "tool_call", None)
                        if tcall and tcall.function_calls:
                            from agent.voice.voice_tools import handle_voice_tool
                            fresponses = []
                            for fc in tcall.function_calls:
                                args = dict(fc.args or {})
                                log.info("voice_tool_call", tool=fc.name, args=args)
                                result = await handle_voice_tool(fc.name, args)
                                fresponses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
                            await session.send_tool_response(function_responses=fresponses)
                            continue
                        sc = getattr(response, "server_content", None)
                        if not sc:
                            continue
                        it = getattr(sc, "input_transcription", None)
                        if it and getattr(it, "text", None):
                            segments.append({"speaker": "caller", "text": it.text})
                        ot = getattr(sc, "output_transcription", None)
                        if ot and getattr(ot, "text", None):
                            segments.append({"speaker": "miles", "text": ot.text})
                        if getattr(sc, "interrupted", False):
                            down_state = None
                            await twilio_ws.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))
                            continue
                        turn = getattr(sc, "model_turn", None)
                        if not turn:
                            continue
                        for part in turn.parts:
                            data = getattr(getattr(part, "inline_data", None), "data", None)
                            if data:
                                mulaw, down_state = gemini_to_twilio(data, down_state)
                                await twilio_ws.send_text(json.dumps({
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": base64.b64encode(mulaw).decode()},
                                }))
                    if not received_any:
                        return  # session closed — stop re-entering receive()

            up = asyncio.create_task(twilio_to_gemini_pump())
            down = asyncio.create_task(gemini_to_twilio_pump())
            _, pend = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            for t in pend:
                t.cancel()
    except Exception as e:
        log.warning("voice_bridge_error", err=str(e))
    finally:
        log.info("voice_call_end", stream_sid=stream_sid, call_id=call_id, segments=len(segments))
        # Summarize + deliver off the event loop so a slow LLM call can't block teardown.
        await asyncio.to_thread(_deliver_outcome, call_id, to, purpose, segments)
        try:
            await twilio_ws.close()
        except Exception:
            pass
