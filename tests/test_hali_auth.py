"""Security and session regression tests for the Hali dashboard login."""

from __future__ import annotations

import base64
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import auth_service  # noqa: E402
import auth_gateway  # noqa: E402


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


class HaliPublicAssetBoundaryTests(unittest.TestCase):
    """Real HTTP requests against synthetic files, never production secrets."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name) / "dashboard"
        cls.root.mkdir()
        (cls.root / "assets" / "icons").mkdir(parents=True)
        (cls.root / "assets" / "public.css").write_bytes(b"synthetic-public-css")
        (cls.root / "assets" / "icons" / "example.png").write_bytes(b"synthetic-icon")
        (cls.root / "login.html").write_bytes(b"synthetic-login-page")
        for name in ("internal.txt", "auth_gateway.py", "auth_service.py", "config.json"):
            (cls.root / name).write_bytes(b"SYNTHETIC-PRIVATE-FIXTURE")
        (cls.root.parent / "something").write_bytes(b"SYNTHETIC-PRIVATE-FIXTURE")
        cls.root_patch = patch.object(auth_gateway, "ROOT", cls.root)
        cls.config_patch = patch.object(auth_gateway, "get_auth_config", return_value=None)
        cls.root_patch.start()
        cls.config_patch.start()
        cls.httpd = auth_gateway.ThreadingHTTPServer(("127.0.0.1", 0), auth_gateway.HaliGateway)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.config_patch.stop()
        cls.root_patch.stop()
        cls.temp.cleanup()

    def request(self, path, *, method="GET", headers=None, body=None):
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def assert_denied(self, path):
        status, headers, body = self.request(path)
        self.assertEqual(status, 403, path)
        self.assertEqual(body, b"403", path)
        self.assertNotIn("Location", headers)
        self.assertNotIn(b"SYNTHETIC-PRIVATE-FIXTURE", body)
        self.assertNotIn(b"synthetic-login-page", body)

    def test_public_asset_without_session(self):
        status, headers, body = self.request("/assets/public.css")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"synthetic-public-css")
        self.assertIn("text/css", headers["Content-Type"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_nested_public_asset(self):
        status, _, body = self.request("/assets/icons/example.png")
        self.assertEqual((status, body), (200, b"synthetic-icon"))

    def test_public_asset_query_and_encoded_normal_separator(self):
        self.assertEqual(self.request("/assets/public.css?v=1")[0], 200)
        self.assertEqual(self.request("/assets/icons%2fexample.png")[2], b"synthetic-icon")

    def test_missing_public_asset_is_404(self):
        self.assertEqual(self.request("/assets/missing.css")[0], 404)

    def test_parent_traversal(self):
        self.assert_denied("/assets/../internal.txt")

    def test_repository_parent_traversal(self):
        self.assert_denied("/assets/../../something")

    def test_nested_traversal(self):
        self.assert_denied("/assets/sub/../../internal.txt")

    def test_parent_directory_itself(self):
        self.assert_denied("/assets/..")

    def test_auth_files_are_not_public_assets(self):
        for filename in ("auth_gateway.py", "auth_service.py", "config.json"):
            with self.subTest(filename=filename):
                self.assert_denied("/assets/../" + filename)

    def test_login_sibling_not_served_through_assets(self):
        self.assert_denied("/assets/../login.html")

    def test_encoded_dots_lowercase(self):
        self.assert_denied("/assets/%2e%2e/internal.txt")

    def test_encoded_dots_uppercase(self):
        self.assert_denied("/assets/%2E%2E/internal.txt")

    def test_encoded_slash(self):
        self.assert_denied("/assets/%2e%2e%2finternal.txt")

    def test_encoded_backslash(self):
        self.assert_denied("/assets/%2e%2e%5cinternal.txt")

    def test_literal_windows_backslash(self):
        self.assert_denied("/assets/..\\internal.txt")

    def test_absolute_and_windows_drive_paths(self):
        for path in ("/assets/%2finternal.txt", "/assets/%5c%5cserver%5cfile",
                     "/assets/C:%5cinternal.txt", "/assets/public.css:stream"):
            with self.subTest(path=path):
                self.assert_denied(path)

    def test_double_encoded_traversal_fails_closed(self):
        self.assert_denied("/assets/%252e%252e%252finternal.txt")

    def test_invalid_encoding_and_windows_aliases(self):
        for path in ("/assets/%FF", "/assets/%00internal.txt", "/assets/%2e%2e%20/internal.txt",
                     "/assets/icons/%2e%2e/%2e%2e/internal.txt"):
            with self.subTest(path=path):
                self.assert_denied(path)

    def test_symlink_escape(self):
        link = self.root / "assets" / "outside"
        junction = False
        try:
            link.symlink_to(self.root.parent, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            if os.name != "nt":
                self.skipTest("OS does not permit synthetic symlink creation: " + type(error).__name__)
            # Windows junctions exercise the same resolve() boundary without
            # requiring the symlink privilege. Both targets are synthetic.
            command = "New-Item -ItemType Junction -Path '{}' -Value '{}' -ErrorAction Stop | Out-Null".format(
                str(link).replace("'", "''"), str(self.root.parent).replace("'", "''"))
            encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
            result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                    capture_output=True, timeout=10)
            if result.returncode:
                self.skipTest("OS does not permit synthetic symlink/junction creation")
            junction = True
        try:
            self.assert_denied("/assets/outside/dashboard/internal.txt")
        finally:
            if junction:
                link.rmdir()
            else:
                link.unlink()

    def test_login_stays_public_but_direct_login_html_is_not(self):
        status, _, body = self.request("/login")
        self.assertEqual((status, body), (200, b"synthetic-login-page"))
        status, headers, _ = self.request("/login.html")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/login")

    def test_auth_status_stays_public_without_identity(self):
        status, _, body = self.request("/api/auth/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"configured": False, "authenticated": False, "user": None})

    def test_internal_routes_still_require_auth(self):
        for path in ("/api/status", "/api/chart", "/api/health"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 401)
        for path in ("/", "/manifest.webmanifest", "/internal.txt", "/auth_gateway.py"):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 302)
                self.assertEqual(headers["Location"], "/login")
                self.assertNotIn(b"SYNTHETIC-PRIVATE-FIXTURE", body)

    def test_post_routes_do_not_gain_public_asset_exception(self):
        self.assertEqual(self.request("/assets/public.css", method="POST")[0], 401)
        self.assertEqual(self.request("/api/status", method="POST")[0], 401)
        self.assertEqual(self.request("/api/logout", method="POST")[0], 200)

    def test_webhook_keeps_backend_token_authorization_not_session_auth(self):
        class TokenBackend(auth_gateway.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                allowed = (self.path == "/api/tradingview/webhook"
                           and self.headers.get("Authorization") == "Bearer synthetic-test-only"
                           and body == b'{}')
                self.send_response(200 if allowed else 401)
                self.send_header("Content-Length", "0")
                self.end_headers()

        backend = auth_gateway.ThreadingHTTPServer(("127.0.0.1", 0), TokenBackend)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(auth_gateway, "BACKEND_PORT", backend.server_address[1]):
                self.assertEqual(self.request("/api/tradingview/webhook", method="POST", body=b'{}')[0], 401)
                self.assertEqual(self.request("/api/tradingview/webhook", method="POST", body=b'{}',
                    headers={"Authorization": "Bearer synthetic-test-only"})[0], 200)
        finally:
            backend.shutdown()
            backend.server_close()
            thread.join(timeout=5)

    def test_head_contract_does_not_expose_assets_or_internal_files(self):
        self.assertEqual(self.request("/login", method="HEAD")[0], 200)
        self.assertEqual(self.request("/api/auth/status", method="HEAD")[0], 200)
        status, _, body = self.request("/assets/../internal.txt", method="HEAD")
        self.assertEqual(status, 302)  # HEAD assets were never public in this contract.
        self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
