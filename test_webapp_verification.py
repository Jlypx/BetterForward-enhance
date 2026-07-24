#!/usr/bin/env python3
"""Tests for Telegram Mini App and Turnstile verification."""

import hashlib
import hmac
import json
import socket
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import Mock, patch

sys.argv = [sys.argv[0], "-token", "test-token", "-group_id", "-100123"]

from diskcache import Cache
from src.bot import TGBot
from src.utils.captcha import CaptchaManager
from src.utils.webapp_verification import TurnstileWebAppService


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def signed_init_data(bot_token, user_id, auth_date=None):
    values = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "test-query",
        "user": json.dumps(
            {"id": user_id, "first_name": "Test", "username": "tester"},
            separators=(",", ":"),
        ),
    }
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(
        secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class FakeBot:
    def __init__(self):
        self.messages = []

    def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        return SimpleNamespace(message_id=len(self.messages))


class FakeDatabase:
    def __init__(self, settings):
        self.settings = settings

    def set_setting(self, key, value):
        self.settings[key] = value


class WebAppVerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cache = Cache(str(Path(self.directory.name) / "cache"))
        self.verified = []
        self.service = TurnstileWebAppService(
            "test-token", self.cache, self._on_verified)
        self.port = free_port()
        self.settings = {
            "enabled": "enable",
            "public_url": "https://verify.example.com/captcha",
            "site_key": "site-key",
            "secret_key": "secret-key",
            "hostname": "verify.example.com",
            "host": "127.0.0.1",
            "port": str(self.port),
            "auth_max_age": "300",
        }
        ok, error = self.service.reload(self.settings)
        self.assertTrue(ok, error)

    def tearDown(self):
        self.service.stop()
        self.cache.close()
        self.directory.cleanup()

    def _on_verified(self, user_id, purpose, user):
        self.verified.append((user_id, purpose, user["first_name"]))
        return True, "Verification successful"

    def test_telegram_init_data_signature_and_age(self):
        valid = signed_init_data("test-token", 123)
        self.assertEqual(
            self.service.validate_telegram_init_data(valid, 300)["id"], 123)

        tampered = valid.replace("123", "124")
        self.assertIsNone(self.service.validate_telegram_init_data(tampered, 300))

        expired = signed_init_data("test-token", 123, int(time.time()) - 301)
        self.assertIsNone(self.service.validate_telegram_init_data(expired, 300))

    def test_captcha_manager_sends_a_real_webapp_button(self):
        bot = FakeBot()
        manager = CaptchaManager(
            bot, self.cache, webapp_service=self.service)
        manager.generate_captcha(123, "webapp", purpose="normal")

        markup = bot.messages[0][1]["reply_markup"].to_dict()
        self.assertIn("web_app", markup["inline_keyboard"][0][0])
        self.assertTrue(manager.is_webapp_pending(123))

    def test_normal_webapp_verification_cannot_bypass_an_active_block(self):
        db_path = str(Path(self.directory.name) / "verification.db")
        with sqlite3.connect(db_path) as db:
            db.executescript("""
                CREATE TABLE blocked_users (
                    user_id INTEGER PRIMARY KEY,
                    block_reason TEXT,
                    blocked_until TIMESTAMP
                );
                CREATE TABLE verified_users (user_id INTEGER PRIMARY KEY);
                CREATE TABLE verification_attempts (
                    user_id INTEGER PRIMARY KEY,
                    attempt_count INTEGER
                );
                INSERT INTO blocked_users
                    (user_id, block_reason, blocked_until)
                    VALUES (123, 'rate_limit', datetime('now', '+1 hour'));
            """)

        bot = object.__new__(TGBot)
        bot.db_path = db_path
        bot.bot = FakeBot()
        bot.captcha_manager = CaptchaManager(bot.bot, self.cache)
        bot.message_handler = SimpleNamespace()
        ok, _ = bot._handle_webapp_verification(
            123, "normal", {"id": 123, "first_name": "Test"})
        self.assertFalse(ok)
        with sqlite3.connect(db_path) as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM verified_users WHERE user_id = 123").fetchone())

    def test_challenge_is_user_bound_purpose_bound_and_one_time(self):
        url = self.service.create_challenge(123, "appeal")
        challenge_id = url.split("challenge=", 1)[1]
        payload = {
            "challenge": challenge_id,
            "init_data": signed_init_data("test-token", 123),
            "turnstile_token": "valid-token",
        }
        with patch.object(self.service, "verify_turnstile", return_value=True):
            status, result = self.service.handle_verification(payload)
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])
            self.assertEqual(self.verified, [(123, "appeal", "Test")])

            status, _ = self.service.handle_verification(payload)
            self.assertEqual(status, 410)

    def test_challenge_rejects_a_different_telegram_user(self):
        url = self.service.create_challenge(123)
        challenge_id = url.split("challenge=", 1)[1]
        status, _ = self.service.handle_verification({
            "challenge": challenge_id,
            "init_data": signed_init_data("test-token", 999),
            "turnstile_token": "valid-token",
        })
        self.assertEqual(status, 410)

    def test_configuration_reload_invalidates_old_challenges(self):
        url = self.service.create_challenge(123)
        challenge_id = url.split("challenge=", 1)[1]
        ok, error = self.service.reload(dict(self.settings))
        self.assertTrue(ok, error)
        status, _, _, _ = self.service.handle_page(challenge_id)
        self.assertEqual(status, 410)

    def test_http_page_and_security_headers(self):
        url = self.service.create_challenge(123)
        challenge_id = url.split("challenge=", 1)[1]
        request_url = f"http://127.0.0.1:{self.port}/captcha?challenge={challenge_id}"
        with urllib.request.urlopen(request_url, timeout=2) as response:
            body = response.read().decode()
            self.assertIn("Verify identity", body)
            self.assertIn("Content-Security-Policy", response.headers)
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_turnstile_response_checks_action_and_hostname(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "success": True,
            "action": "telegram_verify",
            "hostname": "verify.example.com",
        }
        with patch("src.utils.webapp_verification.httpx.post", return_value=response):
            self.assertTrue(self.service.verify_turnstile("token", self.settings))

        response.json.return_value["action"] = "other_action"
        with patch("src.utils.webapp_verification.httpx.post", return_value=response):
            self.assertFalse(self.service.verify_turnstile("token", self.settings))

    def test_runtime_listener_reloads_and_can_be_disabled(self):
        new_port = free_port()
        updated = dict(self.settings, port=str(new_port))
        ok, error = self.service.reload(updated)
        self.assertTrue(ok, error)
        with urllib.request.urlopen(
                f"http://127.0.0.1:{new_port}/healthz", timeout=2) as response:
            self.assertEqual(response.status, 200)

        ok, error = self.service.reload(dict(updated, enabled="disable"))
        self.assertTrue(ok, error)
        self.assertFalse(self.service.is_enabled())

    def test_runtime_setting_is_persisted_and_reloaded(self):
        settings = {
            "webapp_enabled": "disable",
            "webapp_public_url": "https://old.example.com",
            "turnstile_site_key": "site",
            "turnstile_secret_key": "secret",
            "turnstile_hostname": "",
            "webapp_host": "127.0.0.1",
            "webapp_port": "8080",
            "webapp_auth_max_age": "300",
            "captcha": "image",
        }
        cache = Cache(str(Path(self.directory.name) / "runtime-cache"))
        try:
            for key, value in settings.items():
                cache.set(f"setting_{key}", value)
            bot = object.__new__(TGBot)
            bot.cache = cache
            bot.database = FakeDatabase(settings)
            bot.webapp_service = SimpleNamespace(
                reload=Mock(return_value=(True, None)))

            ok, error = bot.update_turnstile_setting(
                "public_url", "https://new.example.com")
            self.assertTrue(ok, error)
            self.assertEqual(
                settings["webapp_public_url"], "https://new.example.com")
            self.assertEqual(
                cache.get("setting_webapp_public_url"),
                "https://new.example.com",
            )
            bot.webapp_service.reload.assert_called_once()
        finally:
            cache.close()


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
