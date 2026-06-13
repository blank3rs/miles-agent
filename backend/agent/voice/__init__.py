"""Voice: Twilio phone calls bridged to Gemini Live native audio.

Twilio Media Streams gives raw μ-law/8kHz audio both ways; Gemini Live wants
PCM16/16kHz in and returns PCM16/24kHz. transcode.py converts between them,
gemini_live.py opens the Live session, and bridge.py pumps audio in both
directions with barge-in. The caller hears Gemini's own native voice — not a
TTS layer — which is what makes it sound human.
"""
