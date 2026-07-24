"""Telegram Mini App and Cloudflare Turnstile verification service."""

import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from src.config import logger


class TurnstileWebAppService:
    """Serve and validate one-time Telegram Mini App verification challenges."""

    CHALLENGE_TTL = 300
    VERIFY_ACTION = "telegram_verify"
    SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

    def __init__(self, bot_token, cache, on_verified):
        self.bot_token = bot_token
        self.cache = cache
        self.on_verified = on_verified
        self._settings = {}
        self._generation = secrets.token_urlsafe(18)
        self._server = None
        self._thread = None
        self._lock = threading.RLock()

    @staticmethod
    def validate_settings(settings):
        """Return an error string for invalid enabled settings, otherwise None."""
        if settings.get("enabled") != "enable":
            return None

        public_url = str(settings.get("public_url") or "").strip()
        parsed = urlsplit(public_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            return "Public URL must be an HTTPS URL without query parameters or fragments"
        if not str(settings.get("site_key") or "").strip():
            return "Turnstile Site Key is required"
        if not str(settings.get("secret_key") or "").strip():
            return "Turnstile Secret Key is required"
        try:
            port = int(settings.get("port", 8080))
            max_age = int(settings.get("auth_max_age", 300))
        except (TypeError, ValueError):
            return "Listen port and Telegram authorization age must be integers"
        if not 1 <= port <= 65535:
            return "Listen port must be between 1 and 65535"
        if not 30 <= max_age <= 3600:
            return "Telegram authorization age must be between 30 and 3600 seconds"
        if not str(settings.get("host") or "").strip():
            return "Listen host is required"
        return None

    def is_enabled(self):
        with self._lock:
            return self._settings.get("enabled") == "enable" and self._server is not None

    def reload(self, settings):
        """Apply settings immediately, restarting the listener only when necessary."""
        try:
            port = int(settings.get("port") or 8080)
            auth_max_age = int(settings.get("auth_max_age") or 300)
        except (TypeError, ValueError):
            return False, "Listen port and Telegram authorization age must be integers"
        normalized = {
            "enabled": settings.get("enabled", "disable"),
            "public_url": str(settings.get("public_url") or "").strip().rstrip("/"),
            "site_key": str(settings.get("site_key") or "").strip(),
            "secret_key": str(settings.get("secret_key") or "").strip(),
            "hostname": str(settings.get("hostname") or "").strip().lower(),
            "host": str(settings.get("host") or "127.0.0.1").strip(),
            "port": port,
            "auth_max_age": auth_max_age,
        }
        error = self.validate_settings(normalized)
        if error:
            return False, error

        with self._lock:
            if normalized["enabled"] != "enable":
                self._settings = normalized
                self._generation = secrets.token_urlsafe(18)
                self._stop_locked()
                return True, None

            old_listener = (
                self._settings.get("host"), self._settings.get("port"))
            new_listener = (normalized["host"], normalized["port"])
            if self._server is not None and old_listener == new_listener:
                self._settings = normalized
                self._generation = secrets.token_urlsafe(18)
                return True, None

            old_settings = self._settings
            had_server = self._server is not None
            if had_server:
                self._stop_locked()
            try:
                server = _WebAppHTTPServer(new_listener, _VerificationRequestHandler)
                server.daemon_threads = True
                server.service = self
                thread = threading.Thread(
                    target=server.serve_forever,
                    name="TurnstileWebAppServer",
                    daemon=True,
                )
                self._settings = normalized
                self._generation = secrets.token_urlsafe(18)
                self._server = server
                self._thread = thread
                thread.start()
                logger.info(
                    "Turnstile WebApp listening on http://%s:%s",
                    normalized["host"], normalized["port"],
                )
                return True, None
            except OSError as error:
                self._settings = old_settings
                if had_server:
                    self._start_rollback_server(old_settings)
                return False, f"Could not bind WebApp listener: {error}"

    def _start_rollback_server(self, settings):
        try:
            server = _WebAppHTTPServer(
                (settings["host"], int(settings["port"])),
                _VerificationRequestHandler,
            )
            server.daemon_threads = True
            server.service = self
            thread = threading.Thread(
                target=server.serve_forever,
                name="TurnstileWebAppServer",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()
        except OSError as error:
            logger.error("Failed to restore previous WebApp listener: %s", error)

    def stop(self):
        with self._lock:
            self._stop_locked()

    def _stop_locked(self):
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)

    def create_challenge(self, user_id, purpose="normal"):
        """Create a one-time challenge and return its public Mini App URL."""
        if purpose not in {"normal", "appeal"}:
            raise ValueError("Invalid verification purpose")
        with self._lock:
            if not self.is_enabled():
                raise RuntimeError("Turnstile WebApp is not enabled")
            public_url = self._settings["public_url"]
            generation = self._generation

        previous_id = self.cache.get(f"webapp_challenge_user_{user_id}")
        if previous_id:
            self.cache.delete(f"webapp_challenge_{previous_id}")

        challenge_id = secrets.token_urlsafe(32)
        challenge = {
            "user_id": int(user_id),
            "purpose": purpose,
            "created_at": int(time.time()),
            "generation": generation,
        }
        self.cache.set(
            f"webapp_challenge_{challenge_id}", challenge,
            expire=self.CHALLENGE_TTL,
        )
        self.cache.set(
            f"webapp_challenge_user_{user_id}", challenge_id,
            expire=self.CHALLENGE_TTL,
        )
        self.cache.set(
            f"captcha_{user_id}",
            {"type": "webapp", "challenge_id": challenge_id, "purpose": purpose},
            expire=self.CHALLENGE_TTL,
        )

        parsed = urlsplit(public_url)
        query = urlencode({"challenge": challenge_id})
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", query, ""))

    def _page_path(self):
        parsed = urlsplit(self._settings.get("public_url", ""))
        path = parsed.path or "/"
        return path.rstrip("/") or "/"

    def handle_page(self, challenge_id):
        with self._lock:
            settings = dict(self._settings)
        challenge = self.cache.get(f"webapp_challenge_{challenge_id}")
        if (settings.get("enabled") != "enable" or not challenge
                or challenge.get("generation") != self._generation):
            return 410, "text/html; charset=utf-8", self._expired_page(), None
        nonce = secrets.token_urlsafe(18)
        return 200, "text/html; charset=utf-8", self._render_page(
            settings["site_key"], challenge_id, self._page_path(), nonce), nonce

    def handle_verification(self, payload):
        challenge_id = str(payload.get("challenge") or "")
        init_data = str(payload.get("init_data") or "")
        turnstile_token = str(payload.get("turnstile_token") or "")
        if not challenge_id or len(challenge_id) > 128:
            return 400, {"ok": False, "message": "Invalid verification challenge"}
        if not init_data or len(init_data) > 8192:
            return 400, {"ok": False, "message": "Open verification from Telegram"}
        if not turnstile_token or len(turnstile_token) > 4096:
            return 400, {"ok": False, "message": "Turnstile verification is required"}

        with self._lock:
            settings = dict(self._settings)
        if settings.get("enabled") != "enable":
            return 503, {"ok": False, "message": "Verification is temporarily unavailable"}

        user = self.validate_telegram_init_data(init_data, settings["auth_max_age"])
        if user is None:
            return 403, {"ok": False, "message": "Telegram authorization is invalid or expired"}

        challenge_key = f"webapp_challenge_{challenge_id}"
        challenge = self.cache.get(challenge_key)
        if (not challenge or challenge.get("user_id") != user.get("id")
                or challenge.get("generation") != self._generation):
            return 410, {"ok": False, "message": "Verification challenge has expired"}

        user_id = int(user["id"])
        if not self.cache.add(
                f"webapp_submit_rate_{user_id}", True, expire=2):
            return 429, {"ok": False, "message": "Please wait before trying again"}

        turnstile_result = self.verify_turnstile(turnstile_token, settings)
        if not turnstile_result:
            return 403, {"ok": False, "message": "Human verification failed"}

        lock_key = f"webapp_challenge_lock_{challenge_id}"
        if not self.cache.add(lock_key, True, expire=30):
            return 409, {"ok": False, "message": "Verification is already being processed"}
        try:
            challenge = self.cache.get(challenge_key)
            if (not challenge or challenge.get("user_id") != user_id
                    or challenge.get("generation") != self._generation):
                return 410, {"ok": False, "message": "Verification challenge has expired"}
            ok, message = self.on_verified(
                user_id, challenge.get("purpose", "normal"), user)
            if not ok:
                return 403, {"ok": False, "message": message}

            self.cache.delete(challenge_key)
            self.cache.delete(f"webapp_challenge_user_{user_id}")
            self.cache.delete(f"captcha_{user_id}")
            self.cache.delete(f"appeal_verification_{user_id}")
            return 200, {"ok": True, "message": message or "Verification successful"}
        finally:
            self.cache.delete(lock_key)

    def validate_telegram_init_data(self, init_data, max_age):
        """Validate Telegram Mini App initData using the bot-token HMAC method."""
        try:
            pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
            values = dict(pairs)
            if len(values) != len(pairs):
                return None
            received_hash = values.pop("hash")
            auth_date = int(values["auth_date"])
            now = int(time.time())
            if auth_date > now + 30 or now - auth_date > int(max_age):
                return None
            data_check_string = "\n".join(
                f"{key}={values[key]}" for key in sorted(values))
            secret_key = hmac.new(
                b"WebAppData", self.bot_token.encode("utf-8"), hashlib.sha256
            ).digest()
            calculated_hash = hmac.new(
                secret_key, data_check_string.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(calculated_hash, received_hash):
                return None
            user = json.loads(values["user"])
            if not isinstance(user, dict) or not isinstance(user.get("id"), int):
                return None
            return user
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def verify_turnstile(self, token, settings):
        """Validate a Turnstile token with Cloudflare Siteverify."""
        try:
            response = httpx.post(
                self.SITEVERIFY_URL,
                data={
                    "secret": settings["secret_key"],
                    "response": token,
                    "idempotency_key": str(uuid.uuid4()),
                },
                timeout=5,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("Turnstile Siteverify request failed: %s", error)
            return False

        if result.get("success") is not True:
            logger.info("Turnstile rejected a token: %s", result.get("error-codes", []))
            return False
        if result.get("action") != self.VERIFY_ACTION:
            logger.warning("Turnstile action mismatch: %s", result.get("action"))
            return False
        expected_hostname = settings.get("hostname")
        if expected_hostname and str(result.get("hostname", "")).lower() != expected_hostname:
            logger.warning("Turnstile hostname mismatch: %s", result.get("hostname"))
            return False
        return True

    @staticmethod
    def _render_page(site_key, challenge_id, page_path, nonce):
        verify_path = (page_path.rstrip("/") if page_path != "/" else "") + "/verify"
        site_key_json = json.dumps(site_key)
        challenge_json = json.dumps(challenge_id)
        verify_path_json = json.dumps(verify_path)
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Verification</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style nonce="{nonce}">
:root {{ color-scheme: light dark; font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:24px; color:var(--tg-theme-text-color,#161616); background:var(--tg-theme-bg-color,#f5f6f7); }}
main {{ width:min(100%,420px); text-align:center; }}
h1 {{ margin:0 0 10px; font-size:24px; letter-spacing:0; }}
p {{ margin:0 0 24px; color:var(--tg-theme-hint-color,#707579); font-size:15px; line-height:1.5; }}
#widget {{ min-height:70px; display:flex; justify-content:center; }}
#status {{ min-height:24px; margin-top:18px; font-size:14px; }}
.success {{ color:#16854b; }} .error {{ color:#c73737; }}
</style>
<script nonce="{nonce}">
const tg = window.Telegram.WebApp;
const challenge = {challenge_json};
const siteKey = {site_key_json};
const verifyPath = {verify_path_json};
let widgetId = null;
tg.ready();
tg.expand();
function setStatus(text, kind = "") {{
  const node = document.getElementById("status");
  node.textContent = text;
  node.className = kind;
}}
async function submitVerification(token) {{
  setStatus("Checking...");
  try {{
    const response = await fetch(verifyPath, {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{challenge, init_data: tg.initData, turnstile_token: token}}),
      credentials: "same-origin"
    }});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.message || "Verification failed");
    setStatus(result.message || "Verification successful", "success");
    setTimeout(() => tg.close(), 900);
  }} catch (error) {{
    setStatus(error.message || "Verification failed", "error");
    if (window.turnstile && widgetId !== null) window.turnstile.reset(widgetId);
  }}
}}
function onTurnstileLoad() {{
  if (!tg.initData) {{ setStatus("Open this page from Telegram", "error"); return; }}
  widgetId = turnstile.render("#widget", {{
    sitekey: siteKey,
    action: "{TurnstileWebAppService.VERIFY_ACTION}",
    callback: submitVerification,
    "error-callback": () => setStatus("Could not load verification", "error"),
    "expired-callback": () => setStatus("Verification expired. Try again.", "error")
  }});
}}
</script>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onTurnstileLoad&amp;render=explicit" async defer></script>
</head>
<body><main><h1>Verify identity</h1><p>Complete the check to continue.</p><div id="widget"></div><div id="status" role="status" aria-live="polite"></div></main></body>
</html>"""

    @staticmethod
    def _expired_page():
        return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Expired</title></head><body><main><h1>Verification expired</h1><p>Return to Telegram and request a new verification.</p></main></body></html>"""


class _WebAppHTTPServer(ThreadingHTTPServer):
    """HTTP server with bounded concurrency and no reverse-DNS bind lookup."""

    allow_reuse_address = True
    request_queue_size = 64
    max_request_threads = 32

    def __init__(self, *args, **kwargs):
        self._request_slots = threading.BoundedSemaphore(self.max_request_threads)
        super().__init__(*args, **kwargs)

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class _VerificationRequestHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler with strict response headers and body limits."""

    server_version = "BetterForwardVerification/1"

    @property
    def service(self):
        return self.server.service

    def setup(self):
        super().setup()
        self.request.settimeout(10)

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        if parsed.path != self.service._page_path():
            self.send_error(404)
            return
        challenge_id = parse_qs(parsed.query).get("challenge", [""])[0]
        status, content_type, body, nonce = self.service.handle_page(challenge_id)
        self._send(status, content_type, body.encode("utf-8"), nonce=nonce)

    def do_POST(self):
        parsed = urlsplit(self.path)
        verify_path = (
            self.service._page_path().rstrip("/")
            if self.service._page_path() != "/" else ""
        ) + "/verify"
        if parsed.path != verify_path:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if length <= 0 or length > 16384:
            self.send_error(413)
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"ok": False, "message": "Invalid request"})
            return
        status, result = self.service.handle_verification(payload)
        self._send_json(status, result)

    def _send_json(self, status, payload):
        self._send(
            status, "application/json; charset=utf-8",
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )

    def _send(self, status, content_type, body, nonce=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if nonce:
            content_security_policy = (
                "default-src 'none'; "
                f"script-src 'nonce-{nonce}' https://telegram.org https://challenges.cloudflare.com; "
                f"style-src 'nonce-{nonce}'; "
                "frame-src https://challenges.cloudflare.com; "
                "connect-src 'self' https://challenges.cloudflare.com; "
                "img-src data: https://challenges.cloudflare.com"
            )
        else:
            content_security_policy = "default-src 'none'; frame-ancestors 'none'"
        self.send_header("Content-Security-Policy", content_security_policy)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        logger.debug("WebApp HTTP: " + format_string, *args)
