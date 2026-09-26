"""Authenticated local gateway for the Hali dashboard.

Caddy should reverse-proxy HTTPS traffic to 127.0.0.1:8788. This gateway
serves the custom login, validates signed sessions, and forwards authenticated
traffic to the existing dashboard server on 127.0.0.1:8787.

No passwords or session secrets belong in the repository.
"""

from __future__ import annotations

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from auth_service import (
    COOKIE_NAME,
    clear_session_cookie,
    create_session_token,
    default_config_path,
    load_auth_config,
    parse_cookie,
    session_cookie,
    verify_password,
    verify_session_token,
)


ROOT = Path(__file__).resolve().parent
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8787
GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 8788

STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

CONFIG_CACHE = None
CONFIG_MTIME = None
CONFIG_LOCK = threading.Lock()

LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_ATTEMPTS = 8
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_LOCK = threading.Lock()

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def get_auth_config():
    """Read auth config with mtime caching. Missing/invalid config fails closed."""
    global CONFIG_CACHE, CONFIG_MTIME
    path = default_config_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = None
    with CONFIG_LOCK:
        if CONFIG_MTIME == mtime:
            return CONFIG_CACHE
        CONFIG_CACHE = load_auth_config(path) if mtime is not None else None
        CONFIG_MTIME = mtime
        return CONFIG_CACHE


def rate_limited(client_key: str, *, success: bool = False) -> bool:
    now = time.time()
    with LOGIN_LOCK:
        recent = [
            stamp
            for stamp in LOGIN_ATTEMPTS.get(client_key, [])
            if now - stamp < LOGIN_WINDOW_SECONDS
        ]
        if success:
            LOGIN_ATTEMPTS.pop(client_key, None)
            return False
        if len(recent) >= LOGIN_MAX_ATTEMPTS:
            LOGIN_ATTEMPTS[client_key] = recent
            return True
        recent.append(now)
        LOGIN_ATTEMPTS[client_key] = recent
        return False


class HaliGateway(BaseHTTPRequestHandler):
    server_version = "HaliGateway/1.0"

    def log_message(self, fmt, *args):
        return

    def security_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )

    def send_bytes(self, code: int, content_type: str, payload: bytes, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.security_headers()
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def send_json(self, code: int, value: dict, headers=None):
        self.send_bytes(
            code,
            "application/json; charset=utf-8",
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            headers,
        )

    def redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def client_key(self) -> str:
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
        return forwarded or (self.client_address[0] if self.client_address else "unknown")

    def current_user(self):
        config = get_auth_config()
        if not config:
            return None
        token = parse_cookie(self.headers.get("Cookie"), COOKIE_NAME)
        return verify_session_token(token, config)

    def route(self) -> str:
        return urlsplit(self.path).path

    def serve_local_asset(self, route: str) -> bool:
        relative = route.lstrip("/")
        target = (ROOT / relative).resolve()
        try:
            target.relative_to(ROOT.resolve())
        except ValueError:
            self.send_bytes(403, "text/plain; charset=utf-8", b"403")
            return True
        if not target.is_file():
            self.send_bytes(404, "text/plain; charset=utf-8", b"404")
            return True
        ctype = STATIC_CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self.send_bytes(200, ctype, target.read_bytes())
        return True

    def read_body(self, maximum: int = 1024 * 1024) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 0 or length > maximum:
            return None
        return self.rfile.read(length) if length else b""

    def proxy(self, *, allow_authorization: bool = False):
        body = self.read_body()
        if body is None:
            self.send_bytes(413, "text/plain; charset=utf-8", b"request too large")
            return

        headers = {}
        for name, value in self.headers.items():
            lower = name.lower()
            if lower in HOP_BY_HOP or lower in {"host", "cookie", "content-length"}:
                continue
            if lower == "authorization" and not allow_authorization:
                continue
            headers[name] = value
        headers["Host"] = f"{BACKEND_HOST}:{BACKEND_PORT}"
        if body:
            headers["Content-Length"] = str(len(body))

        conn = HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=20)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            payload = response.read()
            passthrough = {}
            for name, value in response.getheaders():
                lower = name.lower()
                if lower in HOP_BY_HOP or lower in {"content-length", "server", "date"}:
                    continue
                passthrough[name] = value
            content_type = passthrough.pop("Content-Type", "application/octet-stream")
            self.send_bytes(response.status, content_type, payload, passthrough)
        except OSError:
            self.send_json(502, {"ok": False, "error": "HALI_BACKEND_UNAVAILABLE"})
        finally:
            conn.close()

    def do_HEAD(self):
        route = self.route()
        user = self.current_user()

        if route == "/login":
            if user:
                self.send_response(302)
                self.send_header("Location", "/")
                self.security_headers()
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            target = (ROOT / "login.html").resolve()
            if not target.is_file():
                self.send_response(404)
                self.security_headers()
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.security_headers()
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
            return

        if route == "/api/auth/status":
            payload = json.dumps(
                {
                    "configured": bool(get_auth_config()),
                    "authenticated": bool(user),
                    "user": (
                        {"username": user.get("u"), "display_name": user.get("d")}
                        if user
                        else None
                    ),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.security_headers()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return

        if not user:
            self.send_response(401 if route.startswith("/api/") else 302)
            if not route.startswith("/api/"):
                self.send_header("Location", "/login")
            self.security_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.proxy()

    def do_GET(self):
        route = self.route()
        user = self.current_user()

        if route == "/api/auth/status":
            config = get_auth_config()
            self.send_json(
                200,
                {
                    "configured": bool(config),
                    "authenticated": bool(user),
                    "user": (
                        {"username": user.get("u"), "display_name": user.get("d")}
                        if user
                        else None
                    ),
                },
            )
            return

        if route == "/login":
            if user:
                self.redirect("/")
                return
            self.serve_local_asset("/login.html")
            return

        if route.startswith("/assets/"):
            self.serve_local_asset(route)
            return

        if not user:
            if route.startswith("/api/"):
                self.send_json(401, {"authenticated": False, "error": "AUTH_REQUIRED"})
            else:
                self.redirect("/login")
            return

        self.proxy()

    def do_POST(self):
        route = self.route()

        if route == "/api/login":
            config = get_auth_config()
            if not config:
                self.send_json(503, {"ok": False, "error": "AUTH_NOT_CONFIGURED"})
                return
            key = self.client_key()
            if rate_limited(key):
                self.send_json(
                    429,
                    {"ok": False, "error": "RATE_LIMITED"},
                    {"Retry-After": str(LOGIN_WINDOW_SECONDS)},
                )
                return
            if self.headers.get_content_type() != "application/json":
                self.send_json(400, {"ok": False, "error": "INVALID_REQUEST"})
                return
            body = self.read_body(8192)
            if body is None:
                self.send_json(413, {"ok": False, "error": "REQUEST_TOO_LARGE"})
                return
            try:
                payload = json.loads(body.decode("utf-8"))
                username = str(payload.get("username") or "").strip().lower()
                password = str(payload.get("password") or "")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                self.send_json(400, {"ok": False, "error": "INVALID_REQUEST"})
                return

            record = config.users.get(username)
            if not isinstance(record, dict) or not verify_password(password, record):
                self.send_json(401, {"ok": False, "error": "INVALID_CREDENTIALS"})
                return

            rate_limited(key, success=True)
            display = str(record.get("display_name") or username)
            token = create_session_token(username, display, config.session_secret)
            self.send_json(
                200,
                {"ok": True, "user": {"username": username, "display_name": display}},
                {"Set-Cookie": session_cookie(token)},
            )
            return

        if route == "/api/logout":
            self.send_json(200, {"ok": True}, {"Set-Cookie": clear_session_cookie()})
            return

        # TradingView is an external machine-to-machine endpoint. Its existing
        # bearer/token validation remains authoritative and does not use a web session.
        if route == "/api/tradingview/webhook":
            self.proxy(allow_authorization=True)
            return

        if not self.current_user():
            self.send_json(401, {"authenticated": False, "error": "AUTH_REQUIRED"})
            return

        self.proxy()


if __name__ == "__main__":
    config = get_auth_config()
    state = "configured" if config else f"NOT configured ({default_config_path()})"
    print(f"Hali auth gateway: http://{GATEWAY_HOST}:{GATEWAY_PORT} | auth={state}")
    ThreadingHTTPServer((GATEWAY_HOST, GATEWAY_PORT), HaliGateway).serve_forever()
