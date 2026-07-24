#!/usr/bin/env python3
"""Regression tests for ingress verification and temporary blocks."""

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.argv = [sys.argv[0], "-token", "test-token", "-group_id", "-100123"]

from diskcache import Cache
from src.bot import TGBot
from src.config import args
from src.handlers.message_handler import MessageHandler
from src.utils.captcha import CaptchaManager


class FakeBot:
    def __init__(self):
        self.sent_messages = []

    def send_message(self, *args, **kwargs):
        self.sent_messages.append((args, kwargs))
        return SimpleNamespace(message_id=len(self.sent_messages))


class IngressProtectionTests(unittest.TestCase):
    def test_successful_admin_reply_promotes_only_verified_user(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "storage.db")
            cache = Cache(str(Path(directory) / "cache"))
            promoted = []
            try:
                with sqlite3.connect(db_path) as db:
                    db.executescript("""
                        CREATE TABLE topics (user_id INTEGER, thread_id INTEGER);
                        CREATE TABLE messages (
                            received_id INTEGER, forwarded_id INTEGER,
                            topic_id INTEGER, in_group BOOLEAN
                        );
                        CREATE TABLE verified_users (user_id INTEGER PRIMARY KEY);
                        INSERT INTO topics (user_id, thread_id) VALUES (1, 77);
                        INSERT INTO verified_users (user_id) VALUES (1);
                    """)
                    bot = FakeBot()
                    captcha = CaptchaManager(bot, cache)
                    handler = MessageHandler(
                        bot, -100123, db_path, cache, captcha,
                        SimpleNamespace(match_auto_response=lambda _: None),
                        bot_instance=SimpleNamespace(mark_user_replied=promoted.append),
                    )
                    message = SimpleNamespace(
                        chat=SimpleNamespace(id=-100123, type="supergroup"),
                        from_user=SimpleNamespace(id=99),
                        content_type="text",
                        message_thread_id=77,
                        message_id=8,
                        reply_to_message=None,
                    )

                    handler._handle_group_message(message, "reply", None, db.cursor(), db)
                    self.assertEqual(promoted, [1])

                    captcha.remove_user_verification(1, db)
                    message.message_id = 9
                    handler._handle_group_message(message, "reply", None, db.cursor(), db)
                    self.assertEqual(promoted, [1])
            finally:
                cache.close()

    def test_priority_expires_after_conversation_is_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Cache(str(Path(directory) / "cache"))
            original_timeout = args.priority_inactivity_seconds
            try:
                args.priority_inactivity_seconds = 1
                bot = object.__new__(TGBot)
                bot.cache = cache
                bot.message_queue_manager = SimpleNamespace(mark_user_replied=lambda _: None)

                bot.mark_user_replied(2)
                self.assertTrue(bot._is_rate_limit_priority(2))
                time.sleep(1.1)
                self.assertFalse(bot._is_rate_limit_priority(2))
            finally:
                args.priority_inactivity_seconds = original_timeout
                cache.close()

    def test_temporary_block_is_not_an_outbound_reply_amplifier(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "storage.db")
            cache = Cache(str(Path(directory) / "cache"))
            try:
                with sqlite3.connect(db_path) as db:
                    db.executescript("""
                        CREATE TABLE blocked_users (
                            user_id INTEGER PRIMARY KEY,
                            username TEXT,
                            first_name TEXT,
                            last_name TEXT,
                            block_reason TEXT,
                            blocked_at TIMESTAMP,
                            blocked_until TIMESTAMP
                        );
                        CREATE TABLE verified_users (user_id INTEGER PRIMARY KEY);
                        CREATE TABLE verification_attempts (
                            user_id INTEGER PRIMARY KEY,
                            attempt_count INTEGER,
                            last_attempt_time TIMESTAMP,
                            blocked_by_attempts BOOLEAN
                        );
                    """)
                    db.execute(
                        """INSERT INTO blocked_users
                           (user_id, block_reason, blocked_until)
                           VALUES (1, 'rate_limit', datetime('now', '+1 hour'))"""
                    )
                    db.commit()

                    bot = FakeBot()
                    captcha = CaptchaManager(bot, cache)
                    handler = MessageHandler(
                        bot, -100123, db_path, cache, captcha,
                        SimpleNamespace(match_auto_response=lambda _: None),
                    )
                    message = SimpleNamespace(
                        chat=SimpleNamespace(id=1, type="private"),
                        from_user=SimpleNamespace(id=1, username="user", first_name="User", last_name=None),
                        text="flood",
                        message_id=1,
                    )

                    self.assertFalse(handler.can_process_private_action(message))
                    self.assertFalse(handler.can_process_private_action(message))
                    self.assertEqual(len(bot.sent_messages), 1)
            finally:
                cache.close()


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
