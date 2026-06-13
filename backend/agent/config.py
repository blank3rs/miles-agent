"""Paths and constants shared across the agent. No logic here."""
import os
from pathlib import Path

# Defaults derive from this file's location so they work on any machine with no
# hardcoded paths. The container overrides both via docker-compose env.
_REPO_ROOT   = Path(__file__).resolve().parents[2]   # .../ceo-person-careful
SANDBOX_ROOT = Path(os.getenv("SANDBOX_ROOT", str(_REPO_ROOT)))
HESO_ROOT    = Path(os.getenv("HESO_ROOT", str(_REPO_ROOT.parent)))
BACKEND_DIR  = SANDBOX_ROOT / "backend"
# DATA_DIR is overridable so the container can run a single canonical tree (/data)
# instead of the nested /data/backend/data. Locally it defaults to backend/data.
# This is the one tree Miles reads AND writes — it's his library; keep them the same.
DATA_DIR     = Path(os.environ["DATA_DIR"]) if os.getenv("DATA_DIR") else BACKEND_DIR / "data"

SKILLS_DIR      = DATA_DIR / "skills"
PLAYBOOKS_DIR   = DATA_DIR / "playbooks"
CALLS_DIR       = DATA_DIR / "calls"   # per-call briefings written by text-Miles, loaded by voice-Miles
HEARTBEATS_DIR  = DATA_DIR / "heartbeats"
LOGS_DIR        = DATA_DIR / "logs"
JOURNAL_DIR     = DATA_DIR / "journal"
DREAMS_DIR      = DATA_DIR / "dreams"
SESSIONS_DIR    = DATA_DIR / "sessions"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
REPORTS_DIR     = DATA_DIR / "reports"
SOUL_FILE       = DATA_DIR / "soul.md"
TASKS_FILE      = DATA_DIR / "tasks.json"
AGENT_STATE_FILE = DATA_DIR / "agent_state.json"

KEYRING_SERVICE = "heso-ceo-miles"

MODEL          = os.getenv("MODEL", "Kimi-K2.6")
AZURE_ENDPOINT = os.getenv("AZURE_ENDPOINT")
AZURE_API_KEY  = os.getenv("AZURE_API_KEY")
EMAIL_ADDRESS  = os.getenv("EMAIL_ADDRESS", "miles@heso.ca")
AKSHAY_EMAIL   = os.getenv("AKSHAY_EMAIL", "akshay@heso.ca")

# Public wss host Twilio reaches for Media Streams (must be TLS, e.g. "voice.heso.ca").
VOICE_PUBLIC_HOST = os.getenv("VOICE_PUBLIC_HOST", "")
# Shared secret in the wss URL — 443 is world-open, so /voice/stream only opens a
# (paid) Gemini session when the token matches. Our TwiML includes it; nobody else has it.
VOICE_STREAM_TOKEN = os.getenv("VOICE_STREAM_TOKEN", "")
