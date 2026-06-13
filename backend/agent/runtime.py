"""Shared mutable runtime references, set by server.py at startup.

Tools read these at call time (never at import time), so server can inject
them after the event loop and external services are up.
"""
from typing import Any, Callable

scheduler: Any = None              # HeartbeatScheduler
graphiti: Any = None               # Graphiti client, or None if unavailable
enqueue_task: Callable | None = None  # thread-safe enqueue onto the agent queue (set by server)
