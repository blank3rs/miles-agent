"""exec_sandboxed: run untrusted code in a real process boundary, with a read-only tool API.

This replaces run_python's in-process exec() — which handed arbitrary code the live
__builtins__, the real miles.db handle, and the full process env (every secret) — with a
true subprocess boundary:

  • a separate PID with its own memory, spawned via asyncio.create_subprocess_exec
  • a SCOPED minimal env (os.environ is NOT inherited) — this alone closes the secret leak
    that run_python and web_cli both had (FOUNDRY/AZURE keys, EMAIL creds, VOICE token…)
  • cwd in a fresh per-call mkdtemp under SANDBOX_ROOT (no view of the real tree)
  • rlimits (CPU, address space, file size, no core) set in a preexec_fn on Linux
  • start_new_session=True so the child is its own process group; a hard asyncio.wait_for
    SIGKILLs the WHOLE group on timeout, so a child that forks leaves no orphan
  • no network helpers and no proxy env injected

The "MCP" capability is the code-execution-as-tools pattern: a tiny JSON-RPC bridge over a
dedicated fd pair (NOT stdin/stdout, which the code's own I/O owns) lets the sandboxed code
call back into a HARDCODED read-only allowlist of Miles tools. Those calls are serviced by
the PARENT under its normal store lock + autonomy gating (via agent.tools.call_tool) — the
child never imports agent.*, never opens a second DB connection, never reaches an ACTION tool.
The allowlist is RESEARCH reads only (search_web/exa_search/scrape_url/read_pdf) plus the two
read-only memory tools — deliberately NOT arbitrary filesystem reads (read_file/read_heso_file
are excluded), because the bridge runs under the parent's full filesystem authority and on prod
.env lives directly under SANDBOX_ROOT. The allowlist is the security boundary, independent of
policy.tool_kind.

DEFERRED hardening (known gaps, NOT auto-deployed because of them): this is pure subprocess +
rlimit, not a full container. The scoped env removes credentials from the CHILD's process env,
but that is not the whole story — the parent-mediated bridge must therefore expose reads-only-
research tools only, never general filesystem reads, so even a future bridge regression can't
reach the secrets. Two deploy-time tiers are intentionally left for a later pass: (1) relocate
the prod .env OFF the /data mount (e.g. mount it at /run/secrets/miles.env, outside SANDBOX_ROOT)
so no path under the sandbox root can ever reach it; (2) gVisor / a rootless, network-namespaced
sandbox container for true network-egress isolation (rlimit does not block sockets). Code-exec is
NOT being deployed yet; pure-subprocess ships with zero infra change (this VM's miles container
runs as root with no docker.sock mounted, so a docker-in-docker sandbox would hand the agent
root-on-host — strictly worse).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

import structlog

from agent import audit
from agent.config import (
    SANDBOX_EXEC_CPU_SECONDS,
    SANDBOX_EXEC_FSIZE_MB,
    SANDBOX_EXEC_MAX_OUTPUT,
    SANDBOX_EXEC_MAX_PER_DAY,
    SANDBOX_EXEC_MEM_MB,
    SANDBOX_EXEC_TIMEOUT_S,
    SANDBOX_ROOT,
)

try:
    import resource  # guarded so module import never fails where it's absent.
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]

# rlimits are only reliably enforceable on Linux (the prod container). macOS imports `resource`
# but rejects RLIMIT_AS in setrlimit, so we skip the preexec there and log instead — the handler
# still runs locally, just without limits. Real limits hold in the prod Linux container.
_RLIMITS_AVAILABLE = resource is not None and sys.platform.startswith("linux")

log = structlog.get_logger()

# Read-only tool allowlist exposed to sandboxed code. This is the security boundary — the
# RPC server rejects anything outside it regardless of TOOL_HANDLERS / policy.tool_kind.
# RESEARCH reads only (search the web / scrape a URL / read a PDF) plus the two read-only
# memory tools. Deliberately NO read_file / read_heso_file: those are general filesystem-read
# primitives over /data and /heso, and on prod /data holds .env directly under SANDBOX_ROOT —
# handing the child an arbitrary local read would re-open the exact secret-exfil hole the scoped
# env closes. If sandboxed code needs a specific file's contents, the parent reads it (read_file)
# and inlines it into `code`. NEVER send_email / make_call / web_cli / browser_task / writes.
_BRIDGE_TOOLS: frozenset[str] = frozenset({
    "search_web", "exa_search", "scrape_url", "read_pdf",
    "search_memories", "retrieve_episodes",
})

# Child shim, run with `python -I` (isolated mode: ignores PYTHON* env, no user site, no
# implicit cwd on sys.path). It imports NOTHING from agent — it only speaks JSON-RPC over two
# inherited fds whose numbers arrive via env. It reads the user code from stdin and execs it
# with a `miles` client whose .call(tool, **kw) (and per-tool shortcuts) round-trips to the
# parent, which runs the real tool under the store lock + gating.
_BOOTSTRAP = r"""
import json, os, sys

_req_fd = int(os.environ["_MILES_RPC_REQ_FD"])
_resp_fd = int(os.environ["_MILES_RPC_RESP_FD"])
_req = os.fdopen(_req_fd, "w", buffering=1)
_resp = os.fdopen(_resp_fd, "r")

_ALLOWED = (
    "search_web", "exa_search", "scrape_url", "read_pdf",
    "search_memories", "retrieve_episodes",
)


class _Miles:
    def call(self, tool, **kwargs):
        _req.write(json.dumps({"tool": tool, "kwargs": kwargs}) + "\n")
        _req.flush()
        line = _resp.readline()
        if not line:
            return "[bridge closed]"
        try:
            msg = json.loads(line)
        except Exception:
            return "[bridge decode error]"
        if "error" in msg:
            return "[bridge error] " + str(msg["error"])
        return msg.get("result", "")


_miles = _Miles()
for _t in _ALLOWED:
    setattr(_Miles, _t, (lambda _name: (lambda self, **kw: self.call(_name, **kw)))(_t))

_code = sys.stdin.read()
exec(compile(_code, "<exec_sandboxed>", "exec"), {"miles": _miles, "__name__": "__main__"})
"""


def _scoped_env(workdir: Path) -> dict[str, str]:
    """A MINIMAL env for the child. Does NOT read os.environ — that is the fix for the
    secret leak in web_cli (splats full os.environ) and run_python (in-process, full env).
    No credentials, no proxy vars, no FOUNDRY/AZURE/EMAIL/VOICE keys reach the child."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "SANDBOX_WORKDIR": str(workdir),
    }


def _limits() -> None:  # preexec_fn — runs in the child between fork and exec (Linux only).
    """Cap CPU seconds, address space, file size, and disable core dumps. Only called when
    `resource` is available (Linux container); omitted entirely on macOS local dev."""
    resource.setrlimit(resource.RLIMIT_CPU, (SANDBOX_EXEC_CPU_SECONDS, SANDBOX_EXEC_CPU_SECONDS))
    mem = SANDBOX_EXEC_MEM_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    fsize = SANDBOX_EXEC_FSIZE_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


async def _serve_rpc(req_r: int, resp_w: int, allow_tools: bool) -> None:
    """Parent-side bridge loop. Reads newline-framed {'tool','kwargs'} requests from the child
    over `req_r`, runs allowlisted reads via call_tool (under the normal store lock + gating in
    THIS process), and writes newline-framed {'result'|'error'} back over `resp_w`. Catches
    everything — a bridge failure returns an error result to the child, never crashes the
    runner. Exits when the child closes its end (the runner SIGKILLs the group on timeout)."""
    from agent.tools import call_tool  # lazy: agent.tools imports this module

    loop = asyncio.get_running_loop()
    req = os.fdopen(req_r, "r")
    resp = os.fdopen(resp_w, "w", buffering=1)
    try:
        while True:
            line = await loop.run_in_executor(None, req.readline)
            if not line:
                return
            try:
                msg = json.loads(line)
                tool = msg.get("tool")
                kwargs = msg.get("kwargs") or {}
            except Exception as e:
                resp.write(json.dumps({"error": f"bad request: {e}"}) + "\n")
                resp.flush()
                continue
            if not allow_tools or tool not in _BRIDGE_TOOLS:
                resp.write(json.dumps({"error": "tool_not_allowed"}) + "\n")
                resp.flush()
                log.info("sandbox_bridge_blocked", tool=tool)
                continue
            try:
                result = await call_tool(tool, **kwargs)
            except Exception as e:  # call_tool catches its own, but be defensive
                result = f"[bridge tool error] {e}"
            resp.write(json.dumps({"result": str(result)}) + "\n")
            resp.flush()
            log.info("sandbox_bridge_call", tool=tool)
    except Exception as e:
        log.warning("sandbox_bridge_failed", err=str(e))
    finally:
        try:
            req.close()
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass


async def exec_sandboxed(code: str, timeout: int = 30, allow_tools: bool = True) -> str:
    """Run Python code in an isolated subprocess and return its combined stdout/stderr.

    The child has NO access to my secrets (a scoped env, not os.environ), a fresh temp working
    directory (not the real tree), CPU/memory/file-size limits (Linux), and — when allow_tools
    is set — a read-only `miles` API for safe reads/searches. Every run is rate-limited and
    receipted. Returns a string always; raises only TypeError on a bad signature."""
    if not code or not code.strip():
        return "[exec_sandboxed] empty code."

    code_sha = hashlib.sha256(code.encode("utf-8", "replace")).hexdigest()[:16]
    timeout = int(timeout) if timeout else SANDBOX_EXEC_TIMEOUT_S

    if not audit.within_rate_limit("exec_sandboxed", "sandbox", SANDBOX_EXEC_MAX_PER_DAY):
        audit.record("exec_sandboxed", target="sandbox", decision="blocked",
                     reason="rate cap", params={"code_sha": code_sha})
        return (f"[exec_sandboxed] daily cap reached ({SANDBOX_EXEC_MAX_PER_DAY}/day) — "
                f"easing off to avoid a runaway loop. Try again later.")

    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="sbx-", dir=str(SANDBOX_ROOT)))

    req_r, req_w = os.pipe()    # child writes requests to req_w; parent reads req_r
    resp_r, resp_w = os.pipe()  # parent writes responses to resp_w; child reads resp_r

    env = _scoped_env(workdir)
    env["_MILES_RPC_REQ_FD"] = str(req_w)
    env["_MILES_RPC_RESP_FD"] = str(resp_r)

    preexec = _limits if _RLIMITS_AVAILABLE else None
    if not _RLIMITS_AVAILABLE:
        log.warning("sandbox_unlimited_local", note="rlimits not enforceable here (non-Linux) — skipped; enforced in prod")

    proc = None
    rpc_task = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-c", _BOOTSTRAP,
                cwd=str(workdir),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                preexec_fn=preexec,
                pass_fds=(req_w, resp_r),
            )
        finally:
            # The child owns req_w / resp_r now; the parent must close its copies so the
            # child's close is the real EOF the bridge loop and the child's reader see.
            os.close(req_w)
            os.close(resp_r)

        rpc_task = asyncio.create_task(_serve_rpc(req_r, resp_w, allow_tools))
        req_r = resp_w = -1  # ownership handed to the bridge task; don't double-close below

        try:
            out, err = await asyncio.wait_for(proc.communicate(input=code.encode()), timeout=timeout)
        except asyncio.TimeoutError:
            _kill_group(proc)
            audit.record("exec_sandboxed", target="sandbox", decision="error",
                         reason=f"timeout {timeout}s", params={"code_sha": code_sha})
            return f"[exec_sandboxed] timed out after {timeout}s — killed the process group."

        rc = proc.returncode or 0
        stdout = (out or b"").decode(errors="replace").strip()
        stderr = (err or b"").decode(errors="replace").strip()

        audit.record("exec_sandboxed", target="sandbox",
                     decision="allowed" if rc == 0 else "error",
                     reason=f"exit {rc}", params={"code_sha": code_sha})

        parts = []
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        result = "\n\n".join(parts) if parts else (f"(exit {rc}, no output)" if rc else "(no output)")
        if len(result) > SANDBOX_EXEC_MAX_OUTPUT:
            result = result[:SANDBOX_EXEC_MAX_OUTPUT] + "\n…[truncated]"
        return result
    except Exception as e:
        audit.record("exec_sandboxed", target="sandbox", decision="error",
                     reason=f"runner: {e}", params={"code_sha": code_sha})
        return f"[exec_sandboxed] failed to run: {e}"
    finally:
        if rpc_task is not None:
            rpc_task.cancel()
        for fd in (req_r, resp_w):
            if fd is not None and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            _cleanup(workdir)
        except Exception:
            pass


def _kill_group(proc) -> None:
    """SIGKILL the child's whole process group (start_new_session put it in its own), so a
    child that forked grandchildren leaves no orphans."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def _cleanup(workdir: Path) -> None:
    import shutil
    shutil.rmtree(workdir, ignore_errors=True)


HANDLERS = {
    "exec_sandboxed": exec_sandboxed,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "exec_sandboxed",
            "description": (
                "Run Python code in an isolated subprocess and get stdout/stderr back. Use this for "
                "data processing, prototyping, computation, or anything that needs code. The code runs "
                "with NO access to my secrets, in a fresh temporary working directory, under CPU/memory/"
                "time limits. When allow_tools is on, the code gets a read-only `miles` API for safe "
                "research reads/searches — e.g. miles.search_web(query=\"...\") or "
                "miles.call(\"scrape_url\", url=\"...\") — but it can never read local files, send email, "
                "make calls, or write anything. (Need a specific file's contents? Read it with read_file "
                "first and paste it into the code.) This is the safe replacement for run_python."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"},
                    "timeout": {
                        "type": "integer",
                        "default": 30,
                        "description": "Seconds before the process group is killed",
                    },
                    "allow_tools": {
                        "type": "boolean",
                        "default": True,
                        "description": "Expose the read-only `miles` tool API to the code (search/read only)",
                    },
                },
                "required": ["code"],
            },
        },
    },
]
