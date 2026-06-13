# Miles v2 redesign — working plan

Research-backed redesign (Anthropic harness/context-engineering posts, Letta sleep-time compute).
Keep what's validated, fix the four architecture gaps, split the monolith.

## Keep (validated)
- FastAPI + single-queue consumer, heartbeats, boot continuation
- Graphiti/FalkorDB temporal graph, incremental dream()
- Clear-based in-turn compaction + LLM between-turn summarization
- Selective read_file/edit_file, tools-return-strings-never-raise

## Change

### 1. tools.py (3026 lines) → agent/tools/ package
Per-module HANDLERS dict + DEFINITIONS list; `__init__.py` aggregates into
TOOL_HANDLERS / TOOL_DEFINITIONS and re-exports public names.
- agent/config.py      — paths, env constants, KEYRING_SERVICE
- agent/runtime.py     — shared mutable refs (scheduler, graphiti), set by server
- tools/files.py       — read_file, read_heso_file, edit_file, write_sandbox_file, list_*
- tools/gmail.py       — google creds cache, send_email (+ sensitive-data guard), read_emails
- tools/calendar_tools.py — list/create/respond/find_free_slots
- tools/web.py         — scrape_url (firecrawl→jina→httpx merged), search_web, exa_search, read_pdf
- tools/browser.py     — browser_open, browser_action, list_sessions, chrome lock helpers
- tools/browser_auth.py— google_signin (+ post_actions), solve_captcha, read_sms
- tools/vision.py      — take_screenshot, analyze_image/screenshot/video
- tools/system.py      — install_package, run_python, run_shell
- tools/secrets_store.py — store/get/list/delete secret
- tools/skills.py      — create/run/list skill, download_github_skill (removeprefix fix kept)
- tools/memory.py      — journal_entry, dream (incremental + WRITES soul.md itself), search_memories, retrieve_episodes
- tools/tasks.py       — NEW task ledger: add_task, update_task, list_tasks → data/tasks.json
- tools/contacts.py    — signalhire_credits, signalhire_find_contact
- tools/subagent.py    — NEW real subagent: own tool loop (search/scrape/read/report toolset),
                          writes full output to data/reports/, returns summary + path

### 2. Real subagents (biggest behavior fix)
run_subagent(task, context, output_format) → mini agent loop, max 15 rounds,
restricted tools: search_web, exa_search, scrape_url, read_file, read_heso_file, read_pdf, write_report.
Full report → /data/reports/<slug>_<ts>.md; main agent gets summary + path (filesystem output pattern).

### 3. Durable agent state
core.py: after each run, persist {history, summary} → data/agent_state.json; load in __init__.
Restart no longer wipes working memory.

### 4. Task ledger
data/tasks.json; boot continuation injects open tasks; persona instructs add/update as work starts/finishes.

### 5. dream() owns soul.md (Letta pattern: sleep agent has write authority)
After lens analyses: final LLM call takes soul.md + analyses → returns updated
"What I'm learning / People I know / Things that matter right now" sections; dream applies them.

### 6. Tool diet
Drop remember/recall (+ /memory endpoint) — graph + soul + tasks cover it.
Fold firecrawl_scrape into scrape_url. ~50 → ~46 tools.

### 7. Loop polish
- Round cap: at round 27/30 inject wrap-up nudge (summarize, update tasks, set heartbeat) instead of dropping the turn.
- google_signin post_actions: continue in the SAME browser context post-OAuth (fixes Twilio MFA).

### 8. Persona rewrite
- Effort-scaled delegation (replace "subagent if >3 tool calls"): do small tool-work yourself;
  delegate research/drafting; parallelize independent subagents.
- Task ledger section; dream wording (4-hourly, auto-updates soul).
- PRESERVE VERBATIM: all secrets/trust/external-comms security constraints.

### server.py
runtime.* refs instead of module attrs; remove /memory endpoint; boot continuation includes open tasks;
inbox watcher imports creds from tools.gmail.

## Status
- [x] config + runtime
- [x] files, heartbeats, gmail, calendar, web, browser, browser_auth, vision, system, secrets, skills, memory, tasks, contacts, subagent
- [x] __init__ aggregator, delete old tools.py
- [x] core.py (durable state, wrap-up nudge, parallel tool calls)
- [x] persona.py rewrite
- [x] server.py updates (+ /tasks endpoint; frontend Memory tab → Tasks tab)
- [x] local tests (registry parity 50/50, compactor, durable state, ledger, dream soul/incremental, subagent toolset)
- [x] deploy + live verify (2026-06-12: healthy, graphiti up, /tasks live, /memory 404)
