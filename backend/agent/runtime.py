"""Shared mutable runtime references, set by server.py at startup.

Tools read these at call time (never at import time), so server can inject
them after the event loop and external services are up.
"""
from typing import Any, Callable

scheduler: Any = None              # HeartbeatScheduler
graphiti: Any = None               # Graphiti client, or None if unavailable
enqueue_task: Callable | None = None  # thread-safe enqueue onto the agent queue (set by server)

# An inbound call pauses text-Miles while he's on the phone, so the text loop isn't
# mutating shared state (sending mail, editing files) under the conversation. The voice
# bridge holds the lease for the life of the call; the consumer waits on it between turns.
pause_agent: Callable | None = None    # (reason: str) -> None  — acquire the lease
resume_agent: Callable | None = None   # () -> None             — release the lease
