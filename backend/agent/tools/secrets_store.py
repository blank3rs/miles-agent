"""Secrets in the OS keyring (macOS Keychain / encrypted file on Linux).

A sidecar file tracks key *names* only, so list_secret_keys never touches values.
"""
import json

from agent.config import DATA_DIR, KEYRING_SERVICE

_SECRET_META_FILE = DATA_DIR / "secret_keys.json"


def _update_meta(key: str, add: bool) -> None:
    try:
        _SECRET_META_FILE.parent.mkdir(parents=True, exist_ok=True)
        meta = json.loads(_SECRET_META_FILE.read_text()) if _SECRET_META_FILE.exists() else []
        if add and key not in meta:
            meta.append(key)
        if not add:
            meta = [k for k in meta if k != key]
        _SECRET_META_FILE.write_text(json.dumps(sorted(meta), indent=2))
    except Exception:
        pass


async def store_secret(key: str, value: str) -> str:
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, key, value)
        _update_meta(key, add=True)
        return f"Secret stored: {key}"
    except ImportError:
        return "[keyring not installed] Run: install_package('keyring')"
    except Exception as e:
        return f"[error storing secret] {e}"


async def get_secret(key: str) -> str:
    try:
        import keyring
        value = keyring.get_password(KEYRING_SERVICE, key)
        if value is None:
            return f"(no secret found for key: {key})"
        return value
    except ImportError:
        return "[keyring not installed] Run: install_package('keyring')"
    except Exception as e:
        return f"[error retrieving secret] {e}"


async def list_secret_keys() -> str:
    try:
        if not _SECRET_META_FILE.exists():
            return "(no secrets stored)"
        keys = json.loads(_SECRET_META_FILE.read_text())
        return "\n".join(f"- {k}" for k in keys) if keys else "(no secrets stored)"
    except Exception as e:
        return f"[error listing secrets] {e}"


async def delete_secret(key: str) -> str:
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, key)
        _update_meta(key, add=False)
        return f"Secret deleted: {key}"
    except ImportError:
        return "[keyring not installed] Run: install_package('keyring')"
    except Exception as e:
        return f"[error deleting secret] {e}"


HANDLERS = {
    "store_secret":     store_secret,
    "get_secret":       get_secret,
    "list_secret_keys": list_secret_keys,
    "delete_secret":    delete_secret,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "store_secret",
            "description": "Store a credential or API key in the OS keyring. Encrypted at rest. Use for passwords, tokens, API keys you need to operate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key":   {"type": "string", "description": "Name for this secret (e.g. 'github_token', 'stripe_key')"},
                    "value": {"type": "string", "description": "The secret value to store"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_secret",
            "description": "Retrieve a stored credential from the keyring by key name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Name of the secret to retrieve"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_secret_keys",
            "description": "List the names of all stored secrets. Values are never shown.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_secret",
            "description": "Delete a stored secret from the keyring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                },
                "required": ["key"],
            },
        },
    },
]
