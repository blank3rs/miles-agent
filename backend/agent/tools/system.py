"""Self-extension: install packages, run Python, run shell commands."""
import asyncio
import io
import subprocess
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout

from agent.config import BACKEND_DIR, DATA_DIR, HESO_ROOT, SANDBOX_ROOT

_BLOCKED_SHELL = ["rm -rf /", "rm -rf ~", "sudo", "> /dev/", "dd if=", "mkfs"]


async def install_package(package: str) -> str:
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["uv", "add", package],
            capture_output=True,
            text=True,
            cwd=str(BACKEND_DIR),
            timeout=120,
        )
        if result.returncode == 0:
            return f"Installed: {package}\n{result.stdout.strip()}"
        return f"[install failed] {result.stderr.strip() or result.stdout.strip()}"
    except FileNotFoundError:
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "pip", "install", package, "--quiet"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                return f"Installed via pip: {package}"
            return f"[pip install failed] {result.stderr.strip()}"
        except Exception as e2:
            return f"[install failed] {e2}"
    except Exception as e:
        return f"[install failed] {e}"


async def run_python(code: str) -> str:
    """Execute Python code in-process and return stdout + stderr."""
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    try:
        exec_globals = {
            "__builtins__": __builtins__,
            "sandbox_root": SANDBOX_ROOT,
            "heso_root": HESO_ROOT,
            "data_dir": DATA_DIR,
        }
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exec(compile(code, "<agent>", "exec"), exec_globals)  # noqa: S102
        out = stdout_buf.getvalue()
        err = stderr_buf.getvalue()
        parts = []
        if out:
            parts.append(f"stdout:\n{out.strip()}")
        if err:
            parts.append(f"stderr:\n{err.strip()}")
        return "\n\n".join(parts) if parts else "(no output)"
    except Exception:
        err = stderr_buf.getvalue()
        return f"[exception]\n{traceback.format_exc()}\n{err}".strip()


async def run_shell(command: str) -> str:
    for b in _BLOCKED_SHELL:
        if b in command:
            return f"[blocked] That command is not allowed: {b}"
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            command,
            shell=True,  # noqa: S602
            capture_output=True,
            text=True,
            cwd=str(SANDBOX_ROOT),
            timeout=60,
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr] {err}")
        return "\n".join(parts) if parts else f"(exit code {result.returncode}, no output)"
    except subprocess.TimeoutExpired:
        return "[error] Command timed out after 60s"
    except Exception as e:
        return f"[error running shell command] {e}"


HANDLERS = {
    "install_package": install_package,
    "run_python":      run_python,
    "run_shell":       run_shell,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "install_package",
            "description": "Install a Python package into your environment. Use this whenever you need a library that isn't available.",
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {"type": "string", "description": "Package name (e.g. 'requests', 'pandas', 'scrapling')"},
                },
                "required": ["package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute arbitrary Python code and get stdout/stderr back. Use for data processing, prototyping, computation, or anything that needs code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command inside your sandbox directory. For reading files prefer read_file — it's cheaper and paginates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
]
