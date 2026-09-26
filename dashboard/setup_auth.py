"""Interactive helper to create/update Hali dashboard users.

Passwords are requested with getpass so they never appear in shell history.
The generated auth file lives outside the repository by default.
"""

from __future__ import annotations

import argparse
import base64
from getpass import getpass
import json
from pathlib import Path
import secrets

from auth_service import PBKDF2_ITERATIONS, default_config_path, derive_password_hash


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def load_or_create(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if int(data.get("schema_version", 0)) == 1 and isinstance(data.get("users"), dict):
                return data
        except Exception:
            pass
    return {
        "schema_version": 1,
        "session_secret": b64e(secrets.token_bytes(48)),
        "users": {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update one Hali dashboard login.")
    parser.add_argument("--user", required=True, help="Login username, e.g. harumi")
    parser.add_argument("--display", default="", help="Display name shown in Hali")
    parser.add_argument("--config", default=str(default_config_path()), help="Auth config path")
    args = parser.parse_args()

    username = args.user.strip()
    if not username or any(ch.isspace() for ch in username):
        raise SystemExit("Username must be non-empty and contain no spaces.")

    password = getpass(f"Password for {username}: ")
    confirm = getpass("Confirm password: ")
    if password != confirm:
        raise SystemExit("Passwords do not match.")
    if len(password) < 10:
        raise SystemExit("Use at least 10 characters.")

    path = Path(args.config)
    data = load_or_create(path)
    salt = secrets.token_bytes(16)
    data["users"][username] = {
        "display_name": (args.display.strip() or username),
        "enabled": True,
        "iterations": PBKDF2_ITERATIONS,
        "salt": b64e(salt),
        "password_hash": derive_password_hash(password, salt, PBKDF2_ITERATIONS),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Hali auth updated: {path}")
    print(f"User ready: {username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
