"""Bounded, rate-limited message queue for Telegram updates."""

import queue
import threading
import time
from collections import OrderedDict, defaultdict, deque
from itertools import count
from typing import Callable

from telebot.types import Message
from telebot.util import antiflood

from src import config
from src.config import logger, _


class RedisRateLimiter:
    """Atomic Redis-backed token buckets for multi-instance deployments."""

    _ALLOW_SCRIPT = """
local global_key = KEYS[1]
local user_key = KEYS[2]
local priority_key = KEYS[3]
local now = tonumber(ARGV[1])
local global_rate = tonumber(ARGV[2])
local global_capacity = tonumber(ARGV[3])
local user_rate = tonumber(ARGV[4])
local user_capacity = tonumber(ARGV[5])
local priority_rate = tonumber(ARGV[6])
local priority_capacity = tonumber(ARGV[7])
local priority_ttl = tonumber(ARGV[8])
local priority_allowed = tonumber(ARGV[9])
local block_threshold = tonumber(ARGV[10])
local ttl = tonumber(ARGV[11])
local is_priority = 0

if priority_allowed == 1 then
    is_priority = redis.call('EXISTS', priority_key)
end

if is_priority == 1 then
    user_rate = priority_rate
    user_capacity = priority_capacity
end

local function load_bucket(key, capacity)
    local values = redis.call('HMGET', key, 'tokens', 'updated', 'violations', 'block_requested')
    local tokens = tonumber(values[1]) or capacity
    local updated = tonumber(values[2]) or now
    local violations = tonumber(values[3]) or 0
    local block_requested = tonumber(values[4]) or 0
    return tokens, updated, violations, block_requested
end

local function refill(tokens, updated, rate, capacity)
    return math.min(capacity, tokens + math.max(0, now - updated) * rate)
end

local global_tokens, global_updated = load_bucket(global_key, global_capacity)
global_tokens = refill(global_tokens, global_updated, global_rate, global_capacity)

local user_tokens, user_updated, violations, block_requested = load_bucket(user_key, user_capacity)
user_tokens = refill(user_tokens, user_updated, user_rate, user_capacity)

if user_tokens < 1 then
    violations = violations + 1
    local should_block = 0
    if violations >= block_threshold and block_requested == 0 then
        block_requested = 1
        should_block = 1
    end
    redis.call('HMSET', user_key, 'tokens', user_tokens, 'updated', now,
        'violations', violations, 'block_requested', block_requested)
    redis.call('EXPIRE', user_key, ttl)
    return {0, 0, should_block, is_priority}
end

if global_tokens < 1 then
    redis.call('HMSET', global_key, 'tokens', global_tokens, 'updated', now)
    redis.call('EXPIRE', global_key, ttl)
    return {0, 1, 0, is_priority}
end

redis.call('HMSET', global_key, 'tokens', global_tokens - 1, 'updated', now)
redis.call('EXPIRE', global_key, ttl)
redis.call('HMSET', user_key, 'tokens', user_tokens - 1, 'updated', now,
    'violations', 0, 'block_requested', block_requested)
redis.call('EXPIRE', user_key, ttl)
if is_priority == 1 then
    redis.call('EXPIRE', priority_key, priority_ttl)
end
return {1, 0, 0, is_priority}
"""

    def __init__(self, redis_url: str, key_prefix: str, state_ttl: int):
        import redis

        self.client = redis.Redis.from_url(
            redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=False,
        )
        self.client.ping()
        self.key_prefix = key_prefix
        self.state_ttl = state_ttl
        self.script = self.client.register_script(self._ALLOW_SCRIPT)

    def allow(self, user_id: int, user_rate: float, user_capacity: int,
              priority_rate: float, priority_capacity: int, priority_ttl: int,
              priority_allowed: bool, global_rate: float, global_capacity: int,
              block_threshold: int) -> tuple[bool, bool, bool, bool]:
        result = self.script(
            keys=[
                f"{self.key_prefix}:rate-limit:global",
                f"{self.key_prefix}:rate-limit:user:{user_id}",
                f"{self.key_prefix}:priority-user:{user_id}",
            ],
            args=[
                time.time(), global_rate, global_capacity,
                user_rate, user_capacity, priority_rate, priority_capacity,
                priority_ttl, int(priority_allowed), block_threshold, self.state_ttl,
            ],
        )
        allowed, global_limited, should_block, is_priority = (
            int(value) for value in result)
        return bool(allowed), bool(global_limited), bool(should_block), bool(is_priority)

    def mark_priority(self, user_id: int, inactivity_seconds: int):
        """Promote a user and reset their bucket to the higher-capacity tier."""
        pipeline = self.client.pipeline(transaction=True)
        pipeline.set(
            f"{self.key_prefix}:priority-user:{user_id}",
            "1",
            ex=inactivity_seconds,
        )
        pipeline.delete(f"{self.key_prefix}:rate-limit:user:{user_id}")
        pipeline.execute()

    def revoke_priority(self, user_id: int):
        """Delete shared priority and rate state when verification is revoked."""
        self.client.delete(
            f"{self.key_prefix}:priority-user:{user_id}",
            f"{self.key_prefix}:rate-limit:user:{user_id}",
        )


class MessageQueueManager:
    """Process Telegram updates with bounded queues and ingress rate limits."""

    def __init__(self, handler_func: Callable, num_workers: int = 5,
                 queue_size: int = 1000, group_queue_size: int = 200,
                 per_user_queue_size: int = 5,
                 unverified_rate: float = 0.1, unverified_burst: int = 1,
                 verified_rate: float = 1 / 6, verified_burst: int = 3,
                 priority_rate: float = 0.5, priority_burst: int = 10,
                 priority_inactivity_seconds: int = 86400,
                 global_rate: float = 10.0, global_burst: int = 20,
                 abuse_block_threshold: int = 20,
                 rate_limit_state_size: int = 10000,
                 is_user_verified: Callable[[int], bool] | None = None,
                 is_user_priority: Callable[[int], bool] | None = None,
                 touch_user_priority: Callable[[int], None] | None = None,
                 block_user: Callable[[Message], None] | None = None,
                 redis_url: str = "", redis_prefix: str = "betterforward"):
        self._validate_settings(
            num_workers, queue_size, group_queue_size, per_user_queue_size,
            unverified_rate, unverified_burst, verified_rate, verified_burst,
            priority_rate, priority_burst, priority_inactivity_seconds,
            global_rate, global_burst, abuse_block_threshold, rate_limit_state_size,
        )
        self.handler_func = handler_func
        self.num_workers = num_workers
        self.per_user_queue_size = per_user_queue_size
        self.unverified_rate = unverified_rate
        self.unverified_burst = unverified_burst
        self.verified_rate = verified_rate
        self.verified_burst = verified_burst
        self.priority_rate = priority_rate
        self.priority_burst = priority_burst
        self.priority_inactivity_seconds = priority_inactivity_seconds
        self.global_rate = global_rate
        self.global_burst = global_burst
        self.abuse_block_threshold = abuse_block_threshold
        self.rate_limit_state_size = rate_limit_state_size
        self.is_user_verified = is_user_verified or (lambda _: False)
        self.is_user_priority = is_user_priority or (lambda _: False)
        self.touch_user_priority = touch_user_priority or (lambda _: None)
        self.block_user = block_user

        # Private traffic and group management traffic have separate capacity.
        self.private_queue = queue.PriorityQueue(maxsize=queue_size)
        self.group_queue = queue.Queue(maxsize=group_queue_size)
        self.main_queue = self.private_queue  # Backwards-compatible name for queue metrics.
        self._queue_sequence = count()

        self.user_queues = defaultdict(deque)
        self.processing_users = set()
        self.lock = threading.Lock()
        self.workers = []
        self._stop_event = threading.Event()

        self._rate_states = OrderedDict()
        self._global_tokens = float(global_burst)
        self._global_updated = time.monotonic()
        self._stats = defaultdict(int)
        self._redis_limiter = self._create_redis_limiter(redis_url, redis_prefix)

        self._start_workers()

    @staticmethod
    def _validate_settings(num_workers, *values):
        if num_workers < 0 or any(value <= 0 for value in values):
            raise ValueError("Queue and rate-limit settings must be greater than zero")

    def _create_redis_limiter(self, redis_url: str, redis_prefix: str):
        if not redis_url:
            return None

        state_ttl = max(60, int(max(
            self.unverified_burst / self.unverified_rate,
            self.verified_burst / self.verified_rate,
            self.priority_burst / self.priority_rate,
        ) * 10))
        try:
            limiter = RedisRateLimiter(redis_url, redis_prefix, state_ttl)
            logger.info("Using Redis for shared ingress rate limiting")
            return limiter
        except Exception as error:
            logger.error("Redis rate limiter unavailable; using local limits: %s", error)
            return None

    def _start_workers(self):
        for index in range(self.num_workers):
            worker = threading.Thread(
                target=self._worker,
                name=f"MessageWorker-{index + 1}",
                daemon=True,
            )
            worker.start()
            self.workers.append(worker)
        logger.info(_("Started {} message processing workers").format(self.num_workers))

    @staticmethod
    def _is_private(message: Message) -> bool:
        return getattr(message.chat, "type", None) == "private"

    @staticmethod
    def _get_actor_id(message: Message) -> int | None:
        actor = getattr(message, "from_user", None) or getattr(message, "user", None)
        return getattr(actor, "id", None)

    def _get_user_id(self, message: Message) -> int | str:
        actor_id = self._get_actor_id(message)
        if self._is_private(message) and actor_id is not None:
            return actor_id

        thread_id = getattr(message, "message_thread_id", None)
        if thread_id is not None:
            return f"thread_{thread_id}"
        chat_id = getattr(message.chat, "id", "unknown")
        return f"chat_{chat_id}_user_{actor_id}" if actor_id is not None else f"chat_{chat_id}"

    @staticmethod
    def _refill(tokens: float, updated: float, rate: float, capacity: int,
                now: float) -> float:
        return min(capacity, tokens + max(0.0, now - updated) * rate)

    def _allow_locally(self, user_id: int, verified: bool,
                       priority: bool) -> tuple[bool, bool, bool]:
        now = time.monotonic()
        if priority:
            user_rate = self.priority_rate
            user_capacity = self.priority_burst
        elif verified:
            user_rate = self.verified_rate
            user_capacity = self.verified_burst
        else:
            user_rate = self.unverified_rate
            user_capacity = self.unverified_burst

        with self.lock:
            state = self._rate_states.get(user_id)
            if state is None:
                state = {
                    "tokens": float(user_capacity),
                    "updated": now,
                    "violations": 0,
                    "block_requested": False,
                }
                self._rate_states[user_id] = state
            else:
                self._rate_states.move_to_end(user_id)

            state["tokens"] = self._refill(
                state["tokens"], state["updated"], user_rate, user_capacity, now)
            state["updated"] = now

            if state["tokens"] < 1:
                state["violations"] += 1
                should_block = (
                    state["violations"] >= self.abuse_block_threshold
                    and not state["block_requested"]
                )
                if should_block:
                    state["block_requested"] = True
                self._trim_rate_states()
                return False, False, should_block

            global_tokens = self._refill(
                self._global_tokens, self._global_updated,
                self.global_rate, self.global_burst, now)
            self._global_tokens = global_tokens
            self._global_updated = now
            if global_tokens < 1:
                self._trim_rate_states()
                return False, True, False

            state["tokens"] -= 1
            state["violations"] = 0
            self._global_tokens -= 1
            self._trim_rate_states()
            return True, False, False

    def _trim_rate_states(self):
        while len(self._rate_states) > self.rate_limit_state_size:
            self._rate_states.popitem(last=False)

    def _allow_private_message(self, message: Message) -> tuple[bool, bool, bool]:
        user_id = self._get_actor_id(message)
        if user_id is None:
            with self.lock:
                self._stats["dropped_private_without_user"] += 1
            return False, True, False
        try:
            verified = bool(self.is_user_verified(user_id))
            priority = verified and bool(self.is_user_priority(user_id))
        except Exception as error:
            logger.warning("Could not read rate-limit status for user %s: %s", user_id, error)
            verified = False
            priority = False

        user_rate = self.verified_rate if verified else self.unverified_rate
        user_capacity = self.verified_burst if verified else self.unverified_burst
        if self._redis_limiter is not None:
            try:
                allowed, global_limited, should_block, priority = self._redis_limiter.allow(
                    user_id, user_rate, user_capacity,
                    self.priority_rate, self.priority_burst,
                    self.priority_inactivity_seconds, verified,
                    self.global_rate, self.global_burst,
                    self.abuse_block_threshold,
                )
            except Exception as error:
                logger.error("Redis rate limiter failed; using local limits: %s", error)
                self._redis_limiter = None
                allowed, global_limited, should_block = self._allow_locally(
                    user_id, verified, priority)
        else:
            allowed, global_limited, should_block = self._allow_locally(
                user_id, verified, priority)

        if allowed:
            if priority:
                try:
                    self.touch_user_priority(user_id)
                except Exception as error:
                    logger.warning("Could not refresh priority status for user %s: %s", user_id, error)
                with self.lock:
                    self._stats["accepted_priority"] += 1
            return True, False, priority

        with self.lock:
            self._stats["dropped_global_rate_limit" if global_limited else "dropped_rate_limit"] += 1

        if should_block and self.block_user and getattr(message, "from_user", None):
            try:
                self.block_user(message)
                with self.lock:
                    self._stats["auto_blocked"] += 1
                logger.warning("Auto-blocked user %s after repeated ingress rate-limit violations", user_id)
            except Exception as error:
                logger.error("Failed to auto-block rate-limited user %s: %s", user_id, error)
        return False, True, priority

    def mark_user_replied(self, user_id: int):
        """Promote an administrator-replied user to the temporary priority tier."""
        with self.lock:
            self._rate_states.pop(user_id, None)
            self._stats["priority_promotions"] += 1

        if self._redis_limiter is not None:
            try:
                self._redis_limiter.mark_priority(
                    user_id, self.priority_inactivity_seconds)
            except Exception as error:
                logger.error("Failed to share priority status through Redis: %s", error)

    def revoke_user_priority(self, user_id: int):
        """Remove priority state after a block or verification revocation."""
        with self.lock:
            self._rate_states.pop(user_id, None)

        if self._redis_limiter is not None:
            try:
                self._redis_limiter.revoke_priority(user_id)
            except Exception as error:
                logger.error("Failed to revoke shared priority status: %s", error)

    def _get_next_message(self) -> tuple[tuple[Message, Callable], queue.Queue]:
        try:
            return self.group_queue.get_nowait(), self.group_queue
        except queue.Empty:
            _, _, queued_message = self.private_queue.get(timeout=0.2)
            return queued_message, self.private_queue

    def _worker(self):
        while not config.stop and not self._stop_event.is_set():
            try:
                queued_message, source_queue = self._get_next_message()
                message = queued_message[0]
                user_id = self._get_user_id(message)

                with self.lock:
                    if user_id in self.processing_users:
                        pending = self.user_queues[user_id]
                        if len(pending) >= self.per_user_queue_size:
                            self._stats["dropped_per_user_queue"] += 1
                        else:
                            pending.append(queued_message)
                        source_queue.task_done()
                        continue
                    self.processing_users.add(user_id)

                self._process_user_messages(user_id, queued_message, source_queue)
            except queue.Empty:
                continue
            except Exception as error:
                logger.error(_("Worker error: {}").format(error))

    def _process_user_messages(self, user_id, first_item: tuple[Message, Callable],
                               source_queue: queue.Queue):
        try:
            queued_message = first_item
            while True:
                message, handler_func = queued_message
                try:
                    antiflood(handler_func, message)
                except Exception as error:
                    logger.error(_("Failed to process message for user {}: {}").format(user_id, error))

                with self.lock:
                    pending = self.user_queues.get(user_id)
                    if not pending:
                        self.processing_users.discard(user_id)
                        self.user_queues.pop(user_id, None)
                        break
                    queued_message = pending.popleft()
        finally:
            source_queue.task_done()

    def put(self, message: Message, handler_func: Callable | None = None) -> bool:
        """Try to queue an update without blocking the polling thread."""
        queued_message = (message, handler_func or self.handler_func)
        if self._is_private(message):
            allowed, _, priority = self._allow_private_message(message)
            if not allowed:
                return False
            target_queue = self.private_queue
            queue_name = "private"
            queue_item = (0 if priority else 1, next(self._queue_sequence), queued_message)
        else:
            target_queue = self.group_queue
            queue_name = "group"
            queue_item = queued_message

        try:
            target_queue.put_nowait(queue_item)
            return True
        except queue.Full:
            with self.lock:
                self._stats[f"dropped_{queue_name}_queue_full"] += 1
            return False

    def stop(self):
        """Stop workers without waiting indefinitely for queued attack traffic."""
        logger.info(_("Stopping message queue manager..."))
        self._stop_event.set()
        for worker in self.workers:
            worker.join(timeout=5)
        logger.info(_("Message queue manager stopped"))

    def get_stats(self) -> dict:
        with self.lock:
            return {
                "main_queue_size": self.private_queue.qsize(),
                "private_queue_size": self.private_queue.qsize(),
                "group_queue_size": self.group_queue.qsize(),
                "processing_users_count": len(self.processing_users),
                "user_queues_count": len(self.user_queues),
                "total_queued_messages": sum(len(items) for items in self.user_queues.values()),
                "rate_limit_state_count": len(self._rate_states),
                "workers_count": len(self.workers),
                **dict(self._stats),
            }
