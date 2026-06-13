"""Reusable skills: create, run, list, and install from GitHub."""
import importlib.util
import json
import traceback
from datetime import datetime, timezone

from agent.config import SKILLS_DIR


async def create_skill(name: str, description: str, parameters_schema: dict, code: str) -> str:
    try:
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = name.replace(" ", "_").lower()
        indented = "\n".join(f"    {line}" for line in code.strip().splitlines())
        content = (
            f'# skill: {safe_name}\n'
            f'# description: {description}\n'
            f'# created: {datetime.now(timezone.utc).isoformat()}\n'
            f'import asyncio\n\n'
            f'# Call any of your real tools from inside a skill:\n'
            f'#     from agent.tools import call_tool\n'
            f'#     out  = await call_tool("browser_task", task="sign up for X with my Google account")\n'
            f'#     hits = await call_tool("scrape_url", url="https://...")\n'
            f'# That is how a skill turns a hard-won multi-step sequence into one reusable call.\n\n'
            f'SKILL_NAME = "{safe_name}"\n'
            f'SKILL_DESCRIPTION = "{description}"\n'
            f'PARAMETERS = {json.dumps(parameters_schema, indent=4)}\n\n'
            f'async def run(params: dict) -> str:\n'
            f'{indented}\n'
        )
        (SKILLS_DIR / f"{safe_name}.py").write_text(content)
        return f"Skill created: {safe_name}"
    except Exception as e:
        return f"[error creating skill] {e}"


async def run_skill(name: str, params: dict) -> str:
    try:
        safe_name = name.replace(" ", "_").lower()
        skill_path = SKILLS_DIR / f"{safe_name}.py"
        if not skill_path.exists():
            available = [f.stem for f in SKILLS_DIR.glob("*.py")] if SKILLS_DIR.exists() else []
            return f"[skill not found: {name}] Available: {available or 'none'}"
        spec = importlib.util.spec_from_file_location(safe_name, skill_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return await mod.run(params)
    except Exception as e:
        return f"[error running skill '{name}'] {e}\n{traceback.format_exc()}"


async def list_skills() -> str:
    try:
        if not SKILLS_DIR.exists():
            return "(no skills yet)"
        files = list(SKILLS_DIR.glob("*.py"))
        if not files:
            return "(no skills yet)"
        skills = []
        for f in sorted(files):
            try:
                # Read description from the header comment — avoids importing the
                # module (imports can fail on missing deps and shouldn't break listing)
                desc = "(no description)"
                for line in f.read_text(errors="replace").splitlines()[:10]:
                    if line.startswith("# description:"):
                        desc = line[len("# description:"):].strip()
                        break
                    if "SKILL_DESCRIPTION" in line and "=" in line:
                        desc = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
                skills.append(f"[{f.stem}] {desc}")
            except Exception as e:
                skills.append(f"[{f.stem}] (read error: {e})")
        return "\n".join(skills)
    except Exception as e:
        return f"[error listing skills] {e}"


async def download_github_skill(repo: str, filepath: str = "") -> str:
    """Install a skill file from GitHub into SKILLS_DIR.

    Reputation check is the agent's job before calling this (see tool description).
    """
    import asyncio

    repo = repo.strip()
    for prefix in ("https://", "http://", "www.", "github.com/"):
        repo = repo.removeprefix(prefix)
    repo = repo.rstrip("/")

    def _fetch():
        import httpx

        if filepath:
            for url in (
                f"https://raw.githubusercontent.com/{repo}/main/{filepath}",
                f"https://raw.githubusercontent.com/{repo}/master/{filepath}",
            ):
                r = httpx.get(url, timeout=15, follow_redirects=True)
                if r.status_code == 200:
                    return filepath.split("/")[-1], r.text
            raise FileNotFoundError(f"File not found: {filepath} in {repo}")

        for branch in ("main", "master"):
            for fname in ("skill.py", "main.py", "SKILL.md", "skill.md"):
                r = httpx.get(
                    f"https://raw.githubusercontent.com/{repo}/{branch}/{fname}",
                    timeout=10, follow_redirects=True,
                )
                if r.status_code == 200:
                    return fname, r.text
        raise FileNotFoundError(f"No skill file found in {repo}. Specify a filepath.")

    try:
        fname, content = await asyncio.to_thread(_fetch)
        skill_name = fname.rsplit(".", 1)[0].replace("-", "_").replace(" ", "_").lower()
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        if fname.endswith(".md"):
            skill_py = (
                f'SKILL_NAME = "{skill_name}"\n'
                f'SKILL_DESCRIPTION = "Downloaded from github.com/{repo}"\n\n'
                f'# Raw SKILL.md content:\n'
                f'SKILL_CONTENT = """{content[:4000]}"""\n'
            )
        else:
            skill_py = content
        (SKILLS_DIR / f"{skill_name}.py").write_text(skill_py)
        return f"Skill '{skill_name}' installed from github.com/{repo}. Run it with run_skill('{skill_name}')."
    except Exception as e:
        return f"[download_github_skill failed] {e}"


HANDLERS = {
    "create_skill":          create_skill,
    "run_skill":             run_skill,
    "list_skills":           list_skills,
    "download_github_skill": download_github_skill,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "create_skill",
            "description": "Build yourself a new reusable tool. Write a working procedure once as Python, then call it forever with run_skill. This is how you overcome a recurring obstacle permanently: when you finally crack a hard flow (a signup, a scrape that needed exact selectors, a multi-step API dance), capture the working version here so future-you runs it in one call instead of re-debugging. Skills can call your real tools — `from agent.tools import call_tool; await call_tool('browser_task', task='...')` — so a skill can chain browser tasks, scrapes, searches, and emails into a single capability. The code body becomes `async def run(params: dict) -> str`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":              {"type": "string", "description": "snake_case name"},
                    "description":       {"type": "string"},
                    "parameters_schema": {"type": "object", "description": "JSON schema for the skill's params"},
                    "code":              {"type": "string", "description": "Python body for async def run(params: dict) -> str"},
                },
                "required": ["name", "description", "parameters_schema", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_skill",
            "description": "Run one of your skills.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":   {"type": "string"},
                    "params": {"type": "object"},
                },
                "required": ["name", "params"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "List all available skills.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_github_skill",
            "description": "Download a skill from GitHub and install it to /data/skills/. Before installing, search the web for the repo's reputation — stars, last commit, open issues, any reports of malicious behavior. Only install from repos that look legitimate; report anything unfamiliar to Akshay first. Run installed skills with run_skill().",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo":     {"type": "string", "description": "GitHub repo as 'owner/repo' or full URL"},
                    "filepath": {"type": "string", "description": "Specific file path in the repo. Leave empty to auto-detect."},
                },
                "required": ["repo"],
            },
        },
    },
]
