"""Authentication helpers for the private Hali dashboard.

Secrets and password hashes live outside the repository. The dashboard fails
closed when the auth config is missing or invalid.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from typing import Any


PBKDF2_ITERATIONS = 600_000
SESSION_TTL_SECONDS = 12 * 60 * 60
COOKIE_NAME = "hali_session"


def default_config_path() -> Path:
    configured = os.environ.get("HALI_AUTH_CONFIG", "").strip()
    if configured:
        return Path(configured)
    if os.name == "nt":
        return Path(r"C:\GoldScout\secrets\hali-auth.json")
    return Path(__file__).resolve().parent / "data" / "hali-auth.json"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def derive_password_hash(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    return _b64e(digest)


def verify_password(password: str, user_record: dict[str, Any]) -> bool:
    if not isinstance(password, str) or not isinstance(user_record, dict):
        return False
    try:
        if user_record.get("enabled", True) is False:
            return False
        salt = _b64d(str(user_record["salt"]))
        iterations = int(user_record.get("iterations", PBKDF2_ITERATIONS))
        expected = str(user_record["password_hash"])
    except (KeyError, TypeError, ValueError, base64.binascii.Error):
        return False
    actual = derive_password_hash(password, salt, iterations)
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class AuthConfig:
    users: dict[str, dict[str, Any]]
    session_secret: bytes
    path: Path


def load_auth_config(path: Path | None = None) -> AuthConfig | None:
    target = Path(path or default_config_path())
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if int(raw.get("schema_version", 0)) != 1:
            return None
        users = raw.get("users")
        secret_text = raw.get("session_secret")
        if not isinstance(users, dict) or not users or not isinstance(secret_text, str):
            return None
        secret = _b64d(secret_text)
        if len(secret) < 32:
            return None
        return AuthConfig(users=users, session_secret=secret, path=target)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, base64.binascii.Error):
        return None


def _session_payload(username: str, display_name: str, now: int, ttl_seconds: int) -> str:
    payload = {
        "u": username,
        "d": display_name,
        "iat": int(now),
        "exp": int(now + ttl_seconds),
    }
    return _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def create_session_token(
    username: str,
    display_name: str,
    secret: bytes,
    *,
    now: int | None = None,
    ttl_seconds: int = SESSION_TTL_SECONDS,
) -> str:
    issued = int(time.time() if now is None else now)
    payload = _session_payload(username, display_name, issued, ttl_seconds)
    signature = _b64e(hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def verify_session_token(
    token: str,
    config: AuthConfig,
    *,
    now: int | None = None,
) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    payload_text, signature_text = token.rsplit(".", 1)
    expected = _b64e(hmac.new(config.session_secret, payload_text.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature_text):
        return None
    try:
        payload = json.loads(_b64d(payload_text).decode("utf-8"))
        username = str(payload["u"])
        expiry = int(payload["exp"])
    except (KeyError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, base64.binascii.Error):
        return None
    current = int(time.time() if now is None else now)
    if expiry <= current:
        return None
    record = config.users.get(username)
    if not isinstance(record, dict) or record.get("enabled", True) is False:
        return None
    payload["d"] = str(record.get("display_name") or payload.get("d") or username)
    return payload


def parse_cookie(cookie_header: str | None, name: str = COOKIE_NAME) -> str:
    if not cookie_header:
        return ""
    for part in cookie_header.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value
    return ""


def session_cookie(token: str, *, max_age: int = SESSION_TTL_SECONDS) -> str:
    return (
        f"{COOKIE_NAME}={token}; Path=/; Max-Age={int(max_age)}; "
        "HttpOnly; Secure; SameSite=Lax"
    )


def clear_session_cookie() -> str:
    return f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
