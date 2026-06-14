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

from agent import runtime, store
from agent.config import AZURE_API_KEY, AZURE_ENDPOINT, CALLS_DIR, MODEL
from agent.voice.gemini_live import build_client, build_config, connect
from agent.voice.persona import build_voice_instruction, is_trusted_caller
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


def _compose_identity() -> str:
    """Voice-Miles's identity, read from the same memory blocks the text loop compiles —
    his durable self plus what his dreams have learned. One brain, two front-ends."""
    blocks = store.all_blocks()
    parts: list[str] = []
    if blocks.get("identity", "").strip():
        parts.append(blocks["identity"].strip())
    for label in store.DREAM_BLOCKS:
        body = blocks.get(label, "").strip()
        if body:
            parts.append(f"## {label.replace('_', ' ').title()}\n{body}")
    return "\n\n".join(parts)


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
    paused = False
    segments: list[dict] = []
    send_lock = asyncio.Lock()  # serialize all sends on the one Live session
    tool_tasks: list[asyncio.Task] = []  # background non-blocking voice-tool runs
    tool_gen = 0  # bumps on barge-in; a tool whose answer is from a stale generation is dropped

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

    # Identity comes from the SAME store the text loop uses — voice-Miles is the same
    # Miles, not a separate persona reading a stale file.
    try:
        soul = _compose_identity()
    except Exception:
        soul = ""

    # Inbound call from Akshay with no prepared briefing → hand voice-Miles a LIVE snapshot
    # of what text-Miles is doing right now, read fresh from the store. Trusted caller only;
    # we never narrate internal work state to someone we don't recognize.
    briefing = _load_briefing(call_id)
    inbound = not to
    if not briefing and inbound and is_trusted_caller(caller):
        try:
            snap = store.live_snapshot()
            if snap:
                briefing = ("This is Akshay calling. Here's exactly what you're in the middle of "
                            "right now, so talk about it like you know your own day:\n\n" + snap)
        except Exception as e:
            log.warning("voice_snapshot_failed", err=str(e))

    # Pause the text loop for the length of an inbound call so it isn't mutating state
    # (sending mail, editing files) under the conversation. Outbound calls Miles places
    # himself don't pause — he dispatched them and keeps working.
    if inbound and runtime.pause_agent:
        runtime.pause_agent("inbound call")
        paused = True

    config = build_config(build_voice_instruction(briefing, soul=soul, caller=caller))
    log.info("voice_call_start", stream_sid=stream_sid, call_id=call_id,
             briefed=bool(briefing), caller=caller, inbound=inbound, soul=bool(soul))

    try:
        async with connect(client, config) as session:
            from agent.voice.voice_tools import handle_voice_tool

            async def _send_audio(pcm: bytes) -> None:
                async with send_lock:
                    await session.send_realtime_input(audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))

            async def _run_voice_tool(fc, spawn_gen: int) -> None:
                """Run a voice tool OFF the receive loop and feed the result back when ready.
                The tools are NON_BLOCKING, so Miles kept talking; WHEN_IDLE folds the answer
                in at his next pause and he continues right where he was — no dead air."""
                args = dict(fc.args or {})
                log.info("voice_tool_call", tool=fc.name, args=args)
                result = await handle_voice_tool(fc.name, args)
                # If the caller barged in (or hung up) while this ran, the question is stale —
                # don't inject an answer to something they've moved past.
                if spawn_gen != tool_gen:
                    log.info("voice_tool_dropped_stale", tool=fc.name)
                    return
                try:
                    async with send_lock:
                        await session.send_tool_response(function_responses=[types.FunctionResponse(
                            id=fc.id, name=fc.name, response=result,
                            scheduling=types.FunctionResponseScheduling.WHEN_IDLE,
                        )])
                except Exception as e:
                    log.warning("voice_tool_response_failed", tool=fc.name, err=str(e))

            try:
                async with send_lock:
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
                    await _send_audio(pcm)
                pending_media.clear()
                while True:
                    msg = json.loads(await twilio_ws.receive_text())
                    ev = msg.get("event")
                    if ev == "media":
                        pcm, up_state = twilio_to_gemini(base64.b64decode(msg["media"]["payload"]), up_state)
                        await _send_audio(pcm)
                    elif ev == "stop":
                        return

            async def gemini_to_twilio_pump():
                nonlocal down_state, tool_gen
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
                            # Don't block the receive loop on the tool — spawn it and keep
                            # streaming audio. The result is fed back when it's ready.
                            for fc in tcall.function_calls:
                                tool_tasks.append(asyncio.create_task(_run_voice_tool(fc, tool_gen)))
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
                            tool_gen += 1  # any in-flight tool answer is now stale
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

            async def _lease_keepalive():
                # A real call can outlast the pause lease (30 min). Re-extend it while the
                # call is live so the TTL only ever fires as a true deadlock backstop, never
                # under an active call (which would let the text loop mutate state mid-call).
                while True:
                    await asyncio.sleep(600)
                    if paused and runtime.pause_agent:
                        runtime.pause_agent("call keepalive")

            up = asyncio.create_task(twilio_to_gemini_pump())
            down = asyncio.create_task(gemini_to_twilio_pump())
            keepalive = asyncio.create_task(_lease_keepalive()) if paused else None

            done, pend = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception():  # a pump that ended by raising — surface it, don't swallow
                    log.warning("voice_pump_error", err=str(t.exception()))
            # Cancel every still-running task AND await them before leaving the `async with`
            # block (which closes the Live session) — otherwise a tool task suspended in
            # send_tool_response could resume and send on a dead session.
            stragglers = [*pend, *tool_tasks] + ([keepalive] if keepalive else [])
            for t in stragglers:
                t.cancel()
            await asyncio.gather(*stragglers, return_exceptions=True)
    except Exception as e:
        log.warning("voice_bridge_error", err=str(e))
    finally:
        # Always release the text loop, even if the call blew up — the lease TTL is only a
        # backstop, not the normal path.
        if paused and runtime.resume_agent:
            runtime.resume_agent()
        log.info("voice_call_end", stream_sid=stream_sid, call_id=call_id, segments=len(segments))
        # Summarize + deliver off the event loop so a slow LLM call can't block teardown.
        await asyncio.to_thread(_deliver_outcome, call_id, to, purpose, segments)
        try:
            await twilio_ws.close()
        except Exception:
            pass
