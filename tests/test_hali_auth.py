"""Security and session regression tests for the Hali dashboard login."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import secrets
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import auth_service  # noqa: E402


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class HaliAuthServiceTests(unittest.TestCase):
    def user_record(self, secret: str = "example-passphrase-123") -> dict:
        salt = secrets.token_bytes(16)
        return {
            "display_name": "Test User",
            "enabled": True,
            "iterations": 10_000,
            "salt": b64e(salt),
            "password_hash": auth_service.derive_password_hash(secret, salt, 10_000),
        }

    def test_password_hash_verification(self):
        record = self.user_record()
        self.assertTrue(auth_service.verify_password("example-passphrase-123", record))
        self.assertFalse(auth_service.verify_password("wrong-value", record))

    def test_disabled_user_cannot_authenticate(self):
        record = self.user_record()
        record["enabled"] = False
        self.assertFalse(auth_service.verify_password("example-passphrase-123", record))

    def test_signed_session_round_trip_and_tamper_detection(self):
        secret = secrets.token_bytes(48)
        config = auth_service.AuthConfig(
            users={"tester": self.user_record()},
            session_secret=secret,
            path=Path("unused"),
        )
        token = auth_service.create_session_token(
            "tester", "Test User", secret, now=1000, ttl_seconds=3600
        )
        payload = auth_service.verify_session_token(token, config, now=1100)
        self.assertEqual(payload["u"], "tester")
        self.assertEqual(payload["d"], "Test User")

        prefix, signature = token.rsplit(".", 1)
        tampered = prefix + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
        self.assertIsNone(auth_service.verify_session_token(tampered, config, now=1100))

    def test_expired_session_is_rejected(self):
        secret = secrets.token_bytes(48)
        config = auth_service.AuthConfig(
            users={"tester": self.user_record()},
            session_secret=secret,
            path=Path("unused"),
        )
        token = auth_service.create_session_token(
            "tester", "Test User", secret, now=1000, ttl_seconds=60
        )
        self.assertIsNone(auth_service.verify_session_token(token, config, now=1060))

    def test_config_is_fail_closed_when_missing_or_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing.json"
            self.assertIsNone(auth_service.load_auth_config(missing))

            invalid = Path(temp) / "invalid.json"
            invalid.write_text('{"schema_version":1,"users":{},"session_secret":"x"}', encoding="utf-8")
            self.assertIsNone(auth_service.load_auth_config(invalid))

    def test_config_loads_users_and_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "auth.json"
            record = self.user_record()
            payload = {
                "schema_version": 1,
                "session_secret": b64e(secrets.token_bytes(48)),
                "users": {"tester": record},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            config = auth_service.load_auth_config(path)
        self.assertIsNotNone(config)
        self.assertIn("tester", config.users)
        self.assertGreaterEqual(len(config.session_secret), 32)

    def test_cookie_parser_uses_exact_cookie_name(self):
        header = "other=abc; hali_session=token-value; x=1"
        self.assertEqual(auth_service.parse_cookie(header), "token-value")
        self.assertEqual(auth_service.parse_cookie("hali_session_extra=no"), "")


if __name__ == "__main__":
    unittest.main()
