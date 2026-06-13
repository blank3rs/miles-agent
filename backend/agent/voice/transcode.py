"""Audio transcoding between Twilio (μ-law 8kHz) and Gemini Live (PCM16).

Twilio Media Streams:  μ-law (G.711), 8000 Hz, mono, base64.
Gemini Live input:     PCM signed 16-bit LE, 16000 Hz, mono.
Gemini Live output:    PCM signed 16-bit LE, 24000 Hz, mono.

audioop.ratecv is stateful — it carries fractional-sample state across chunks,
so each direction keeps its own state object (start at None) for clean resampling.
"""
import audioop

_WIDTH = 2  # 16-bit samples
_CHANNELS = 1


def twilio_to_gemini(mulaw_8k: bytes, state):
    """μ-law 8kHz (from Twilio) → PCM16 16kHz (to Gemini). Returns (pcm, new_state)."""
    pcm_8k = audioop.ulaw2lin(mulaw_8k, _WIDTH)
    pcm_16k, state = audioop.ratecv(pcm_8k, _WIDTH, _CHANNELS, 8000, 16000, state)
    return pcm_16k, state


def gemini_to_twilio(pcm_24k: bytes, state):
    """PCM16 24kHz (from Gemini) → μ-law 8kHz (to Twilio). Returns (mulaw, new_state)."""
    pcm_8k, state = audioop.ratecv(pcm_24k, _WIDTH, _CHANNELS, 24000, 8000, state)
    mulaw_8k = audioop.lin2ulaw(pcm_8k, _WIDTH)
    return mulaw_8k, state
