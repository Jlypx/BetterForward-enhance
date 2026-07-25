#!/usr/bin/env python3
"""Regression tests for appeal callback feedback and verification startup."""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.argv = [sys.argv[0], "-token", "test-token", "-group_id", "-100123"]

from diskcache import Cache
from src.handlers.callback_handler import CallbackHandler


class FakeBot:
    def __init__(self):
        self.answers = []
        self.edits = []

    def answer_callback_query(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    def edit_message_text(self, *args, **kwargs):
        self.edits.append((args, kwargs))


class FakeWebAppService:
    def __init__(self):
        self.challenges = []

    def is_enabled(self):
        return True

    def create_challenge(self, user_id, purpose):
        self.challenges.append((user_id, purpose))
        return "https://verify.example.test/?challenge=abc"


class FakeCaptchaManager:
    def __init__(self, fail=False, webapp_service=None):
        self.fail = fail
        self.calls = []
        self.webapp_service = webapp_service

    def generate_captcha(self, user_id, captcha_type, purpose="normal"):
        self.calls.append((user_id, captcha_type, purpose))
        if self.fail:
            raise RuntimeError("captcha unavailable")
        return "4 + 5 = ?"


class AppealCallbackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.directory.name) / "storage.db")
        self.cache = Cache(str(Path(self.directory.name) / "cache"))
        with sqlite3.connect(self.db_path) as db:
            db.executescript("""
                CREATE TABLE blocked_users (
                    user_id INTEGER PRIMARY KEY,
                    block_reason TEXT,
                    blocked_until TIMESTAMP
                );
                CREATE TABLE appeal_requests (
                    user_id INTEGER PRIMARY KEY,
                    status TEXT
                );
                INSERT INTO blocked_users (user_id, block_reason)
                VALUES (123, 'auto_attempts');
            """)

    def tearDown(self):
        self.cache.close()
        self.directory.cleanup()

    def _callback(self, captcha_manager):
        bot = FakeBot()
        handler = CallbackHandler(
            bot,
            -100123,
            SimpleNamespace(cache=self.cache),
            SimpleNamespace(),
            captcha_manager,
            db_path=self.db_path,
        )
        call = SimpleNamespace(
            id="appeal-callback",
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=123),
                message_id=77,
            ),
        )
        return bot, handler, call

    def test_public_callback_starts_verification_with_one_status_response(self):
        captcha = FakeCaptchaManager()
        bot, handler, call = self._callback(captcha)
        call.data = json.dumps({"action": "appeal_request", "user_id": 123})

        handler.handle_callback_query(call)

        self.assertEqual(captcha.calls, [(123, "math", "normal")])
        self.assertEqual(len(bot.answers), 1)
        self.assertEqual(bot.answers[0][0][0], "appeal-callback")

    def test_math_challenge_replaces_the_appeal_prompt(self):
        captcha = FakeCaptchaManager()
        bot, handler, call = self._callback(captcha)

        handler._handle_appeal_request(call, {"user_id": 123})

        self.assertEqual(captcha.calls, [(123, "math", "normal")])
        self.assertTrue(self.cache.get("appeal_verification_123"))
        self.assertEqual(len(bot.edits), 1)
        self.assertIn("4 + 5 = ?", bot.edits[0][0][0])
        self.assertEqual(bot.answers[0][0][0], "appeal-callback")
        self.assertIn("Please complete verification", bot.answers[0][0][1])

    def test_webapp_challenge_replaces_the_appeal_prompt(self):
        service = FakeWebAppService()
        captcha = FakeCaptchaManager(webapp_service=service)
        self.cache.set("setting_captcha", "webapp")
        bot, handler, call = self._callback(captcha)

        handler._handle_appeal_request(call, {"user_id": 123})

        self.assertEqual(service.challenges, [(123, "appeal")])
        self.assertEqual(captcha.calls, [])
        self.assertEqual(len(bot.edits), 1)
        markup = bot.edits[0][1]["reply_markup"]
        self.assertEqual(markup.keyboard[0][0].web_app.url, "https://verify.example.test/?challenge=abc")

    def test_failure_clears_pending_state_and_shows_feedback(self):
        captcha = FakeCaptchaManager(fail=True)
        bot, handler, call = self._callback(captcha)

        handler._handle_appeal_request(call, {"user_id": 123})

        self.assertIsNone(self.cache.get("appeal_verification_123"))
        self.assertEqual(len(bot.edits), 1)
        self.assertIn("Could not start appeal verification", bot.edits[0][0][0])
        self.assertTrue(bot.answers[0][1]["show_alert"])

    def test_callback_with_string_user_id_starts_verification(self):
        captcha = FakeCaptchaManager()
        bot, handler, call = self._callback(captcha)

        handler._handle_appeal_request(call, {"user_id": "123"})

        self.assertEqual(captcha.calls, [(123, "math", "normal")])
        self.assertEqual(len(bot.edits), 1)


if __name__ == "__main__":
    unittest.main()
