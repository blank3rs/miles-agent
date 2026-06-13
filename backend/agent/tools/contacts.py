"""SignalHire: verified contact lookup (email/phone from LinkedIn URL or email)."""
import asyncio
import json
import time

from agent.config import KEYRING_SERVICE


def _sh_key() -> str | None:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, "signalhire_api_key")
    except Exception:
        return None


async def signalhire_credits() -> str:
    key = await asyncio.to_thread(_sh_key)
    if not key:
        return "[signalhire] No API key. Use store_secret('signalhire_api_key', '<key>') first."

    def _check():
        import httpx
        r = httpx.get("https://www.signalhire.com/api/v1/credits", headers={"apikey": key}, timeout=10)
        return r.headers.get("X-Credits-Left", "?"), r.status_code, r.text

    try:
        left, code, text = await asyncio.to_thread(_check)
        if code != 200:
            return f"[signalhire error {code}] {text}"
        return f"SignalHire credits remaining: {left}"
    except Exception as e:
        return f"[signalhire credits failed] {e}"


async def signalhire_find_contact(identifier: str, wait_seconds: int = 45) -> str:
    """Async SignalHire search delivered via a throwaway webhook.site inbox we poll."""
    key = await asyncio.to_thread(_sh_key)
    if not key:
        return "[signalhire] No API key. Use store_secret('signalhire_api_key', '<key>') first."

    def _request():
        import httpx

        token_resp = httpx.post("https://webhook.site/token", timeout=10)
        token_resp.raise_for_status()
        token_uuid = token_resp.json()["uuid"]

        sh = httpx.post(
            "https://www.signalhire.com/api/v1/candidate/search",
            headers={"apikey": key, "Content-Type": "application/json"},
            json={"items": [identifier], "callbackUrl": f"https://webhook.site/{token_uuid}"},
            timeout=15,
        )
        if sh.status_code not in (200, 201):
            return f"[signalhire error {sh.status_code}] {sh.text}"

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            time.sleep(4)
            poll = httpx.get(
                f"https://webhook.site/token/{token_uuid}/requests",
                params={"sorting": "newest", "per_page": 1},
                timeout=10,
            )
            if poll.status_code == 200:
                data = poll.json().get("data", [])
                if data:
                    content = data[0].get("content", "")
                    try:
                        return json.dumps(json.loads(content), indent=2)
                    except Exception:
                        return content
        return "(no result in time — SignalHire may still be processing)"

    try:
        return await asyncio.to_thread(_request)
    except Exception as e:
        return f"[signalhire find_contact failed] {e}"


HANDLERS = {
    "signalhire_credits":      signalhire_credits,
    "signalhire_find_contact": signalhire_find_contact,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "signalhire_credits",
            "description": "Check how many SignalHire API credits you have remaining.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "signalhire_find_contact",
            "description": "Find verified email and phone for a person using SignalHire. Pass a LinkedIn URL, email, or phone number. Costs 1 credit per found contact.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier":   {"type": "string", "description": "LinkedIn profile URL, email, or phone of the person to look up"},
                    "wait_seconds": {"type": "integer", "default": 45, "description": "How long to wait for results"},
                },
                "required": ["identifier"],
            },
        },
    },
]
