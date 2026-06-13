"""Gemini Live session setup — native-audio realtime voice.

Model: gemini-3.1-flash-live-preview (real-time voice/vision, native audio out).
The session is opened by bridge.py via `async with connect(...) as session`.
"""
import os

from google import genai
from google.genai import types

from agent.voice.persona import DEFAULT_VOICE

MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")

# A few of the 30 native voices, by character. Warm/grounded ones suit Miles.
VOICES = {
    "Sulafat": "warm",
    "Charon": "informative",
    "Gacrux": "mature",
    "Kore": "firm",
    "Puck": "upbeat",
}


def _gemini_key() -> str:
    """The heso-498805 Gemini API key, from env or the keyring (store_secret)."""
    key = os.getenv("GEMINI_API_KEY", "")
    if key:
        return key
    try:
        import keyring
        from agent.config import KEYRING_SERVICE
        return keyring.get_password(KEYRING_SERVICE, "GEMINI_API_KEY") or ""
    except Exception:
        return ""


def build_client() -> genai.Client:
    api_key = _gemini_key()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY not set. Put the heso-498805 'Gemini API Key' value in the "
            "environment or store_secret('GEMINI_API_KEY', ...) before placing/answering calls."
        )
    return genai.Client(api_key=api_key)


def build_config(system_instruction: str, voice: str = DEFAULT_VOICE) -> types.LiveConnectConfig:
    # READ-ONLY tools only (search_memory, check_calendar) so voice-Miles has live
    # context on a call but can't take any outbound action — no sends, no spend.
    from agent.voice.voice_tools import VOICE_TOOLS
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        system_instruction=system_instruction,
        tools=VOICE_TOOLS,
        # Transcribe both sides so the call can be reported back to text-Miles.
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


def connect(client: genai.Client, config: types.LiveConnectConfig):
    """Return the async context manager for a Live session."""
    return client.aio.live.connect(model=MODEL, config=config)
