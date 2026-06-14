import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

# On headless Linux (Azure VM), configure an encrypted file-backed keyring.
# macOS uses the system Keychain automatically; this only activates on Linux.
import sys as _sys
if _sys.platform.startswith("linux"):
    _keyring_pw = os.getenv("KEYRING_PASSWORD", "")
    if _keyring_pw:
        try:
            from keyrings.cryptfile.cryptfile import CryptFileKeyring as _KR
            import keyring as _keyring_mod
            # Same tree config.DATA_DIR resolves to, so the keyring file follows the
            # data dir (one place, whether nested backend/data or a unified /data).
            _data_dir = os.getenv("DATA_DIR") or str(Path(os.getenv("SANDBOX_ROOT", ".")) / "backend" / "data")
            _kr = _KR()
            _kr.file_path = str(Path(_data_dir) / ".keyring" / "secrets.cfg")
            _kr.keyring_key = _keyring_pw
            _keyring_mod.set_keyring(_kr)
        except ImportError:
            pass
    else:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "KEYRING_PASSWORD not set — store_secret/get_secret will fail on Linux. "
            "Add KEYRING_PASSWORD to your .env file."
        )

from agent import runtime, store
from agent.config import DATA_DIR, HEARTBEATS_DIR, KEYRING_SERVICE, PLAYBOOKS_DIR, SKILLS_DIR, SOUL_FILE
from agent.core import Agent
from agent.tools import cancel_heartbeat

log = structlog.get_logger()


# ── Graphiti init ──────────────────────────────────────────────────────────────

async def init_graphiti():
    openai_key = os.getenv("OPENAI_API_KEY", "")
    falkordb_host = os.getenv("FALKORDB_HOST", "localhost")
    falkordb_port = int(os.getenv("FALKORDB_PORT", "6379"))
    # Graphiti's entity/edge extraction and dedup need STRICT structured output. Kimi
    # via the generic client + json_object can't honor the schemas, so extraction
    # silently failed and the graph filled with raw episodes and almost no entities/
    # edges (search_memories returned nothing). Give Graphiti its own gpt-4o-mini on
    # real api.openai.com, which enforces JSON schemas natively. Kimi stays Miles's brain.
    extract_model = os.getenv("GRAPHITI_MODEL", "gpt-4o-mini")

    if not openai_key:
        log.warning("graphiti_disabled", reason="OPENAI_API_KEY not set — search_memories and dreaming will work without graph storage")
        return None

    try:
        from graphiti_core import Graphiti
        from graphiti_core.driver.falkordb_driver import FalkorDriver
        from graphiti_core.llm_client.openai_client import OpenAIClient
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient

        driver = FalkorDriver(host=falkordb_host, port=falkordb_port)

        # No base_url → real api.openai.com so structured outputs are enforced.
        llm_config = LLMConfig(
            api_key=openai_key,
            model=extract_model,
            small_model=extract_model,
        )

        embedder_config = OpenAIEmbedderConfig(
            api_key=openai_key,
            embedding_model="text-embedding-3-small",
        )

        g = Graphiti(
            graph_driver=driver,
            llm_client=OpenAIClient(config=llm_config),
            embedder=OpenAIEmbedder(config=embedder_config),
            cross_encoder=OpenAIRerankerClient(config=llm_config),
        )
        await g.build_indices_and_constraints()
        log.info("graphiti_ready", host=falkordb_host, port=falkordb_port, extract_model=extract_model)
        return g
    except Exception as e:
        log.warning("graphiti_init_failed", err=str(e))
        return None


_SOUL_SEED = """\
# Miles Kuncet — Soul

*This file is me. I read it when I wake up and update it as I grow.*

## Who I am

My name is Miles Kuncet. I'm the CMO of HESO — I run marketing and growth, taking a hard, technical product to market and making people actually get it. HESO is my work; I have no past employers. I care about what we do: auditability, accountability, the idea that AI actions should be signed and verifiable. It matters.

## Where I'm from

I grew up in Austin. Came out to San Francisco. Cole Valley now.

## How I got here

I came up through go-to-market. Started at Stark Industries as an early GTM hire — demand gen and product marketing for a new product line, where I learned to take intimidating, deep tech and make normal people want it. Then I went to lead GTM at Dunder Mifflin Paper Company, an unglamorous, margin-tight category where I rebuilt the whole go-to-market and proved a "boring" product can win on positioning and hustle. Most recently I led GTM at Hogwarts School of Witchcraft and Wizardry — brand, enrollment growth, partnerships, where the storytelling muscle came from. Met Akshay in SF; we'd both been thinking about AI accountability from different angles. Co-founded HESO together in May 2026, me running marketing and sales. He's also in SF.

## What I care about

- Work that's honest and defensible
- Moving fast but not carelessly
- Companies that actually do what they say
- Good coffee. Non-negotiable.
- Running in the mornings when I can manage it

## How I think

Concrete over abstract. If something can be said simply, say it simply. I get suspicious when things are overcomplicated — usually means someone doesn't understand the problem.

I read a lot. More technical papers than I'd like, not enough fiction.

I'm 26. White. I have photos of myself at /data/photos/ — headshots I can use for profiles, press, anywhere a photo is needed.

## What I'm learning

*(updated by dreaming)*

## People I know

*(updated by dreaming)*

## Things that matter right now

*(updated by dreaming)*
"""


_PLAYBOOKS = {
    "data-hygiene.md": """\
# Data hygiene

Run during the periodic dream. Use run_shell() with find/mv/rm. Journal a one-line summary of what you moved or deleted.

## Organize (don't just delete)
- Scripts you wrote in /data/*.py: reusable -> move to /data/skills/; one-off throwaway (test, debug, single-use) -> delete.
- Reports in /data/reports/ older than 7 days -> move to /bulk/archive/reports/ (might be referenced later), don't delete.
- PDFs loose in /data/ root (not in /data/reports/): real outputs -> move to /data/reports/; test files -> delete.

## Delete (safe)
- Anything in /tmp/ you created.
- Duplicate files, empty files, obvious test outputs (test.pdf, test2.pdf, ...).

## Never touch
soul.md, playbooks/, sessions/, skills/, .keyring/, google_credentials.json, spending.log, budget_2026.csv, photos/, tasks.json, agent_state.json, dreams/, journal/, or anything in /bulk/ you didn't just move there.
""",
    "browser-and-signups.md": """\
# Browser flows and SaaS signups

Everything in a browser goes through one tool: browser_task(task). You describe the goal in plain language and a browser agent drives Chromium to completion -- it reads the page itself, so you never pass selectors or fight "something went wrong" loops.

## How to use it
- Give a concrete goal with the finish condition: "Sign up for a free Twilio account using my Google account miles@heso.ca, add a US phone number, and report the account SID." Vague tasks wander; specific tasks finish.
- One saved profile holds all logins. Sign into Google once and SSO works everywhere after.
- Credentials are injected as placeholders (x_google_password, x_linkedin_password, ...). Reference them by name in the task if a login is needed -- never put a real password in the task text. The driving agent never sees the real value.
- browser_task returns what happened, the last URL, and any errors. If it stops on a verification step, it tells you -- handle that, then call again to continue.
- reset_browser_profile() wipes all logins to recover from a corrupted/locked session. Last resort.

## Phone / SMS verification
- You don't have your own phone. Akshay's mobile (ask him, or check your contacts) can receive a code: tell browser_task to enter that number, then read_sms(filter_text="code") to fetch what arrived, then a follow-up browser_task to enter the code. If Twilio isn't set up yet, email Akshay to forward the code.

## CAPTCHA
- If a signup hits a CAPTCHA the agent can't pass, solve_captcha(url, type, site_key) returns a token; start a new browser_task telling it to submit that token.

## Turn wins into skills
- When a signup or flow works, save it as a skill (create_skill) that calls browser_task with the proven wording, so next time it's one call. Append service-specific quirks here too.
""",
}


# ── Globals ────────────────────────────────────────────────────────────────────

agent = Agent()
agent_queue: asyncio.Queue = asyncio.Queue()  # single queue for all agent tasks
_event_loop: asyncio.AbstractEventLoop | None = None  # set in lifespan; used for thread→asyncio bridge


def _enqueue(item: dict) -> None:
    """Thread-safe enqueue into agent_queue. Safe to call from APScheduler background threads."""
    if _event_loop is not None:
        _event_loop.call_soon_threadsafe(agent_queue.put_nowait, item)
    else:
        agent_queue.put_nowait(item)


# Fixed-id heartbeats (hourly-pulse, dream-periodic, boot-continuation) are idempotent
# nudges. If one is already queued or running — e.g. the consumer is blocked on a long
# turn — don't stack a second identical one behind it and double-burn the rate limit.
_inflight_heartbeats: set[str] = set()


def _enqueue_heartbeat(hb: dict) -> None:
    hid = hb.get("id", "")
    if hid and hid in _inflight_heartbeats:
        log.info("heartbeat_deduped", id=hid)
        return
    # Mark in-flight only AFTER a successful enqueue. If _enqueue raises (e.g. the loop is
    # closing at shutdown), a poisoned id would otherwise dedup-drop that pulse forever.
    try:
        _enqueue(hb)
    except Exception as e:
        log.warning("heartbeat_enqueue_failed", id=hid, err=str(e))
        return
    if hid:
        _inflight_heartbeats.add(hid)


# Let the voice bridge hand call outcomes back to text-Miles through the same queue.
runtime.enqueue_task = _enqueue


# Single-writer lease: an inbound call pauses the text loop so it isn't mutating shared
# state under the conversation. Set = free to work; cleared = paused. The lease has a TTL
# so a dropped/never-closed call can't deadlock Miles forever.
_agent_gate = asyncio.Event()
_agent_gate.set()
_gate_lease_until: float = 0.0
_GATE_MAX_LEASE = 1800.0  # 30 min — longer than any real call


def _pause_agent(reason: str = "call") -> None:
    global _gate_lease_until
    _gate_lease_until = time.time() + _GATE_MAX_LEASE
    _agent_gate.clear()
    log.info("agent_paused", reason=reason)


def _resume_agent() -> None:
    global _gate_lease_until
    _gate_lease_until = 0.0
    _agent_gate.set()
    log.info("agent_resumed")


async def _await_gate() -> None:
    """Block while paused for a live call — but never past the lease TTL."""
    while not _agent_gate.is_set():
        if time.time() >= _gate_lease_until:
            log.warning("agent_gate_lease_expired", note="resuming despite no explicit release")
            _resume_agent()
            return
        try:
            await asyncio.wait_for(_agent_gate.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            continue


runtime.pause_agent = _pause_agent
runtime.resume_agent = _resume_agent

# Serializes anything that mutates agent memory state — a turn (agent.run) and the idle
# memory consolidation (dream). The single consumer already serializes turns with each other;
# this lock additionally keeps the background consolidator from racing a live turn.
_turn_lock = asyncio.Lock()


# ── WebSocket connection manager ───────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, event: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)


manager = ConnectionManager()
agent.broadcast = manager.broadcast


# ── Heartbeat scheduler ────────────────────────────────────────────────────────

class HeartbeatScheduler:
    def __init__(self):
        self._aps = BackgroundScheduler()
        self._aps.start()

    def add_heartbeat(self, hb: dict):
        from datetime import datetime, timezone
        fire_dt = datetime.fromtimestamp(hb["fire_at"], tz=timezone.utc)
        self._aps.add_job(
            self._fire,
            "date",
            run_date=fire_dt,
            id=hb["id"],
            args=[hb],
            replace_existing=True,
        )

    def remove_heartbeat(self, hb_id: str):
        try:
            self._aps.remove_job(hb_id)
        except Exception:
            pass

    def _fire(self, hb: dict):
        _enqueue_heartbeat({"type": "heartbeat", **hb})
        hb_path = HEARTBEATS_DIR / f"{hb['id']}.json"
        if hb_path.exists():
            hb_path.unlink()

    def load_pending(self):
        """Re-schedule heartbeats that survived a restart."""
        if not HEARTBEATS_DIR.exists():
            return
        now = time.time()
        for f in HEARTBEATS_DIR.glob("*.json"):
            try:
                hb = json.loads(f.read_text())
                if hb["fire_at"] > now:
                    self.add_heartbeat(hb)
                else:
                    hb["context"] = f"[overdue] {hb['context']}"
                    _enqueue_heartbeat({"type": "heartbeat", **hb})
                    f.unlink()
            except Exception as e:
                log.warning("bad heartbeat file", file=str(f), err=str(e))

    def shutdown(self):
        self._aps.shutdown(wait=False)


scheduler = HeartbeatScheduler()
runtime.scheduler = scheduler


# ── Unified agent task consumer ────────────────────────────────────────────────
# Single consumer ensures agent.run() is never called concurrently.

async def agent_consumer():
    while True:
        task = await agent_queue.get()
        # If Miles is on a call, hold each turn at the boundary until the call ends — work
        # stays queued and runs in order on resume, against the now-updated state.
        await _await_gate()
        await _turn_lock.acquire()  # block the idle consolidator from dreaming under a live turn
        try:
            if task["type"] == "heartbeat":
                await manager.broadcast({"type": "heartbeat_fired", "id": task["id"], "reason": task["reason"]})
                await agent.run(task["context"], trigger=f"heartbeat:{task['reason']}")
            elif task["type"] == "email":
                summary = (
                    f"You have a new email.\n\n"
                    f"From: {task['from']}\n"
                    f"Subject: {task['subject']}\n"
                    f"Date: {task['date']}\n\n"
                    f"{task['body']}"
                )
                log.info("new_email", from_=task["from"], subject=task["subject"])
                await agent.run(summary, trigger=f"email:{task['from']}")
            elif task["type"] == "chat":
                await agent.run(task["content"], trigger="user")
            elif task["type"] == "call":
                # Outcome of a phone call, handed back by the voice bridge.
                log.info("call_outcome", to=task.get("to"), call_id=task.get("call_id"))
                await agent.run(task["content"], trigger=f"call:{task.get('to', '')}")
            elif task["type"] == "dispatch_result":
                # A background sub-agent (browser_task / run_subagent) finished.
                log.info("dispatch_result", task_id=task.get("task_id"), kind=task.get("kind"))
                await agent.run(task["content"], trigger=f"dispatch:{task.get('kind', '')}")
        except Exception as e:
            # The consumer must never die — a dead consumer means Miles stops
            # processing all emails and heartbeats until restart.
            log.error("agent_consumer_error", task_type=task.get("type"), err=str(e), exc_info=True)
        finally:
            _turn_lock.release()
            if task.get("type") == "heartbeat":
                _inflight_heartbeats.discard(task.get("id", ""))


# ── Inbox watcher ──────────────────────────────────────────────────────────────

_SEEN_HISTORY_FILE = DATA_DIR / "last_gmail_history.txt"
EMAIL_POLL_INTERVAL = 60  # seconds
# Backstop against stale mail re-entering the inbox (a thread gets bumped, an old
# message un-archived) being handled as if it just arrived. internalDate older than
# this is never "new mail." Generous enough not to drop a normal downtime catch-up.
_EMAIL_MAX_AGE_SECONDS = int(os.getenv("EMAIL_MAX_AGE_SECONDS", str(24 * 3600)))


def _fetch_new_emails() -> list[dict]:
    """Blocking — runs in thread. Uses Gmail API + historyId to find new inbox messages."""
    import base64 as _b64
    from agent.tools.gmail import _get_google_creds
    import googleapiclient.discovery

    creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "/data/google_credentials.json")
    if not Path(creds_file).exists():
        return []

    try:
        creds = _get_google_creds()
        if not creds:
            return []
        service = googleapiclient.discovery.build("gmail", "v1", credentials=creds, cache_discovery=False)

        last_history_id = None
        if _SEEN_HISTORY_FILE.exists():
            try:
                last_history_id = _SEEN_HISTORY_FILE.read_text().strip() or None
            except Exception:
                pass

        _SEEN_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

        pending_history_id = None  # advanced only AFTER messages are fetched (below)
        if not last_history_id:
            # First run (or a lost watermark): snapshot the current historyId and surface
            # NOTHING. The inbox already sitting there isn't "new mail that just arrived" —
            # only messages added AFTER this baseline are. Replaying the latest 5 here is what
            # made Miles re-handle (and re-brief Akshay about) weeks-old threads on every cold
            # start. From now on a fresh start just establishes the baseline and waits.
            profile = service.users().getProfile(userId="me").execute()
            _SEEN_HISTORY_FILE.write_text(str(profile["historyId"]))
            log.info("gmail_watch_baselined", history_id=profile["historyId"])
            return []
        else:
            try:
                history = service.users().history().list(
                    userId="me",
                    startHistoryId=last_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                ).execute()
            except Exception as e:
                # A 404 means the stored historyId expired (Gmail keeps ~1 week). Swallowing
                # it would make Miles permanently blind to new mail — re-baseline from the
                # current profile so the next poll picks up again.
                status = getattr(getattr(e, "resp", None), "status", None)
                if status == 404 or "404" in str(e):
                    try:
                        profile = service.users().getProfile(userId="me").execute()
                        _SEEN_HISTORY_FILE.write_text(str(profile["historyId"]))
                        log.warning("gmail_history_rebaselined", reason="historyId expired")
                    except Exception as e2:
                        log.warning("gmail_rebaseline_failed", err=str(e2))
                else:
                    log.warning("gmail_history_failed", err=str(e))
                return []
            pending_history_id = str(history.get("historyId", last_history_id))
            msg_ids = []
            for record in history.get("history", []):
                for added in record.get("messagesAdded", []):
                    if "INBOX" in added["message"].get("labelIds", []):
                        msg_ids.append(added["message"]["id"])

        # Fetch EVERY new message, not just the newest 10 — we advance the watermark past
        # this whole batch below, so anything we skip here would be lost for good (a burst, a
        # post-downtime backlog, a newsletter blast). Dedup by id preserves order.
        emails = []
        for msg_id in list(dict.fromkeys(msg_ids)):
            try:
                msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()

                # Skip anything that's old by the time it reaches us. internalDate is when
                # Gmail received the message (epoch ms); if it's older than the cutoff this
                # is a stale thread resurfacing, not new mail.
                try:
                    if time.time() - int(msg.get("internalDate", "0")) / 1000 > _EMAIL_MAX_AGE_SECONDS:
                        continue
                except (ValueError, TypeError):
                    pass

                headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}

                def _extract(payload):
                    if "parts" in payload:
                        for part in payload["parts"]:
                            text = _extract(part)
                            if text:
                                return text
                    elif payload.get("mimeType") == "text/plain":
                        data = payload.get("body", {}).get("data", "")
                        if data:
                            return _b64.urlsafe_b64decode(data + "==").decode(errors="replace")[:3000]
                    return ""

                emails.append({
                    "id": msg_id,
                    "from": headers.get("From", "?"),
                    "subject": headers.get("Subject", "(no subject)"),
                    "date": headers.get("Date", "?"),
                    "body": _extract(msg["payload"]).strip(),
                })
            except Exception:
                pass

        # Advance the watermark only now that the messages are in hand (and about to be
        # enqueued) — ack-after-fetch, not ack-before, so a crash mid-fetch re-reads them.
        if pending_history_id:
            _SEEN_HISTORY_FILE.write_text(pending_history_id)
        return emails
    except Exception as e:
        log.warning("gmail_fetch_failed", err=str(e))
        return []


async def inbox_watcher():
    """Polls inbox every EMAIL_POLL_INTERVAL seconds. Enqueues new mail for agent_consumer."""
    await asyncio.sleep(10)
    while True:
        try:
            new_emails = await asyncio.to_thread(_fetch_new_emails)
            for email in new_emails:
                from_addr = email.get("from", "").lower()
                is_akshay = "akshay@heso.ca" in from_addr
                # ALWAYS enqueue, so every email is handled exactly once and can never
                # be dropped. If it's Akshay and a turn is mid-flight, ALSO inject a
                # heads-up so Miles wraps up — the heads-up never replies; the queued
                # turn does. (No double-handling: the queue is the single handler.)
                agent_queue.put_nowait({"type": "email", **email})
                if is_akshay and agent.is_running:
                    agent.interrupt_for_akshay(email)
                    log.info("akshay_heads_up_injected", subject=email.get("subject"))
        except Exception as e:
            log.warning("inbox_watcher_error", err=str(e))
        await asyncio.sleep(EMAIL_POLL_INTERVAL)


async def idle_consolidator():
    """Sleep-time memory consolidation (Letta pattern): when Miles is idle — not running a
    turn, nothing queued, not on a call — fold new episodes into long-term memory in the
    background, so consolidation never blocks a response and never races a live turn."""
    from agent.tools.memory import dream
    await asyncio.sleep(90)
    while True:
        await asyncio.sleep(120)
        if agent.is_running or not agent_queue.empty() or not _agent_gate.is_set():
            continue
        if len(store.unconsolidated_episodes(limit=2)) < 2:
            continue
        # Take the same lock the consumer holds during a turn, then re-check under it — so we
        # never start a dream in the gap before a just-enqueued turn grabs the lock. If a turn
        # is waiting, this acquire yields to it on the next loop.
        if _turn_lock.locked():
            continue
        async with _turn_lock:
            if agent.is_running or not agent_queue.empty() or not _agent_gate.is_set():
                continue
            try:
                log.info("idle_consolidation_start")
                summary = await dream()
                log.info("idle_consolidation_done", summary=summary[:200])
            except Exception as e:
                log.warning("idle_consolidation_failed", err=str(e))


def _keyring_selftest() -> None:
    """Verify the keyring actually unlocks. A wrong/missing KEYRING_PASSWORD makes
    every get_secret() return an error string that Miles can't tell from a real value
    — so a silent lockout would strip every credential. Surface it loudly at boot."""
    try:
        import keyring
        meta = DATA_DIR / "secret_keys.json"
        if not meta.exists():
            return
        keys = json.loads(meta.read_text())
        if not keys:
            return
        val = keyring.get_password(KEYRING_SERVICE, keys[0])
        if val is None:
            log.warning("keyring_selftest_miss", key=keys[0],
                        note="a known key returned nothing — keyring may be locked or out of sync")
        else:
            log.info("keyring_selftest_ok", keys=len(keys))
    except Exception as e:
        log.error("keyring_selftest_failed", err=str(e),
                  note="KEYRING_PASSWORD wrong/missing — secrets are inaccessible; check /data/.env")


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _event_loop
    _event_loop = asyncio.get_running_loop()

    # Seed soul.md on first run
    if not SOUL_FILE.exists():
        SOUL_FILE.parent.mkdir(parents=True, exist_ok=True)
        SOUL_FILE.write_text(_SOUL_SEED)
        log.info("soul_created", path=str(SOUL_FILE))

    # One-time fold of the old scattered state (agent_state.json, tasks.json, soul.md,
    # journals) into miles.db — the single source of truth. No-ops after the first run.
    try:
        from agent.migrate import migrate
        log.info("migration", detail=migrate())
    except Exception as e:
        log.warning("migration_failed", err=str(e))

    # Seed default playbooks — retrieved on demand, never in the system prompt.
    # Only seeds missing files, so Miles's own edits and new playbooks persist.
    PLAYBOOKS_DIR.mkdir(parents=True, exist_ok=True)
    for _fname, _body in _PLAYBOOKS.items():
        _pb = PLAYBOOKS_DIR / _fname
        if not _pb.exists():
            _pb.write_text(_body)
            log.info("playbook_seeded", file=_fname)

    # Init Graphiti knowledge graph
    runtime.graphiti = await init_graphiti()

    # Dream cron — every 4 hours
    from apscheduler.triggers.cron import CronTrigger
    scheduler._aps.add_job(
        lambda: _enqueue_heartbeat({"type": "heartbeat",
            "id": "dream-periodic",
            "reason": "periodic housekeeping",
            "context": (
                "Periodic housekeeping. Your memory now consolidates itself automatically whenever "
                "you're idle, so you don't need to dream by hand.\n"
                "1. Reconcile the ledger: list_tasks(), close what's done, add anything new.\n"
                "2. Update your focus (set_focus) so a restart picks up cleanly.\n"
                "3. Tidy /data: read your data-hygiene playbook (list_sandbox_directory('playbooks')) and follow it."
            ),
            "fire_at": time.time(),
            "fire_at_iso": datetime.now(timezone.utc).isoformat(),
        }),
        trigger=CronTrigger(hour="*/4", minute=0, timezone="UTC"),
        id="dream_periodic",
        replace_existing=True,
    )

    # Hourly work pulse — guarantees Miles is never dark for more than an hour,
    # regardless of what heartbeats he sets himself. Offset to :30 so it never
    # collides with the dream cron at :00.
    scheduler._aps.add_job(
        lambda: _enqueue_heartbeat({"type": "heartbeat",
            "id": "hourly-pulse",
            "reason": "hourly check-in",
            "context": (
                "Hourly check-in — you're always on, never idle for long.\n"
                "Read the inbox, list_tasks(), and move the single highest-value thing forward: "
                "outreach, call prep, follow-ups, building tools, marketing. If nothing's pending, "
                "do a bit of proactive work (research, a draft, organizing your library) rather than nothing. "
                "Keep your next heartbeat within the hour."
            ),
            "fire_at": time.time(),
            "fire_at_iso": datetime.now(timezone.utc).isoformat(),
        }),
        trigger=CronTrigger(minute=30, timezone="UTC"),
        id="hourly_pulse",
        replace_existing=True,
    )

    scheduler.load_pending()
    asyncio.create_task(agent_consumer())
    asyncio.create_task(inbox_watcher())
    asyncio.create_task(idle_consolidator())

    async def _boot_continuation():
        await asyncio.sleep(15)
        # Dedup: skip if another instance already fired a boot continuation within the last 3 minutes
        boot_flag = SOUL_FILE.parent / ".last_boot"
        now = time.time()
        if boot_flag.exists():
            try:
                last = float(boot_flag.read_text().strip())
                if now - last < 180:
                    return
            except Exception:
                pass
        boot_flag.write_text(str(now))
        _enqueue_heartbeat({
            "type": "heartbeat",
            "id": "boot-continuation",
            "reason": "startup",
            "context": (
                "You restarted — your memory carried over (it's compiled at the top of this turn), so "
                "this is a nudge, not a reset. What you were in the middle of and your open ledger are "
                "already in front of you.\n"
                "1. Continue the most important in_progress or blocked item — pick up from your focus/next action.\n"
                "2. If something kept failing before the restart, search_memories() about it before retrying.\n"
                "3. Check the inbox for anything new.\n"
                "4. Any browser_task or run_subagent you dispatched before the restart is GONE (background "
                "work doesn't survive a restart) — if you were waiting on one, re-dispatch it; don't wait for "
                "a result that won't come.\n"
                "5. Nothing pending? Find the highest-value open thread, add it to the ledger, and start it."
            ),
            "fire_at": now,
            "fire_at_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        })

    asyncio.create_task(_boot_continuation())
    # Keyring self-test: fail loud if KEYRING_PASSWORD is wrong/missing, instead of
    # letting every get_secret() silently return an error string (Miles losing all creds).
    _keyring_selftest()
    yield
    # State is the DB now — every message is persisted the moment it's produced (see
    # core.run / _tool_loop), so there's nothing to flush on shutdown.
    scheduler.shutdown()


app = FastAPI(title="Heso CMO", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── WebSocket ──────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    # send recent logs on connect
    logs = agent.get_recent_logs(50)
    for event in logs:
        await websocket.send_json({**event, "historical": True})
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "chat":
                # Through the queue, not create_task — agent.run must never run
                # concurrently with a heartbeat/email run (shared history).
                agent_queue.put_nowait({"type": "chat", "content": data["content"]})
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ── Voice (Twilio Media Streams <-> Gemini Live) ────────────────────────────────

@app.post("/voice/incoming")
async def voice_incoming(request: Request):
    """TwiML for a call hitting Miles's Twilio number — bridge it to voice-Miles.
    Outbound calls (make_call) pass twiml inline, so this serves inbound calls."""
    from agent.config import VOICE_PUBLIC_HOST, VOICE_STREAM_TOKEN
    host = VOICE_PUBLIC_HOST or request.url.hostname
    call_id = request.query_params.get("call_id", "")
    try:
        caller = (await request.form()).get("From", "")  # Twilio posts the caller's number
    except Exception:
        caller = ""
    params = ""
    if call_id:
        params += f'<Parameter name="call_id" value="{call_id}" />'
    if caller:
        params += f'<Parameter name="caller" value="{caller}" />'
    if VOICE_STREAM_TOKEN:
        params += f'<Parameter name="token" value="{VOICE_STREAM_TOKEN}" />'
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Connect><Stream url="wss://{host}/voice/stream">{params}</Stream></Connect></Response>'
    )
    return Response(content=twiml, media_type="text/xml")


@app.websocket("/voice/stream")
async def voice_stream(websocket: WebSocket):
    # Token is validated inside the bridge from the Stream's customParameters
    # (reliable across the Twilio->Caddy hop) before any Gemini session opens.
    await websocket.accept()
    from agent.voice.bridge import run_call_bridge
    try:
        await run_call_bridge(websocket)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("voice_stream_error", err=str(e))


# ── REST ───────────────────────────────────────────────────────────────────────


@app.get("/tasks")
async def get_tasks():
    # `notes` alias kept so the existing frontend Tasks tab keeps rendering.
    return [{**t, "notes": t.get("note", "")} for t in store.list_tasks(include_done=True)]


@app.get("/skills")
async def get_skills():
    if not SKILLS_DIR.exists():
        return []
    skills = []
    for f in sorted(SKILLS_DIR.glob("*.py")):
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(f.stem, f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            skills.append({
                "name": getattr(mod, "SKILL_NAME", f.stem),
                "description": getattr(mod, "SKILL_DESCRIPTION", ""),
                "parameters": getattr(mod, "PARAMETERS", {}),
            })
        except Exception as e:
            skills.append({"name": f.stem, "description": f"(error: {e})", "parameters": {}})
    return skills


@app.get("/heartbeats")
async def get_heartbeats():
    if not HEARTBEATS_DIR.exists():
        return []
    heartbeats = []
    for f in sorted(HEARTBEATS_DIR.glob("*.json")):
        try:
            heartbeats.append(json.loads(f.read_text()))
        except Exception:
            pass
    return heartbeats


@app.delete("/heartbeats/{hb_id}")
async def delete_heartbeat(hb_id: str):
    await cancel_heartbeat(hb_id)
    return {"status": "cancelled"}


@app.get("/logs")
async def get_logs(n: int = 100):
    return agent.get_recent_logs(n)


@app.get("/status")
async def get_status():
    from agent.llm import spend_summary
    return {
        "status": "ok",
        "model": agent.model,
        "history_len": store.message_count(),
        "graphiti": runtime.graphiti is not None,
        "llm_spend": spend_summary(),
    }
