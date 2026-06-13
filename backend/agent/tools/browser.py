"""Browser automation via browser-use.

Miles describes a goal in natural language and a browser-use Agent drives a real
Chromium to completion — navigating, filling forms, logging in, extracting data.
It reads the page's element tree (not guessed CSS selectors), so it self-heals
across layout changes; that replaced the hand-rolled patchright layer that kept
looping on signups.

One persistent Chrome profile (user_data_dir) holds all logins, so a Google
sign-in done once is reused for SSO everywhere. Because two Chromium instances
can't share one profile, browser_task calls are serialized with a lock.

Credentials never reach the driving LLM: known secrets are passed as browser-use
`sensitive_data`, so the model sees a placeholder (x_google_password) while the
real value is typed into the page underneath.
"""
import asyncio
import logging
import os
import shutil

from agent.config import KEYRING_SERVICE, MODEL, SESSIONS_DIR

# Must be set before browser_use is imported anywhere.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("BROWSER_USE_LOGGING_LEVEL", "result")

import structlog

log = structlog.get_logger()

_PROFILE_DIR = SESSIONS_DIR / "browseruse_profile"
_LOCK = asyncio.Lock()  # one shared profile → one browser at a time
_DEFAULT_MAX_STEPS = 30
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# placeholder shown to the LLM → (keyring key, domain glob the value is allowed on)
_CREDENTIALS = [
    ("x_google_password",   "google_password",   "https://*.google.com"),
    ("x_linkedin_password", "linkedin_password", "https://*.linkedin.com"),
]


def _clear_chrome_locks() -> None:
    """Remove stale Chrome singleton locks from the profile.

    browser-use (unlike the old patchright layer) doesn't clean these up. If a
    previous browser_task's Chromium didn't exit cleanly — agent hit max_steps,
    a kill, a container restart mid-run — the SingletonLock symlink survives and
    the next launch hangs 30s waiting on it, then browser-use's start watchdog
    times out. Clearing them before every launch makes browser_task self-heal."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = _PROFILE_DIR / name
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
        except Exception:
            pass


def _load_sensitive_data() -> dict:
    """Build browser-use sensitive_data from keyring secrets, domain-scoped.
    The driving LLM only ever sees the placeholder keys, never the values."""
    try:
        import keyring
    except ImportError:
        return {}
    data: dict = {}
    for placeholder, key, domain in _CREDENTIALS:
        try:
            value = keyring.get_password(KEYRING_SERVICE, key)
        except Exception:
            value = None
        if value:
            data.setdefault(domain, {})[placeholder] = value
    return data


def _available_placeholders(sensitive_data: dict) -> list[str]:
    return sorted(ph for scope in sensitive_data.values() for ph in scope)


async def browser_task(task: str, max_steps: int = _DEFAULT_MAX_STEPS, use_vision: bool = True) -> str:
    """Dispatch a browser task to run in the background; return immediately with a task id.
    Browser tasks serialize on the single shared browser (one at a time); the result comes
    back to Miles as a new turn when done, so he never blocks waiting on it."""
    from agent.tools.dispatch import dispatch
    tid = dispatch("browser", task, _browser_task_impl(task, max_steps, use_vision))
    return (
        f'Dispatched browser task [{tid}] — "{task[:70]}". '
        "The browser runs ONE task at a time, so if another browser task is already going, this one "
        "queues behind it. You'll get the full result back as a new turn when it finishes — keep working "
        "in the meantime, don't wait. check_tasks() for status."
    )


async def _browser_task_impl(task: str, max_steps: int = _DEFAULT_MAX_STEPS, use_vision: bool = True) -> str:
    """Drive a real browser to complete a natural-language task. Returns the result.
    Runs in the background via dispatch(); serialized on _LOCK so only one runs at a time."""
    try:
        from browser_use import Agent, Browser, ChatOpenAI
    except ImportError:
        return "[browser_task] browser-use not installed. Run: install_package('browser-use')"

    logging.getLogger("browser_use").setLevel(logging.WARNING)

    endpoint = os.getenv("AZURE_ENDPOINT")
    api_key = os.getenv("AZURE_API_KEY")
    if not endpoint or not api_key:
        return "[browser_task] AZURE_ENDPOINT / AZURE_API_KEY not set."

    sensitive_data = _load_sensitive_data()
    placeholders = _available_placeholders(sensitive_data)
    cred_note = (
        f"\n\nFor any login, these credential placeholders are available — use them by name, "
        f"do not ask for the real value: {', '.join(placeholders)}."
        if placeholders else ""
    )

    async with _LOCK:
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _clear_chrome_locks()  # self-heal from a prior run's unclean exit
        browser = None
        try:
            llm = ChatOpenAI(model=MODEL, base_url=endpoint, api_key=api_key)
            # Headless: it launches reliably in-container (headed on Xvfb hangs past
            # browser-use's 30s start watchdog). Hardened against trivial bot
            # detection with a real UA + the AutomationControlled flag off. This is
            # enough for normal sites; the hardest cold-SSO walls (Google) still
            # block headless automation — those signups go through a human.
            browser = Browser(
                headless=True,
                user_data_dir=str(_PROFILE_DIR),
                chromium_sandbox=False,            # runs as root in Docker — sandbox would crash
                user_agent=_USER_AGENT,
                args=[
                    "--disable-dev-shm-usage",                    # avoid /dev/shm exhaustion in containers
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            agent = Agent(
                task=task + cred_note,
                llm=llm,
                browser=browser,
                use_vision=use_vision,
                sensitive_data=sensitive_data or None,
            )
            # Hard ceiling so a wedged browser-use call (hung watchdog, stuck network)
            # can't hold the single-browser lock forever and starve every queued task.
            timeout = min(max_steps * 25, 900)
            history = await asyncio.wait_for(agent.run(max_steps=max_steps), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("browser_task_timeout", max_steps=max_steps)
            return (f"[browser_task timed out after {min(max_steps * 25, 900)}s] The browser was released "
                    "so other tasks can run. Re-dispatch with a sharper, smaller goal if it still matters.")
        except Exception as e:
            log.warning("browser_task_failed", err=str(e))
            return f"[browser_task failed] {e}"
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass

    try:
        result = history.final_result() or ""
        done = history.is_done()
        success = history.is_successful()  # True / False / None (unknown)
        errors = [e for e in (history.errors() or []) if e]
        urls = [u for u in (history.urls() or []) if u]
    except Exception as e:
        return f"[browser_task] ran but could not read result: {e}"

    status = "completed" if done and success is not False else ("ended" if done else "did not finish")
    lines = [f"Browser task {status} in ≤{max_steps} steps."]
    if result:
        lines.append(f"\nResult:\n{result[:4000]}")
    if urls:
        lines.append(f"\nLast page: {urls[-1]}")
    if errors:
        lines.append(f"\nErrors along the way ({len(errors)}): " + " | ".join(str(e)[:200] for e in errors[-3:]))
    if not done:
        lines.append("\nNot finished — call browser_task again with a more specific next step, "
                     "or check for a verification step (email/SMS) that's blocking it.")
    return "\n".join(lines)


async def reset_browser_profile() -> str:
    """Wipe the saved browser profile (all logins) to recover from a corrupted/locked session."""
    async with _LOCK:
        try:
            if _PROFILE_DIR.exists():
                shutil.rmtree(_PROFILE_DIR, ignore_errors=True)
            return "Browser profile cleared. Next browser_task starts logged out — Google SSO will re-authenticate."
        except Exception as e:
            return f"[reset_browser_profile failed] {e}"


HANDLERS = {
    "browser_task":          browser_task,
    "reset_browser_profile": reset_browser_profile,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "browser_task",
            "description": (
                "Do something in a real web browser by describing the goal in plain language. A browser agent "
                "drives Chromium to completion — navigating, clicking, filling forms, logging in, signing up, "
                "extracting data. It reads the page itself, so you don't pass selectors; just say what you want "
                "('Sign up for Twilio using my Google account miles@heso.ca and get a US phone number', or "
                "'Go to crunchbase.com and pull the funding history for Acme Inc'). "
                "Logins persist in one saved profile, so Google SSO done once is reused everywhere. "
                "ASYNC: this returns a task id IMMEDIATELY and runs in the background — the full result comes back "
                "to you as a NEW TURN when it's done. Don't wait on it; fire it and keep working on other things "
                "(email, research, the ledger). Only ONE browser task runs at a time (single shared browser), so "
                "additional browser tasks queue behind the current one. check_tasks() shows what's in flight. "
                "If a task stops on email/SMS verification, its result will say so — handle that step (read_sms / "
                "inbox), then dispatch a follow-up to continue. Be specific and include any URLs, names, or values. "
                "IF IT FAILS or stops short of the goal, dispatch it AGAIN — a sharper task and a higher max_steps "
                "for fiddly multi-step UI (LinkedIn edits, dashboards). You own the retry loop: keep refining and "
                "retrying important tasks (often 2-3 sharper attempts), and only escalate to Akshay if genuinely stuck."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task":       {"type": "string", "description": "The goal, in natural language. Include the site, the exact data/fields, and the finish condition."},
                    "max_steps":  {"type": "integer", "default": 30, "description": "Max browser steps before it stops and reports back. Raise for long multi-page flows."},
                    "use_vision": {"type": "boolean", "default": True, "description": "Let the agent see screenshots (more reliable on visual/canvas pages). Turn off to cut cost on simple text flows."},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_browser_profile",
            "description": "Wipe the saved browser profile and all its logins. Use only to recover from a wedged or corrupted session — afterward the next browser_task starts logged out and re-authenticates.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
