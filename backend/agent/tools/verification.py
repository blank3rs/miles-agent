"""Verification helpers used during signups: CAPTCHA solving and SMS codes.

These are independent of the browser engine — browser_task handles the page;
these fetch the token / code it needs from CapSolver and Twilio.
"""
import asyncio
import json
import os

from agent.config import KEYRING_SERVICE


def get_twilio_creds() -> tuple[str | None, str | None, str | None]:
    """Return (account_sid, auth_token, phone_number) from the keyring.

    Miles stores them as a single 'twilio_credentials' JSON blob
    (account_sid/auth_token/phone_number); fall back to three separate keys.
    """
    import keyring
    blob = keyring.get_password(KEYRING_SERVICE, "twilio_credentials")
    if blob:
        try:
            d = json.loads(blob)
            return (
                d.get("account_sid") or d.get("sid"),
                d.get("auth_token") or d.get("token"),
                d.get("phone_number") or d.get("from") or d.get("number"),
            )
        except Exception:
            pass
    return (
        keyring.get_password(KEYRING_SERVICE, "twilio_account_sid"),
        keyring.get_password(KEYRING_SERVICE, "twilio_auth_token"),
        keyring.get_password(KEYRING_SERVICE, "twilio_phone_number"),
    )


async def solve_captcha(url: str, captcha_type: str = "auto", site_key: str = "") -> str:
    """Solve a CAPTCHA via CapSolver. Returns the solution token to inject."""
    import keyring as _kr

    api_key = os.getenv("CAPSOLVER_API_KEY") or _kr.get_password(KEYRING_SERVICE, "CAPSOLVER_API_KEY")
    if not api_key:
        return "[solve_captcha] CAPSOLVER_API_KEY not set. Add with store_secret('CAPSOLVER_API_KEY', key)."

    _TYPE_MAP = {
        "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
        "recaptcha_v3": "ReCaptchaV3TaskProxyLess",
        "hcaptcha":     "HCaptchaTaskProxyLess",
        "turnstile":    "AntiTurnstileTaskProxyLess",
    }
    task: dict = {"type": _TYPE_MAP.get(captcha_type, "AntiTurnstileTaskProxyLess"), "websiteURL": url}
    if site_key:
        task["websiteKey"] = site_key
    if captcha_type == "recaptcha_v3":
        task["pageAction"] = "submit"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.capsolver.com/createTask",
                json={"clientKey": api_key, "task": task},
            )
            data = resp.json()
            if data.get("errorId"):
                return f"[solve_captcha] CapSolver error: {data.get('errorDescription')}"
            task_id = data["taskId"]

            for _ in range(30):
                await asyncio.sleep(3)
                result = await client.post(
                    "https://api.capsolver.com/getTaskResult",
                    json={"clientKey": api_key, "taskId": task_id},
                )
                rdata = result.json()
                if rdata.get("status") == "ready":
                    sol = rdata["solution"]
                    token = sol.get("gRecaptchaResponse") or sol.get("token") or sol.get("response", "")
                    return f"[solved] token={token}"
                if rdata.get("errorId"):
                    return f"[solve_captcha] Error: {rdata.get('errorDescription')}"

            return "[solve_captcha] Timed out waiting for solution"
    except Exception as e:
        return f"[solve_captcha failed] {e}"


async def read_sms(limit: int = 10, filter_text: str = "") -> str:
    """Read recent SMS on Miles's Twilio number — verification codes during signups."""
    account_sid, auth_token, phone_number = get_twilio_creds()
    if not account_sid or not auth_token:
        return "[read_sms] Twilio credentials not found in the keyring (twilio_credentials)."

    try:
        import httpx
        async with httpx.AsyncClient(auth=(account_sid, auth_token), timeout=30) as client:
            resp = await client.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
                params={"PageSize": limit, "To": phone_number or ""},
            )
            data = resp.json()
            messages = data.get("messages", [])
            if not messages:
                return "(no SMS messages found)"

            out = []
            for msg in messages:
                body = msg.get("body", "")
                if filter_text and filter_text.lower() not in body.lower():
                    continue
                out.append(
                    f"From: {msg.get('from')}\n"
                    f"Date: {msg.get('date_sent')}\n"
                    f"Body: {body}\n"
                )
            return "\n---\n".join(out) if out else f"(no messages matching '{filter_text}')"
    except Exception as e:
        return f"[read_sms failed] {e}"


HANDLERS = {
    "solve_captcha": solve_captcha,
    "read_sms":      read_sms,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "solve_captcha",
            "description": "Solve a CAPTCHA using CapSolver AI. Returns a token to inject into the page. Use when a signup hits a CAPTCHA challenge — solve it here, then tell browser_task the token to submit. Requires CAPSOLVER_API_KEY stored via store_secret().",
            "parameters": {
                "type": "object",
                "properties": {
                    "url":          {"type": "string", "description": "The page URL the CAPTCHA is on"},
                    "captcha_type": {"type": "string", "enum": ["recaptcha_v2", "recaptcha_v3", "hcaptcha", "turnstile", "auto"], "description": "Type of CAPTCHA"},
                    "site_key":     {"type": "string", "description": "The sitekey from the page source (data-sitekey attribute)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_sms",
            "description": "Read recent SMS messages received on Miles's Twilio phone number. Use to get verification codes sent by services during signup. Requires twilio_account_sid and twilio_auth_token in keyring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit":       {"type": "integer", "description": "Max messages to return (default 10)"},
                    "filter_text": {"type": "string", "description": "Optional keyword to filter by (e.g. 'verification', 'code')"},
                },
                "required": [],
            },
        },
    },
]
