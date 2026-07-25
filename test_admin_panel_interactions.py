#!/usr/bin/env python3
"""Regression tests for compact admin panels and inline verification controls."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.argv = [sys.argv[0], "-token", "test-token", "-group_id", "-100123"]

from src.handlers.admin_handler import AdminHandler
from src.handlers.command_handler import CommandHandler


class MemoryCache:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value, expire=None):
        self.values[key] = value

    def pop(self, key, default=None):
        return self.values.pop(key, default)

    def delete(self, key):
        self.values.pop(key, None)


class FakeBot:
    def __init__(self):
        self.deleted = []
        self.edits = []
        self.sent = []
        self.next_steps = []
        self._next_message_id = 700

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    def edit_message_text(self, text, chat_id, message_id, reply_markup=None):
        self.edits.append((text, chat_id, message_id, reply_markup))
        return SimpleNamespace(message_id=message_id, chat=SimpleNamespace(id=chat_id))

    def send_message(self, chat_id, text, **kwargs):
        self._next_message_id += 1
        message = SimpleNamespace(
            message_id=self._next_message_id,
            chat=SimpleNamespace(id=chat_id),
            text=text,
            kwargs=kwargs,
        )
        self.sent.append(message)
        return message

    def register_next_step_handler(self, message, callback, *args):
        self.next_steps.append((message.message_id, callback, args))


class FakeDatabase:
    def __init__(self):
        self.values = {"time_zone": "UTC"}

    def get_setting(self, key):
        return self.values.get(key)

    def set_setting(self, key, value):
        self.values[key] = value


class FakeAutoResponseManager:
    def __init__(self):
        self.added = []

    def add_auto_response(self, *args):
        self.added.append(args)

    def update_time_zone(self, value):
        self.time_zone = value


class FakeWebAppService:
    def __init__(self):
        self.running = False

    def is_enabled(self):
        return self.running


class FakeBotInstance:
    def __init__(self):
        self.webapp_service = FakeWebAppService()
        self.settings = {
            "enabled": "disable",
            "public_url": "",
            "site_key": "",
            "secret_key": "",
            "hostname": "",
            "host": "0.0.0.0",
            "port": "8080",
            "auth_max_age": "300",
        }
        self.updated = []

    def get_turnstile_settings(self):
        return dict(self.settings)

    def update_turnstile_setting(self, field, value):
        self.settings[field] = value
        self.webapp_service.running = self.settings["enabled"] == "enable"
        self.updated.append((field, value))
        return True, None

    def update_self_time_zone(self):
        pass


class FakeCaptchaManager:
    def __init__(self):
        self.verified = []
        self.removed = []

    def set_user_verified(self, user_id, db):
        self.verified.append(user_id)

    def remove_user_verification(self, user_id, db):
        self.removed.append(user_id)


def admin_message(message_id=41, text=""):
    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=-100123),
        message_thread_id=None,
        content_type="text",
        text=text,
    )


class AdminPanelInteractionTests(unittest.TestCase):
    def setUp(self):
        self.bot = FakeBot()
        self.cache = MemoryCache()
        self.database = FakeDatabase()
        self.instance = FakeBotInstance()
        self.auto_response_manager = FakeAutoResponseManager()
        self.admin = AdminHandler(
            self.bot,
            -100123,
            "/tmp/unused.db",
            self.cache,
            self.database,
            self.auto_response_manager,
            bot_instance=self.instance,
        )

    def test_turnstile_input_is_deleted_and_original_panel_is_refreshed(self):
        panel = admin_message()
        self.admin.edit_turnstile_setting(panel, "secret_key")

        self.assertEqual(self.bot.next_steps[0][0], panel.message_id)
        self.assertEqual(self.bot.next_steps[0][2], ("secret_key", panel.message_id))

        submitted = admin_message(42, "secret-value")
        self.admin.process_turnstile_setting(submitted, "secret_key", panel.message_id)

        self.assertEqual(self.bot.deleted, [(-100123, 42)])
        self.assertEqual(self.instance.updated, [("secret_key", "secret-value")])
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.bot.edits[-1][2], panel.message_id)
        self.assertIn("Turnstile WebApp settings updated", self.bot.edits[-1][0])

    def test_default_message_input_is_deleted_and_reuses_panel(self):
        panel = admin_message()
        self.admin.edit_default_msg(panel)
        submitted = admin_message(42, "Welcome")

        self.admin.edit_default_msg_handle(submitted, panel.message_id)

        self.assertEqual(self.database.values["default_message"], "Welcome")
        self.assertEqual(self.bot.deleted, [(-100123, 42)])
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.bot.edits[-1][2], panel.message_id)

    def test_auto_reply_draft_deletes_each_input_and_reuses_panel(self):
        panel = admin_message()
        self.admin.add_auto_response(panel)

        trigger = admin_message(42, "hello")
        self.admin.add_auto_response_type(trigger, panel.message_id)
        response = admin_message(43, "world")
        self.admin.add_auto_response_time(response, panel.message_id)
        self.admin.handle_auto_response_time_callback(
            panel, {"value": "no"})

        self.assertEqual(self.bot.deleted, [(-100123, 42), (-100123, 43)])
        self.assertEqual(
            self.auto_response_manager.added,
            [("hello", "world", True, "text", None, None)],
        )
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.bot.edits[-1][2], panel.message_id)

    def test_quick_time_zone_button_updates_the_current_panel(self):
        panel = admin_message()

        self.admin.set_time_zone(panel, "Asia/Tokyo")

        self.assertEqual(self.database.values["time_zone"], "Asia/Tokyo")
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.bot.edits[-1][2], panel.message_id)


class VerifyCommandInteractionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.directory.name) / "storage.db")
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE topics (thread_id INTEGER PRIMARY KEY, user_id INTEGER)")
            db.execute("INSERT INTO topics (thread_id, user_id) VALUES (9, 456)")
        self.bot = FakeBot()
        self.captcha = FakeCaptchaManager()
        self.handler = CommandHandler(
            self.bot, -100123, self.db_path, MemoryCache(), None, self.captcha)

    def tearDown(self):
        self.directory.cleanup()

    def test_verify_uses_inline_controls_and_deletes_command(self):
        command = SimpleNamespace(
            message_id=55,
            chat=SimpleNamespace(id=-100123),
            message_thread_id=9,
            text="/verify",
        )

        self.handler.handle_verify(command)

        self.assertEqual(self.bot.deleted, [(-100123, 55)])
        self.assertEqual(len(self.bot.sent), 1)
        prompt = self.bot.sent[0]
        self.assertEqual(prompt.text, "Set verified status")
        self.assertIsNotNone(prompt.kwargs["reply_markup"])

        panel = SimpleNamespace(
            message_id=prompt.message_id,
            chat=SimpleNamespace(id=-100123),
            message_thread_id=9,
        )
        self.handler.set_verification_status(panel, "verified")

        self.assertEqual(self.captcha.verified, [456])
        self.assertEqual(self.bot.edits[-1][2], prompt.message_id)
        self.assertIn("User verified successfully", self.bot.edits[-1][0])


if __name__ == "__main__":
    unittest.main()
