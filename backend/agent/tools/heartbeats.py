"""Self-scheduling: heartbeats wake the agent at a future time with instructions."""
import json
import time
import uuid
from datetime import datetime, timezone

from agent import runtime
from agent.config import HEARTBEATS_DIR


async def set_heartbeat(seconds: int, reason: str, context: str) -> str:
    try:
        hb_id = str(uuid.uuid4())[:8]
        fire_at = time.time() + int(seconds)
        hb = {
            "id": hb_id,
            "reason": reason,
            "context": context,
            "fire_at": fire_at,
            "fire_at_iso": datetime.fromtimestamp(fire_at, tz=timezone.utc).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        HEARTBEATS_DIR.mkdir(parents=True, exist_ok=True)
        (HEARTBEATS_DIR / f"{hb_id}.json").write_text(json.dumps(hb, indent=2))
        if runtime.scheduler:
            runtime.scheduler.add_heartbeat(hb)
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        human = f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")
        return f"Heartbeat set: [{hb_id}] fires in {human} — {reason}"
    except Exception as e:
        return f"[error setting heartbeat] {e}"


async def cancel_heartbeat(heartbeat_id: str) -> str:
    try:
        path = HEARTBEATS_DIR / f"{heartbeat_id}.json"
        if not path.exists():
            return f"(no heartbeat with id: {heartbeat_id})"
        path.unlink()
        if runtime.scheduler:
            runtime.scheduler.remove_heartbeat(heartbeat_id)
        return f"Cancelled heartbeat: {heartbeat_id}"
    except Exception as e:
        return f"[error cancelling heartbeat] {e}"


async def list_heartbeats() -> str:
    try:
        if not HEARTBEATS_DIR.exists():
            return "(none scheduled)"
        hbs = []
        for f in sorted(HEARTBEATS_DIR.glob("*.json")):
            try:
                hb = json.loads(f.read_text())
                remaining = max(0, hb["fire_at"] - time.time())
                m, s = divmod(int(remaining), 60)
                h, m = divmod(m, 60)
                human = f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")
                hbs.append(f"[{hb['id']}] {hb['reason']} — in {human} ({hb['fire_at_iso']})")
            except Exception:
                pass
        return "\n".join(hbs) if hbs else "(none scheduled)"
    except Exception as e:
        return f"[error listing heartbeats] {e}"


HANDLERS = {
    "set_heartbeat":    set_heartbeat,
    "cancel_heartbeat": cancel_heartbeat,
    "list_heartbeats":  list_heartbeats,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "set_heartbeat",
            "description": "Schedule yourself to wake up and act autonomously at a future time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "integer", "description": "Seconds from now"},
                    "reason":  {"type": "string"},
                    "context": {"type": "string", "description": "Instructions for when you wake up"},
                },
                "required": ["seconds", "reason", "context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_heartbeat",
            "description": "Cancel a scheduled heartbeat by ID.",
            "parameters": {
                "type": "object",
                "properties": {"heartbeat_id": {"type": "string"}},
                "required": ["heartbeat_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_heartbeats",
            "description": "List all scheduled heartbeats with countdowns.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
