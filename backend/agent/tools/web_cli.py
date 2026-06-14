"""web_cli: turn ANY website into a CLI for yourself, via OpenCLI in CDP mode.

OpenCLI's normal path reuses a desktop Chrome through a browser extension — which doesn't
exist on a server. So we run it in CDP mode (its documented escape hatch): we keep a real,
HEADFUL Chromium listening on a debug port, point OpenCLI at it with OPENCLI_CDP_ENDPOINT,
and Miles drives `opencli ...` over it.

Headful, not headless, on purpose. A headless browser advertises itself — the
`HeadlessChrome` user-agent, `navigator.webdriver`, missing-renderer quirks — and that's
exactly what anti-bot systems flag. We launch a real rendered browser drawing to the
container's Xvfb virtual display (DISPLAY=:99, started by the Dockerfile CMD), so to a site
it looks like a person's browser. On macOS it draws to the native window server. On a Linux
box with no display at all we fall back to headless rather than hard-fail.

The flow Miles uses (one tool, he composes the subcommands):
  recon a site once:   web_cli("browser recon analyze https://news.ycombinator.com")
  crystallize/verify:  web_cli("browser recon init hn/top"), then write + verify the adapter
  run a command:       web_cli("hn top --limit 5 --format json")
Crystallized adapters live under /data/opencli_home/.opencli/, so they survive restarts and
build up into a library — the website-side twin of create_skill.

Every call is gated and receipted (agent.audit): reads are autonomous; LinkedIn-style
write/social actions are blocked here (server automation is the top ban trigger) and routed
to Akshay.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import sys
from glob import glob
from pathlib import Path

import structlog

from agent import audit
from agent.config import DATA_DIR, SESSIONS_DIR

log = structlog.get_logger()

# Headful by default — a real rendered browser is far less detectable than headless.
# Toggle with WEB_CLI_HEADFUL=0 if you ever need headless.
_HEADFUL = os.getenv("WEB_CLI_HEADFUL", "1").lower() not in ("0", "false", "no")
_CDP_PORT = int(os.getenv("OPENCLI_CDP_PORT", "9223"))
# Per-site/day cap — a runaway/loop and bot-pattern guard. Generous; tighten via env.
_MAX_PER_SITE_DAY = int(os.getenv("WEB_CLI_MAX_PER_SITE_DAY", "150"))
_CDP_ENDPOINT = f"http://127.0.0.1:{_CDP_PORT}"
# A profile of its own so it never fights browser_task over the single browser-use profile
# (Chrome locks a user-data-dir to one process). Logins happen once via OpenCLI itself.
_PROFILE_DIR = SESSIONS_DIR / "opencli_profile"
# OpenCLI keeps adapters under ~/.opencli — point HOME at /data so they persist on the volume.
_OPENCLI_HOME = DATA_DIR / "opencli_home"

_chrome_proc: asyncio.subprocess.Process | None = None
_launch_lock = asyncio.Lock()  # so two concurrent web_cli calls don't both launch Chrome


def _find_chromium() -> str | None:
    if os.getenv("CHROMIUM_PATH"):
        return os.getenv("CHROMIUM_PATH")
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    # Playwright's bundled chromium (browser_task installs this).
    for base in (Path.home() / ".cache/ms-playwright", Path("/root/.cache/ms-playwright")):
        hits = sorted(glob(str(base / "chromium-*/chrome-linux/chrome")))
        if hits:
            return hits[-1]
    return None


async def _cdp_alive() -> bool:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2) as c:
            r = await c.get(f"{_CDP_ENDPOINT}/json/version")
            return r.status_code == 200
    except Exception:
        return False


async def _ensure_cdp_chrome() -> str | None:
    """Make sure a real (headful) Chromium is listening on the CDP port. Returns an error
    string if it can't be started, else None."""
    global _chrome_proc
    if await _cdp_alive():
        return None
    async with _launch_lock:
        # Re-check inside the lock — another call may have brought it up while we waited.
        if await _cdp_alive():
            return None
        return await _launch_chrome()


async def _launch_chrome() -> str | None:
    global _chrome_proc
    binary = _find_chromium()
    if not binary:
        return ("[web_cli] No Chromium found. Set CHROMIUM_PATH, or run "
                "`playwright install chromium`.")
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    headful = _HEADFUL
    # Headful needs somewhere to draw. The container has Xvfb on :99 (DISPLAY set by the
    # Dockerfile); macOS uses its native window server (no DISPLAY var needed). Only on a
    # Linux box with no display at all do we fall back to headless, so we never hard-fail.
    if headful and sys.platform.startswith("linux") and not env.get("DISPLAY"):
        log.warning("web_cli_no_display", note="no DISPLAY on linux — falling back to headless")
        headful = False

    args = [
        binary,
        f"--remote-debugging-port={_CDP_PORT}",
        f"--user-data-dir={_PROFILE_DIR}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",            # required running as root in the container
        "--disable-dev-shm-usage", # avoid /dev/shm exhaustion in containers
        # Look like a person's browser, not an automation surface:
        "--disable-blink-features=AutomationControlled",  # drops the navigator.webdriver tell
        "--disable-features=Translate,IsolateOrigins,site-per-process",
        "--window-size=1440,900",
        "--window-position=0,0",
        "--lang=en-US",
    ]
    if not headful:
        args += ["--headless=new", "--disable-gpu"]

    try:
        _chrome_proc = await asyncio.create_subprocess_exec(
            *args, env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as e:
        return f"[web_cli] couldn't launch Chromium: {e}"
    for _ in range(24):  # up to ~12s for the debug port to come up
        await asyncio.sleep(0.5)
        if await _cdp_alive():
            log.info("opencli_cdp_chrome_up", port=_CDP_PORT, headful=headful)
            return None
    return "[web_cli] Chromium started but the CDP port never came up."


def _site_token(command: str) -> str:
    parts = command.split()
    # skip leading subcommand groups so the receipt target is the site/adapter
    skip = {"browser", "recon", "run", "--format", "json", "yaml", "csv", "md", "table"}
    for p in parts:
        if p not in skip and not p.startswith("-"):
            return p
    return parts[0] if parts else ""


async def web_cli(command: str, timeout: int = 120) -> str:
    """Run an `opencli` command (everything after the word `opencli`) and return its output."""
    if not command or not command.strip():
        return "[web_cli] empty command. Example: web_cli(\"hn top --limit 5 --format json\")"

    # Policy gate first — a blocked write is blocked regardless of anything else.
    blocked = audit.gate_web_action(command)
    target = _site_token(command)
    if blocked:
        audit.record("web_cli", target=target, decision="blocked", reason=blocked,
                     params={"command": command})
        return blocked

    # Per-site/day cap — don't hammer a site (bot signal) or spin in a loop.
    if not audit.within_rate_limit("web_cli", target, _MAX_PER_SITE_DAY):
        audit.record("web_cli", target=target, decision="blocked", reason="rate cap",
                     params={"command": command})
        return (f"[web_cli] daily cap reached for '{target or 'this site'}' "
                f"({_MAX_PER_SITE_DAY}/day) — easing off so we don't look like a bot or burn a loop. "
                f"Come back to it later, or work a different site.")

    if shutil.which("opencli") is None:
        return ("[web_cli] opencli isn't installed on this host. It ships in the deployed "
                "image; locally, `npm install -g @jackwener/opencli` (needs Node ≥ 20).")

    err = await _ensure_cdp_chrome()
    if err:
        return err

    env = {
        **os.environ,
        "OPENCLI_CDP_ENDPOINT": _CDP_ENDPOINT,
        "HOME": str(_OPENCLI_HOME),   # adapters persist under /data
    }
    _OPENCLI_HOME.mkdir(parents=True, exist_ok=True)
    try:
        argv = ["opencli", *shlex.split(command)]
    except ValueError as e:
        return f"[web_cli] couldn't parse command ({e}). Check your quoting."

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return f"[web_cli] `opencli {command}` timed out after {timeout}s."
    except Exception as e:
        return f"[web_cli] failed to run opencli: {e}"

    stdout = (out or b"").decode(errors="replace").strip()
    stderr = (err_b or b"").decode(errors="replace").strip()
    rc = proc.returncode or 0

    audit.record("web_cli", target=target,
                 decision="allowed" if rc == 0 else "error",
                 reason=f"exit {rc}", params={"command": command})

    if rc == 0:
        return stdout or "(opencli returned no output)"
    # OpenCLI follows sysexits: 66 empty, 69 bridge down, 77 auth required — pass the
    # signal through so Miles can react (e.g. log in, or autofix the adapter).
    return f"[opencli exit {rc}] {stderr or stdout or '(no output)'}"


HANDLERS = {
    "web_cli": web_cli,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "web_cli",
            "description": (
                "Turn any website into a CLI for yourself, and run it. This drives OpenCLI against a "
                "headless browser, reusing a logged-in session. Pass everything that would follow the "
                "word `opencli`.\n"
                "Use it to:\n"
                "• run a built-in or crystallized command for structured data — add `--format json`: "
                "web_cli(\"hn top --limit 5 --format json\")\n"
                "• recon a new site once, then crystallize a reusable adapter: "
                "web_cli(\"browser recon analyze https://example.com\"), then init/write/verify it\n"
                "• repair a broken adapter: web_cli(\"browser autofix <site>/<command>\")\n"
                "Crystallized adapters persist, so a flow you crack once becomes a one-line command "
                "next time. Reads are fine to do yourself; LinkedIn-style write/social actions are "
                "blocked here on purpose (ban risk) and route to Akshay."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The opencli command, e.g. \"reddit top r/startups --limit 10 --format json\" or \"browser recon analyze https://site.com\"",
                    },
                    "timeout": {
                        "type": "integer",
                        "default": 120,
                        "description": "Seconds before giving up (raise for slow recon/verify)",
                    },
                },
                "required": ["command"],
            },
        },
    },
]
