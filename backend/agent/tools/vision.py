"""Vision: screenshots and image/video analysis via the vision model."""
import asyncio
import base64
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from agent.config import AZURE_API_KEY, AZURE_ENDPOINT, MODEL, SCREENSHOTS_DIR


def cleanup_screenshots(max_age_seconds: int = 3600) -> None:
    """Delete screenshots older than max_age_seconds (default 1 hour)."""
    if not SCREENSHOTS_DIR.exists():
        return
    cutoff = time.time() - max_age_seconds
    for f in SCREENSHOTS_DIR.glob("*.png"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass


def _vision_client():
    from openai import OpenAI
    return OpenAI(base_url=AZURE_ENDPOINT, api_key=AZURE_API_KEY)


def _image_to_b64(path_or_url: str) -> tuple[str, str]:
    """Returns (base64_string, mime_type). Accepts file path or URL."""
    import mimetypes
    if path_or_url.startswith(("http://", "https://")):
        import httpx
        resp = httpx.get(path_or_url, follow_redirects=True, timeout=20)
        resp.raise_for_status()
        data = resp.content
        mime = resp.headers.get("content-type", "image/jpeg").split(";")[0]
    else:
        p = Path(path_or_url)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {path_or_url}")
        data = p.read_bytes()
        mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
    return base64.b64encode(data).decode("utf-8"), mime


async def take_screenshot(region: str = "full") -> str:
    """Capture the screen and save to sandbox. Returns file path."""
    cleanup_screenshots()
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        try:
            import mss
            import mss.tools
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[0])
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                img_path = SCREENSHOTS_DIR / f"screenshot_{ts}.png"
                mss.tools.to_png(shot.rgb, shot.size, output=str(img_path))
                return str(img_path)
        except ImportError:
            pass

        # fallback: macOS screencapture CLI
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        img_path = SCREENSHOTS_DIR / f"screenshot_{ts}.png"
        result = await asyncio.to_thread(
            subprocess.run,
            ["screencapture", "-x", str(img_path)],
            capture_output=True,
        )
        if result.returncode == 0 and img_path.exists():
            return str(img_path)
        return f"[screenshot failed] screencapture exit code {result.returncode}"
    except Exception as e:
        return f"[screenshot failed] {e}"


async def analyze_image(path_or_url: str, question: str = "Describe this image in detail.") -> str:
    """Analyze an image (file path or URL) using the vision model."""
    try:
        b64, mime = await asyncio.to_thread(_image_to_b64, path_or_url)
        client = _vision_client()
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
            max_tokens=2048,
        )
        return resp.choices[0].message.content or "(no response)"
    except Exception as e:
        return f"[image analysis failed] {e}"


async def analyze_screenshot(question: str = "Describe what is on screen.") -> str:
    """Take a screenshot and immediately analyze it."""
    try:
        path = await take_screenshot()
        if path.startswith("["):
            return path  # error from take_screenshot
        return await analyze_image(path, question)
    except Exception as e:
        return f"[analyze_screenshot failed] {e}"


async def analyze_video(path_or_url: str, question: str = "Describe what happens in this video.") -> str:
    """Analyze a video by extracting up to 8 frames with ffmpeg and sending them
    to the vision model as images."""
    import shutil
    import tempfile

    tmp_download = None
    frames_dir = None
    try:
        video_path = path_or_url
        if path_or_url.startswith(("http://", "https://")):
            import httpx
            tmp_download = tempfile.mkdtemp()
            video_path = os.path.join(tmp_download, "video.mp4")
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                resp = await client.get(path_or_url)
                resp.raise_for_status()
                Path(video_path).write_bytes(resp.content)
        elif not Path(video_path).exists():
            return f"[video not found] {path_or_url}"

        frames_dir = tempfile.mkdtemp()
        r = await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg", "-i", video_path,
                "-vf", "select=not(mod(n\\,30)),scale=640:-1",
                "-vframes", "8", "-q:v", "3",
                f"{frames_dir}/frame_%02d.jpg",
            ],
            capture_output=True, text=True,
        )
        frames = sorted(Path(frames_dir).glob("*.jpg"))

        if not frames:
            await asyncio.to_thread(
                subprocess.run,
                ["ffmpeg", "-i", video_path, "-vf", "fps=1,scale=640:-1",
                 "-vframes", "8", "-q:v", "3", f"{frames_dir}/frame_b_%02d.jpg"],
                capture_output=True, text=True,
            )
            frames = sorted(Path(frames_dir).glob("*.jpg"))

        if not frames:
            return f"[video analysis failed] ffmpeg produced no frames. stderr: {r.stderr[:300]}"

        content: list = [{
            "type": "text",
            "text": f"{question}\n\nThese are {len(frames)} frames extracted in sequence from the video.",
        }]
        for frame in frames[:8]:
            b64 = base64.b64encode(frame.read_bytes()).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })

        client = _vision_client()
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODEL,
            messages=[{"role": "user", "content": content}],
            max_tokens=2048,
        )
        return resp.choices[0].message.content or "(no response)"

    except FileNotFoundError:
        return "[video analysis failed] ffmpeg not found. Install it: run_shell('apt-get install -y ffmpeg')."
    except Exception as e:
        return f"[video analysis failed] {e}"
    finally:
        for d in [tmp_download, frames_dir]:
            if d:
                shutil.rmtree(d, ignore_errors=True)


HANDLERS = {
    "take_screenshot":    take_screenshot,
    "analyze_image":      analyze_image,
    "analyze_screenshot": analyze_screenshot,
    "analyze_video":      analyze_video,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Capture the current screen and save it as a PNG. Returns the file path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {"type": "string", "description": "Screen region to capture. Use 'full' for the entire screen.", "default": "full"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Analyze an image (local file path or URL) using vision. Ask any question about it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path_or_url": {"type": "string", "description": "Local file path or https:// URL"},
                    "question":    {"type": "string", "description": "What to ask about the image", "default": "Describe this image in detail."},
                },
                "required": ["path_or_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_screenshot",
            "description": "Take a screenshot and immediately analyze it. Useful for seeing what's currently on screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "What to ask about the screen", "default": "Describe what is on screen."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_video",
            "description": "Analyze a video file (local path or URL). Extracts frames and runs vision analysis. Supports mp4, mov, avi, webm.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path_or_url": {"type": "string", "description": "Local file path or https:// URL"},
                    "question":    {"type": "string", "description": "What to ask about the video", "default": "Describe what happens in this video."},
                },
                "required": ["path_or_url"],
            },
        },
    },
]
