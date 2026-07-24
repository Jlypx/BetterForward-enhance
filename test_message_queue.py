#!/usr/bin/env python3
"""Regression tests for bounded ingress queues and rate limits."""

import sys
import threading
import time
import unittest
from types import SimpleNamespace

sys.argv = [sys.argv[0], "-token", "test-token", "-group_id", "-100123"]

from src import config
from src.utils.message_queue import MessageQueueManager


def private_message(user_id: int):
    return SimpleNamespace(
        chat=SimpleNamespace(id=user_id, type="private"),
        from_user=SimpleNamespace(id=user_id),
        message_thread_id=None,
    )


def group_message(thread_id: int):
    return SimpleNamespace(
        chat=SimpleNamespace(id=-100123, type="supergroup"),
        from_user=SimpleNamespace(id=1),
        message_thread_id=thread_id,
    )


class MessageQueueManagerTests(unittest.TestCase):
    def setUp(self):
        config.stop = False
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.stop()
        config.stop = False

    def manager(self, **kwargs):
        manager = MessageQueueManager(
            handler_func=kwargs.pop("handler_func", lambda _: None),
            num_workers=kwargs.pop("num_workers", 0),
            queue_size=kwargs.pop("queue_size", 10),
            group_queue_size=kwargs.pop("group_queue_size", 10),
            per_user_queue_size=kwargs.pop("per_user_queue_size", 1),
            unverified_rate=kwargs.pop("unverified_rate", 0.1),
            unverified_burst=kwargs.pop("unverified_burst", 1),
            verified_rate=kwargs.pop("verified_rate", 100.0),
            verified_burst=kwargs.pop("verified_burst", 10),
            priority_rate=kwargs.pop("priority_rate", 200.0),
            priority_burst=kwargs.pop("priority_burst", 20),
            priority_inactivity_seconds=kwargs.pop("priority_inactivity_seconds", 60),
            global_rate=kwargs.pop("global_rate", 100.0),
            global_burst=kwargs.pop("global_burst", 100),
            abuse_block_threshold=kwargs.pop("abuse_block_threshold", 2),
            rate_limit_state_size=kwargs.pop("rate_limit_state_size", 3),
            is_user_verified=kwargs.pop("is_user_verified", lambda _: False),
            is_user_priority=kwargs.pop("is_user_priority", lambda _: False),
            touch_user_priority=kwargs.pop("touch_user_priority", lambda _: None),
            block_user=kwargs.pop("block_user", None),
            **kwargs,
        )
        self.managers.append(manager)
        return manager

    def test_unverified_users_are_rate_limited_before_queueing(self):
        manager = self.manager()

        self.assertTrue(manager.put(private_message(10)))
        self.assertFalse(manager.put(private_message(10)))

        stats = manager.get_stats()
        self.assertEqual(stats["private_queue_size"], 1)
        self.assertEqual(stats["dropped_rate_limit"], 1)

    def test_repeated_rate_limit_violations_trigger_one_block_callback(self):
        blocked = []
        manager = self.manager(block_user=lambda message: blocked.append(message.from_user.id))

        self.assertTrue(manager.put(private_message(11)))
        self.assertFalse(manager.put(private_message(11)))
        self.assertFalse(manager.put(private_message(11)))
        self.assertFalse(manager.put(private_message(11)))

        self.assertEqual(blocked, [11])
        self.assertEqual(manager.get_stats()["auto_blocked"], 1)

    def test_global_rate_limit_does_not_depend_on_a_single_user(self):
        manager = self.manager(
            is_user_verified=lambda _: True,
            global_burst=1,
            global_rate=0.1,
        )

        self.assertTrue(manager.put(private_message(12)))
        self.assertFalse(manager.put(private_message(13)))
        self.assertEqual(manager.get_stats()["dropped_global_rate_limit"], 1)

    def test_private_and_group_queues_have_independent_capacity(self):
        manager = self.manager(
            is_user_verified=lambda _: True,
            queue_size=1,
            group_queue_size=1,
        )

        self.assertTrue(manager.put(private_message(14)))
        self.assertFalse(manager.put(private_message(15)))
        self.assertTrue(manager.put(group_message(1)))
        self.assertFalse(manager.put(group_message(2)))

        stats = manager.get_stats()
        self.assertEqual(stats["dropped_private_queue_full"], 1)
        self.assertEqual(stats["dropped_group_queue_full"], 1)

    def test_admin_replied_user_gets_higher_limit_and_activity_refresh(self):
        touched = []
        manager = self.manager(
            is_user_verified=lambda _: True,
            is_user_priority=lambda user_id: user_id == 17,
            touch_user_priority=touched.append,
            verified_rate=0.01,
            verified_burst=1,
            priority_rate=100.0,
            priority_burst=3,
        )

        self.assertTrue(manager.put(private_message(17)))
        self.assertTrue(manager.put(private_message(17)))
        self.assertTrue(manager.put(private_message(17)))
        self.assertFalse(manager.put(private_message(17)))
        self.assertEqual(touched, [17, 17, 17])

        self.assertTrue(manager.put(private_message(18)))
        self.assertFalse(manager.put(private_message(18)))

    def test_priority_user_is_processed_before_normal_private_user(self):
        manager = self.manager(
            is_user_verified=lambda _: True,
            is_user_priority=lambda user_id: user_id == 20,
        )

        self.assertTrue(manager.put(private_message(19)))
        self.assertTrue(manager.put(private_message(20)))
        queued_message, source_queue = manager._get_next_message()
        try:
            self.assertEqual(queued_message[0].from_user.id, 20)
        finally:
            source_queue.task_done()

    def test_processing_user_has_bounded_pending_messages(self):
        started = threading.Event()
        release = threading.Event()

        def handler(_):
            started.set()
            release.wait(timeout=2)

        manager = self.manager(
            handler_func=handler,
            num_workers=2,
            is_user_verified=lambda _: True,
            per_user_queue_size=1,
        )

        self.assertTrue(manager.put(private_message(16)))
        self.assertTrue(started.wait(timeout=1))
        self.assertTrue(manager.put(private_message(16)))

        deadline = time.monotonic() + 1
        while manager.get_stats()["total_queued_messages"] != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(manager.get_stats()["total_queued_messages"], 1)

        self.assertTrue(manager.put(private_message(16)))
        deadline = time.monotonic() + 1
        while manager.get_stats().get("dropped_per_user_queue", 0) != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(manager.get_stats()["dropped_per_user_queue"], 1)
        release.set()


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
