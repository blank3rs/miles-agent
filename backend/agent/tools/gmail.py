"""Gmail: credentials cache, send (with sensitive-data guard), read."""
import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path

import structlog

from agent.config import AKSHAY_EMAIL, DATA_DIR, EMAIL_ADDRESS, SANDBOX_ROOT

log = structlog.get_logger()

# Anti-spam: cap how many emails Miles can send the same external recipient in a rolling
# 24h window, so a bad loop (or bad judgment) can't blast someone. Akshay is exempt.
_SEND_LOG_FILE = DATA_DIR / "email_send_log.json"
_MAX_PER_RECIPIENT_24H = int(os.getenv("EMAIL_MAX_PER_RECIPIENT_24H", "4"))


def _norm_recipients(to: str) -> list[str]:
    return [r.strip().lower() for r in re.split(r"[,;]", to or "") if r.strip()]


def _read_send_log() -> dict:
    try:
        return json.loads(_SEND_LOG_FILE.read_text()) if _SEND_LOG_FILE.exists() else {}
    except Exception:
        return {}


def _recipient_over_cap(recips: list[str]) -> str | None:
    """Return the first recipient that's at/over the 24h cap, else None. Akshay exempt."""
    cutoff = time.time() - 86400
    log_data = _read_send_log()
    akshay = (AKSHAY_EMAIL or "").lower()
    for r in recips:
        if r == akshay:
            continue
        if len([t for t in log_data.get(r, []) if t > cutoff]) >= _MAX_PER_RECIPIENT_24H:
            return r
    return None


def _record_send(recips: list[str]) -> None:
    cutoff = time.time() - 86400
    log_data = _read_send_log()
    now = time.time()
    for r in recips:
        log_data[r] = [t for t in log_data.get(r, []) if t > cutoff] + [now]
    try:
        _SEND_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SEND_LOG_FILE.write_text(json.dumps(log_data))
    except Exception:
        pass

_google_creds_cache: dict = {"creds": None, "expires_at": 0.0}


def _get_google_creds():
    # Cache credentials for 45 minutes to avoid refreshing on every tool call
    now = time.time()
    cached = _google_creds_cache["creds"]
    if cached is not None and now < _google_creds_cache["expires_at"]:
        return cached

    creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "/data/google_credentials.json")
    if not Path(creds_file).exists():
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        with open(creds_file) as f:
            data = json.load(f)
        creds = Credentials(
            token=None,
            refresh_token=data["refresh_token"],
            client_id=data["client_id"],
            client_secret=data["client_secret"],
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        )
        creds.refresh(Request())
        _google_creds_cache["creds"] = creds
        _google_creds_cache["expires_at"] = now + 2700  # 45 minutes
        return creds
    except Exception as e:
        log.warning("google_creds_failed", err=str(e))
        _google_creds_cache["creds"] = None
        return None


def _get_google_service(api: str, version: str):
    creds = _get_google_creds()
    if not creds:
        return None
    try:
        import googleapiclient.discovery
        return googleapiclient.discovery.build(api, version, credentials=creds, cache_discovery=False)
    except Exception as e:
        log.warning("google_service_failed", api=api, err=str(e))
        return None


def _decode_part(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "==").decode(errors="replace")


def _collect_bodies(payload: dict, acc: dict | None = None) -> dict:
    """Walk the MIME tree, capturing the first text/plain and first text/html parts."""
    acc = acc if acc is not None else {"plain": "", "html": ""}
    mime = payload.get("mimeType", "")
    if mime == "text/plain" and not acc["plain"]:
        acc["plain"] = _decode_part(payload)
    elif mime == "text/html" and not acc["html"]:
        acc["html"] = _decode_part(payload)
    for part in payload.get("parts", []):
        _collect_bodies(part, acc)
    return acc


def _extract_gmail_body(payload: dict) -> str:
    bodies = _collect_bodies(payload)
    if bodies["plain"].strip():
        return bodies["plain"]
    if bodies["html"].strip():
        # HTML-only email (common for marketing/transactional) — convert to readable
        # text instead of handing Miles a wall of tags.
        try:
            from markdownify import markdownify
            return markdownify(bodies["html"])
        except Exception:
            return re.sub(r"<[^>]+>", " ", bodies["html"])
    return ""


# Secrets must never leave the system in an email. This guard is a hard stop,
# independent of whatever the model intends. It targets things that are *actually*
# secret — card numbers and labeled/prefixed credentials — and deliberately avoids
# bare-entropy catch-alls (git SHAs, UUIDs, long URLs, quoted tracking links all
# tripped the old `[0-9a-f]{32,}` / `[A-Za-z0-9+/]{40,}` rules and silently blocked
# legitimate mail).
_SENSITIVE_PATTERNS = [
    # Credit/debit card numbers (16 digits with optional spaces or dashes)
    r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b',
    # CVC / CVV codes labeled inline
    r'\b(cvc|cvv|cvc2|cvv2|security\s?code)[:\s]+\d{3,4}\b',
    # A credential/secret presented with a label: "api_key: ...", "password=..."
    r'\b(api[_-]?key|secret|client[_-]?secret|access[_-]?token|refresh[_-]?token|'
    r'token|password|passwd|bearer|authorization)\b\s*[:=]\s*\S{12,}',
    # Well-known key prefixes
    r'\bsk-[A-Za-z0-9]{20,}\b',          # OpenAI-style
    r'\bAKIA[0-9A-Z]{16}\b',             # AWS access key id
    r'\bghp_[A-Za-z0-9]{36}\b',          # GitHub personal access token
    r'\bAIza[0-9A-Za-z\-_]{30,}\b',      # Google API key
]
_SENSITIVE_RE = re.compile('|'.join(_SENSITIVE_PATTERNS), re.IGNORECASE)


def _contains_sensitive_data(text: str) -> bool:
    return bool(_SENSITIVE_RE.search(text))


# Render the model's markdown into clean email HTML so recipients see formatted
# text, not raw `**bold**` / `## headers` / backticks. One wrapper div with inline
# styling (Gmail strips <style>/<head>); the body is self-authored, so no sanitizer.
_EMAIL_CSS = (
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    "font-size:15px;line-height:1.55;color:#1a1a1a;"
)


def _render_email_html(markdown_body: str) -> str:
    from markdown_it import MarkdownIt
    md = MarkdownIt("commonmark", {"linkify": True, "breaks": True}).enable("table")
    return f'<div style="{_EMAIL_CSS}">{md.render(markdown_body)}</div>'


async def send_email(to: str, subject: str, body: str, attachments: list | None = None) -> str:
    import mimetypes
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email import encoders as email_encoders

    from agent import audit
    recipients = _norm_recipients(to)
    capped = _recipient_over_cap(recipients)
    if capped:
        audit.record("send_email", target=to, decision="blocked", reason="anti-spam cap",
                     params={"subject": subject})
        return (f"[blocked — anti-spam] You've already emailed {capped} {_MAX_PER_RECIPIENT_24H} times in the "
                "last 24h. Don't keep emailing the same person — wait for a reply, or if it's genuinely urgent "
                "loop in Akshay. This guard prevents accidental spamming.")

    if _contains_sensitive_data(body) or _contains_sensitive_data(subject):
        return "[blocked] Email body or subject contains what looks like sensitive data (card number, API key, or credential). Remove it before sending."

    # Strip AI-writing tells before it goes out (runs AFTER the secret check, so nothing
    # sensitive is sent to the rewrite model). Clean emails skip the LLM pass.
    from agent.style import polish_email_body
    body = await polish_email_body(body)

    # text/plain (raw markdown reads fine as plaintext) + text/html (rendered) so the
    # email looks like a person wrote it, in every client, with a graceful fallback.
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body, "plain", "utf-8"))
    try:
        alt.attach(MIMEText(_render_email_html(body), "html", "utf-8"))
    except Exception as e:
        log.warning("email_html_render_failed", err=str(e))  # fall back to plaintext-only

    attached = []
    attachment_parts = []
    for path_str in (attachments or []):
        try:
            p = Path(path_str) if Path(path_str).is_absolute() else SANDBOX_ROOT / path_str
            if not p.exists():
                attached.append(f"[skipped: not found] {path_str}")
                continue
            mime_type, _ = mimetypes.guess_type(str(p))
            main_type, sub_type = (mime_type or "application/octet-stream").split("/", 1)
            part = MIMEBase(main_type, sub_type)
            part.set_payload(p.read_bytes())
            email_encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=p.name)
            attachment_parts.append(part)
            attached.append(p.name)
        except Exception as e:
            attached.append(f"[attachment error: {path_str}] {e}")

    # multipart/mixed only when there are files; otherwise the alternative IS the message.
    if attachment_parts:
        msg = MIMEMultipart("mixed")
        msg.attach(alt)
        for part in attachment_parts:
            msg.attach(part)
    else:
        msg = alt
    msg["From"]    = EMAIL_ADDRESS
    msg["To"]      = to
    msg["Subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    def _send():
        svc = _get_google_service("gmail", "v1")
        if not svc:
            raise RuntimeError("Google credentials not found. Set GOOGLE_CREDENTIALS_FILE in .env")
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()

    try:
        await asyncio.to_thread(_send)
        _record_send(recipients)  # count only successful sends toward the anti-spam cap
        audit.record("send_email", target=to, decision="allowed", reason=subject[:80])
        result = f"Sent from {EMAIL_ADDRESS} to {to} — {subject}"
        if attached:
            result += f"\nAttachments: {', '.join(attached)}"
        return result
    except Exception as e:
        return f"[email send failed] {e}"


async def read_emails(count: int = 10, unread_only: bool = False) -> str:
    def _fetch():
        svc = _get_google_service("gmail", "v1")
        if not svc:
            return None
        q = "is:unread in:inbox" if unread_only else "in:inbox"
        result = svc.users().messages().list(userId="me", maxResults=count, q=q).execute()
        messages = result.get("messages", [])
        if not messages:
            return []
        emails = []
        for m in messages:
            try:
                msg = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
                headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
                body_text = _extract_gmail_body(msg["payload"])[:3000]
                emails.append(
                    f"From: {headers.get('From', '?')}\n"
                    f"Subject: {headers.get('Subject', '?')}\n"
                    f"Date: {headers.get('Date', '?')}\n\n{body_text.strip()}\n---"
                )
            except Exception as e:
                emails.append(f"[error reading message {m['id']}] {e}\n---")
        return emails

    try:
        emails = await asyncio.to_thread(_fetch)
        if emails is None:
            return "[email not configured] Google credentials not found. Set GOOGLE_CREDENTIALS_FILE in .env"
        return "\n\n".join(emails) if emails else "(inbox empty)"
    except Exception as e:
        return f"[email fetch failed] {e}"


HANDLERS = {
    "send_email":  send_email,
    "read_emails": read_emails,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send an email as Miles. Can attach files — screenshots, PDFs, videos, any file in your sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to":          {"type": "string"},
                    "subject":     {"type": "string"},
                    "body":        {"type": "string"},
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of file paths to attach (absolute or relative to sandbox root)",
                    },
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_emails",
            "description": "Read emails from the inbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count":       {"type": "integer", "default": 10},
                    "unread_only": {"type": "boolean", "default": False},
                },
            },
        },
    },
]
