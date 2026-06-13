"""Filesystem tools: selective read, targeted edit, write, list.

All handlers return strings — they never raise.
"""
from pathlib import Path

from agent.config import HESO_ROOT, SANDBOX_ROOT


def _resolve_heso(relative: str) -> Path | None:
    try:
        p = (HESO_ROOT / relative).resolve()
        if str(p).startswith(str(HESO_ROOT.resolve())):
            return p
    except Exception:
        pass
    return None


def _resolve_sandbox(relative: str) -> Path | None:
    try:
        p = (SANDBOX_ROOT / relative).resolve()
        if str(p).startswith(str(SANDBOX_ROOT.resolve())):
            return p
    except Exception:
        pass
    return None


def _read_with_range(target: Path, offset: int = 0, limit: int = 0) -> str:
    """Read a file. With offset/limit, return that line window (1-based line numbers).
    Without them, return the whole file (truncated at 20k chars with a pointer to paginate)."""
    text = target.read_text(errors="replace")
    if not offset and not limit:
        if len(text) > 20000:
            return (
                text[:20000]
                + f"\n\n[truncated — {len(text):,} total chars. Read a specific part with "
                  f"offset/limit, or grep for the section you need.]"
            )
        return text
    lines = text.splitlines()
    start = max(0, offset)
    end = start + limit if limit else len(lines)
    chunk = lines[start:end]
    if not chunk:
        return f"(no lines in range — file has {len(lines)} lines)"
    numbered = "\n".join(f"{start + i + 1}\t{ln}" for i, ln in enumerate(chunk))
    return numbered + f"\n\n[lines {start + 1}-{start + len(chunk)} of {len(lines)}]"


def _norm_ws(s: str) -> str:
    """Normalize line endings and strip trailing whitespace per line."""
    return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def _list_dir(target: Path) -> str:
    entries = []
    for item in sorted(target.iterdir()):
        kind = "dir" if item.is_dir() else "file"
        try:
            size = "" if item.is_dir() else f"  {item.stat().st_size:,}b"
        except Exception:
            size = ""
        entries.append(f"[{kind}] {item.name}{size}")
    return "\n".join(entries) if entries else "(empty)"


async def read_heso_file(path: str, offset: int = 0, limit: int = 0) -> str:
    target = _resolve_heso(path)
    if target is None:
        return f"[error] Path is outside the heso root: {path}"
    if not target.exists():
        return f"[not found] {path}"
    if target.is_dir():
        return "[error] That's a directory — use list_heso_directory instead."
    try:
        return _read_with_range(target, offset, limit)
    except Exception as e:
        return f"[error reading file] {e}"


async def read_file(path: str, offset: int = 0, limit: int = 0) -> str:
    target = _resolve_sandbox(path)
    if target is None:
        return f"[error] Path is outside sandbox: {path}"
    if not target.exists():
        return f"[not found] {path}"
    if target.is_dir():
        return "[error] That's a directory — use list_sandbox_directory instead."
    try:
        return _read_with_range(target, offset, limit)
    except Exception as e:
        return f"[error reading file] {e}"


async def write_sandbox_file(path: str, content: str) -> str:
    target = _resolve_sandbox(path)
    if target is None:
        return f"[error] Path is outside sandbox: {path}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Written: {path} ({len(content):,} chars)"
    except Exception as e:
        return f"[error writing file] {e}"


async def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Replace old_string with new_string. Exact match first; whitespace-lenient fallback
    handles CRLF and trailing-space differences. old_string must be unique unless replace_all."""
    target = _resolve_sandbox(path)
    if target is None:
        return f"[error] Path is outside sandbox: {path}"
    if not target.exists():
        return f"[not found] {path} — create it with write_sandbox_file first."
    if old_string == new_string:
        return "[error] old_string and new_string are identical — nothing to change."
    try:
        content = target.read_text(errors="replace")
    except Exception as e:
        return f"[error reading file] {e}"

    note = ""
    count = content.count(old_string)
    if count == 1 or (count > 1 and replace_all):
        new_content = (
            content.replace(old_string, new_string)
            if replace_all else content.replace(old_string, new_string, 1)
        )
    elif count > 1:
        return (
            f"[error] old_string appears {count} times in {path}. Add surrounding lines to "
            f"make it unique, or pass replace_all=true to change every occurrence."
        )
    else:
        n_content, n_old = _norm_ws(content), _norm_ws(old_string)
        n_count = n_content.count(n_old)
        if n_count == 0:
            return (
                f"[error] old_string not found in {path}. Read it first with "
                f"read_file('{path}') and copy the exact text including indentation."
            )
        if n_count > 1 and not replace_all:
            return (
                f"[error] old_string matches {n_count} places after normalizing whitespace. "
                f"Add more surrounding context, or pass replace_all=true."
            )
        new_content = (
            n_content.replace(n_old, new_string)
            if replace_all else n_content.replace(n_old, new_string, 1)
        )
        note = " (whitespace-normalized match; line endings normalized to \\n)"

    try:
        target.write_text(new_content)
        return f"Edited {path} ({len(content):,} → {len(new_content):,} chars){note}"
    except Exception as e:
        return f"[error writing file] {e}"


async def list_heso_directory(path: str = "") -> str:
    target = _resolve_heso(path)
    if target is None:
        return f"[error] Path is outside the heso root: {path}"
    if not target.exists():
        return f"[not found] {path or '(root)'}"
    if not target.is_dir():
        return f"[error] Not a directory: {path}"
    try:
        return _list_dir(target)
    except Exception as e:
        return f"[error listing directory] {e}"


async def list_sandbox_directory(path: str = "") -> str:
    target = _resolve_sandbox(path)
    if target is None:
        return f"[error] Path is outside sandbox: {path}"
    if not target.exists():
        return f"[not found] {path or '(root)'}"
    try:
        return _list_dir(target)
    except Exception as e:
        return f"[error listing directory] {e}"


HANDLERS = {
    "read_heso_file":         read_heso_file,
    "read_file":              read_file,
    "write_sandbox_file":     write_sandbox_file,
    "edit_file":              edit_file,
    "list_heso_directory":    list_heso_directory,
    "list_sandbox_directory": list_sandbox_directory,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_heso_file",
            "description": "Read a file from the Heso project directory (read-only). For large files, pass offset (0-based start line) and limit (number of lines) to read just a window instead of the whole file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":   {"type": "string", "description": "Relative path from heso root"},
                    "offset": {"type": "integer", "description": "0-based line to start from. Omit to read from the top.", "default": 0},
                    "limit":  {"type": "integer", "description": "Number of lines to read. Omit to read the whole file.", "default": 0},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from your sandbox (/data). For large files (journals, soul.md, skills, reports), pass offset (0-based start line) and limit (line count) to read just the part you need instead of pulling the whole file into context. Prefer this over run_shell('cat ...').",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":   {"type": "string", "description": "Path relative to your sandbox root (/data), e.g. 'soul.md' or 'reports/foo.md'"},
                    "offset": {"type": "integer", "description": "0-based line to start from. Omit to read from the top.", "default": 0},
                    "limit":  {"type": "integer", "description": "Number of lines to read. Omit to read the whole file.", "default": 0},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Make a targeted edit to a sandbox file by replacing old_string with new_string. Use this for small changes instead of write_sandbox_file — you only emit the changed span, not the whole file. old_string must match exactly (copy it from read_file) and be unique unless replace_all=true. Falls back to whitespace-lenient matching if exact match misses.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":        {"type": "string", "description": "Path relative to sandbox root"},
                    "old_string":  {"type": "string", "description": "Exact text to find — include enough surrounding context to be unique"},
                    "new_string":  {"type": "string", "description": "Text to replace it with"},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring a unique match", "default": False},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_sandbox_file",
            "description": "Write a brand-new file to your sandbox, or fully replace an existing one. For small changes to existing files use edit_file instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_heso_directory",
            "description": "List files and directories in the Heso project.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path. '' = root."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_sandbox_directory",
            "description": "List files in your sandbox directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path. '' = root."}},
                "required": [],
            },
        },
    },
]
