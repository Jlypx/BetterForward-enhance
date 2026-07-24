"""Main bot class for BetterForward."""

import sqlite3
from types import SimpleNamespace

import pytz
from diskcache import Cache
from telebot import types, TeleBot

from src.config import args, logger, _
from src.database import Database
from src.handlers.admin_handler import AdminHandler
from src.handlers.callback_handler import CallbackHandler
from src.handlers.command_handler import CommandHandler
from src.handlers.message_handler import MessageHandler
from src.utils.auto_response import AutoResponseManager
from src.utils.captcha import CaptchaManager
from src.utils.message_queue import MessageQueueManager
from src.utils.spam_detector_manager import SpamDetectorManager
from src.utils.spam_detectors import KeywordSpamDetector
from src.utils.webapp_verification import TurnstileWebAppService


class TGBot:
    """Main Telegram bot class."""

    TURNSTILE_SETTING_KEYS = {
        "enabled": "webapp_enabled",
        "public_url": "webapp_public_url",
        "site_key": "turnstile_site_key",
        "secret_key": "turnstile_secret_key",
        "hostname": "turnstile_hostname",
        "host": "webapp_host",
        "port": "webapp_port",
        "auth_max_age": "webapp_auth_max_age",
    }

    def __init__(self, bot_token: str, group_id: str, db_path: str = "./data/storage.db",
                 num_workers: int = 5):
        """
        Initialize the bot.
        
        Args:
            bot_token: Telegram bot token
            group_id: Target group ID
            db_path: Path to SQLite database
            num_workers: Number of worker threads for message processing (default: 5)
        """
        logger.info(_("Starting BetterForward..."))
        self.group_id = int(group_id)
        self.bot = TeleBot(token=bot_token)
        self.db_path = db_path
        self.num_workers = num_workers

        # Initialize database
        self.database = Database(db_path)

        # Initialize cache
        self.cache = Cache()
        self._cleanup_expired_rate_limit_blocks()
        self._seed_webapp_settings_from_args()

        # Load settings into cache
        self.load_settings()

        self.webapp_service = TurnstileWebAppService(
            bot_token, self.cache, self._handle_webapp_verification)

        # Initialize timezone
        tz_str = self.cache.get("setting_time_zone")
        self.time_zone = pytz.timezone(tz_str) if tz_str else pytz.UTC

        # Initialize managers
        self.captcha_manager = CaptchaManager(
            self.bot, self.cache, webapp_service=self.webapp_service)
        self.auto_response_manager = AutoResponseManager(db_path, self.time_zone)

        # Initialize spam detection system
        self.spam_detector_manager = SpamDetectorManager()
        self.keyword_detector = KeywordSpamDetector()
        self.spam_detector_manager.register_detector(self.keyword_detector)

        # Initialize handlers
        self.message_handler = MessageHandler(
            self.bot, self.group_id, db_path, self.cache,
            self.captcha_manager, self.auto_response_manager,
            spam_detector_manager=self.spam_detector_manager,
            bot_instance=self
        )
        self.command_handler = CommandHandler(
            self.bot, self.group_id, db_path, self.cache,
            self.time_zone, self.captcha_manager
        )
        self.admin_handler = AdminHandler(
            self.bot, self.group_id, db_path, self.cache,
            self.database, self.auto_response_manager,
            spam_keyword_manager=self.keyword_detector,
            bot_instance=self
        )
        self.callback_handler = CallbackHandler(
            self.bot, self.group_id, self.admin_handler,
            self.command_handler, self.captcha_manager,
            db_path=self.db_path,
        )

        # Register handlers
        self._register_handlers()

        # Set bot commands
        self._set_bot_commands()

        # Delete webhook
        self.bot.delete_webhook()

        # Check permissions
        self.check_permission()

        # Setup multi-threaded message queue
        self.message_queue_manager = MessageQueueManager(
            handler_func=self._dispatch_message,
            num_workers=self.num_workers,
            queue_size=args.queue_size,
            group_queue_size=args.group_queue_size,
            per_user_queue_size=args.per_user_queue_size,
            unverified_rate=args.unverified_rate,
            unverified_burst=args.unverified_burst,
            verified_rate=args.verified_rate,
            verified_burst=args.verified_burst,
            priority_rate=args.priority_rate,
            priority_burst=args.priority_burst,
            priority_inactivity_seconds=args.priority_inactivity_seconds,
            global_rate=args.global_rate,
            global_burst=args.global_burst,
            abuse_block_threshold=args.abuse_block_threshold,
            rate_limit_state_size=args.rate_limit_state_size,
            is_user_verified=self._is_rate_limit_verified,
            is_user_priority=self._is_rate_limit_priority,
            touch_user_priority=self._touch_rate_limit_priority,
            block_user=self._block_rate_limited_user,
            redis_url=args.redis_url,
            redis_prefix=args.redis_prefix,
        )

        self.captcha_manager.priority_revoker = (
            self.message_queue_manager.revoke_user_priority)
        ok, error = self.webapp_service.reload(self.get_turnstile_settings())
        if not ok:
            logger.error("Turnstile WebApp could not start: %s", error)

        logger.info(_("Message queue initialized with {} workers").format(self.num_workers))

        # Start polling
        self.bot.infinity_polling(
            skip_pending=True,
            timeout=5,
            allowed_updates=['message', 'edited_message', 'callback_query',
                             'my_chat_member', 'message_reaction', 'message_reaction_count']
        )

    def _register_handlers(self):
        """Register all bot handlers."""
        # Edited messages must share the same bounded ingress queue.
        self.bot.edited_message_handler(func=lambda m: True)(self.push_edited_message)

        # Commands are queued before dispatch so they cannot bypass private-message limits.
        self.bot.message_handler(commands=["start", "help"])(self.push_messages)
        self.bot.message_handler(commands=["ban"])(self.push_messages)
        self.bot.message_handler(commands=["unban"])(self.push_messages)
        self.bot.message_handler(commands=["terminate"])(self.push_messages)
        self.bot.message_handler(commands=["delete"])(self.push_messages)
        self.bot.message_handler(commands=["verify"])(self.push_messages)

        # Message handler (for all message types)
        self.bot.message_handler(
            func=lambda m: True,
            content_types=["photo", "text", "sticker", "video", "document",
                           "voice", "audio", "animation", "contact"]
        )(self.push_messages)

        # Reaction handler
        self.bot.message_reaction_handler(func=lambda message: True)(
            self.push_reaction)

        # Callback query handler
        self.bot.callback_query_handler(func=lambda call: True)(
            self.callback_handler.handle_callback_query)

    def _set_bot_commands(self):
        """Set bot commands for different scopes."""
        self.bot.set_my_commands([
            types.BotCommand("delete", _("Delete a message")),
            types.BotCommand("help", _("Show help")),
        ], scope=types.BotCommandScopeAllPrivateChats())

        self.bot.set_my_commands([
            types.BotCommand("help", _("Show help")),
            types.BotCommand("ban", _("Ban a user")),
            types.BotCommand("unban", _("Unban a user")),
            types.BotCommand("delete", _("Delete a message")),
            types.BotCommand("terminate", _("Terminate a thread")),
            types.BotCommand("verify", _("Set verified status")),
        ], scope=types.BotCommandScopeChat(self.group_id))

    def _seed_webapp_settings_from_args(self):
        """Persist environment/CLI defaults only before the first runtime setup."""
        if self.database.get_setting("webapp_configured") == "yes":
            return
        provided = (
            args.webapp_enabled == "enable"
            or bool(args.webapp_public_url)
            or bool(args.turnstile_site_key)
            or bool(args.turnstile_secret_key)
            or bool(args.turnstile_hostname)
        )
        if not provided:
            return

        enabled = args.webapp_enabled
        if enabled == "enable" and not (
                args.webapp_public_url
                and args.turnstile_site_key
                and args.turnstile_secret_key):
            logger.warning(
                "Incomplete initial Turnstile settings; WebApp remains disabled")
            enabled = "disable"
        initial = {
            "webapp_enabled": enabled,
            "webapp_public_url": args.webapp_public_url,
            "turnstile_site_key": args.turnstile_site_key,
            "turnstile_secret_key": args.turnstile_secret_key,
            "turnstile_hostname": args.turnstile_hostname,
            "webapp_host": args.webapp_host,
            "webapp_port": str(args.webapp_port),
            "webapp_auth_max_age": str(args.webapp_auth_max_age),
            "webapp_configured": "yes",
        }
        for key, value in initial.items():
            self.database.set_setting(key, str(value))

    def get_turnstile_settings(self):
        """Return persisted Turnstile settings in service-facing form."""
        return {
            field: self.cache.get(f"setting_{db_key}")
            for field, db_key in self.TURNSTILE_SETTING_KEYS.items()
        }

    def update_turnstile_setting(self, field, value):
        """Validate, apply, and persist one runtime WebApp setting."""
        if field not in self.TURNSTILE_SETTING_KEYS:
            return False, "Unknown Turnstile setting"
        candidate = self.get_turnstile_settings()
        candidate[field] = str(value).strip()
        ok, error = self.webapp_service.reload(candidate)
        if not ok:
            return False, error

        db_key = self.TURNSTILE_SETTING_KEYS[field]
        self.database.set_setting(db_key, candidate[field])
        self.database.set_setting("webapp_configured", "yes")
        self.cache.set(f"setting_{db_key}", candidate[field])
        self.cache.set("setting_webapp_configured", "yes")

        if field == "enabled" and candidate[field] != "enable":
            if self.cache.get("setting_captcha") == "webapp":
                self.database.set_setting("captcha", "image")
                self.cache.set("setting_captcha", "image")
        return True, None

    def _handle_webapp_verification(self, user_id, purpose, user_data):
        """Apply a verified one-time WebApp challenge to its declared purpose."""
        with sqlite3.connect(self.db_path) as db:
            cursor = db.cursor()
            blocked = cursor.execute(
                """SELECT block_reason FROM blocked_users
                   WHERE user_id = ?
                     AND (blocked_until IS NULL OR blocked_until > CURRENT_TIMESTAMP)""",
                (user_id,),
            ).fetchone()

            if purpose == "normal":
                if blocked:
                    return False, _("Your account is currently blocked")
                self.captcha_manager.set_user_verified(user_id, db)
                self.captcha_manager.reset_attempts(user_id, db)
                self.bot.send_message(
                    user_id, _("Verification successful, you can now send messages"))
                return True, _("Verification successful")

            if purpose == "appeal":
                if not blocked:
                    return False, _("Your account is not blocked")
                user = SimpleNamespace(
                    id=user_id,
                    username=user_data.get("username"),
                    first_name=user_data.get("first_name") or "",
                    last_name=user_data.get("last_name") or "",
                )
                self.message_handler._submit_appeal(user_id, user, db, cursor)
                return True, _("Appeal verification successful")

        return False, _("Invalid verification purpose")

    def load_settings(self):
        """Load settings from database into cache."""
        settings = self.database.get_all_settings()
        for key, value in settings.items():
            self.cache.set(f"setting_{key}", value)

    def update_self_time_zone(self):
        """Update the timezone from cache and propagate to all handlers."""
        tz_str = self.cache.get("setting_time_zone")
        if tz_str:
            self.time_zone = pytz.timezone(tz_str)
            # Update all components that use timezone
            self.auto_response_manager.update_time_zone(self.time_zone)
            self.admin_handler.update_time_zone()
            # command_handler uses property to read from cache, no update needed

    def check_permission(self):
        """Check if bot has necessary permissions."""
        if not self.bot.get_chat(self.group_id).is_forum:
            logger.error(_("Topic function is not enabled in this group"))
            self.bot.send_message(self.group_id, _("Topic function is not enabled in this group"))

        chat_member = self.bot.get_chat_member(self.group_id, self.bot.get_me().id)
        permissions = {
            _("Manage Topics"): chat_member.can_manage_topics,
            _("Delete Messages"): chat_member.can_delete_messages
        }

        for key, value in permissions.items():
            if value is False:
                logger.error(_("Bot doesn't have {} permission").format(key))
                self.bot.send_message(self.group_id, _("Bot doesn't have {} permission").format(key))

        # Check and create spam topic if not exists
        self._ensure_spam_topic()

        self.bot.send_message(self.group_id, _("Bot started successfully"))

    def _ensure_spam_topic(self):
        """Ensure spam topic exists, create if not."""
        self._create_or_load_spam_topic()

    def _create_or_load_spam_topic(self):
        """Create or load spam topic."""
        spam_topic_id = self.database.get_setting('spam_topic')

        # If spam topic ID is not set or is None, create a new topic
        if spam_topic_id is None or spam_topic_id == 'None':
            self._create_spam_topic()
        else:
            # Load existing spam topic ID into cache
            try:
                spam_topic_id = int(spam_topic_id)
                self.cache.set("spam_topic_id", spam_topic_id)
                logger.info(_("Spam topic loaded: {}").format(spam_topic_id))
            except (ValueError, TypeError):
                logger.error(_("Invalid spam topic ID in database: {}").format(spam_topic_id))
                self._create_spam_topic()

    def _create_spam_topic(self):
        """Create a new spam topic."""
        try:
            from telebot.apihelper import create_forum_topic
            logger.info(_("Creating spam topic..."))
            topic = create_forum_topic(
                chat_id=self.group_id,
                name="🚫 Spam Messages",
                token=self.bot.token
            )
            spam_topic_id = topic["message_thread_id"]
            self.database.set_setting('spam_topic', str(spam_topic_id))
            self.cache.set("spam_topic_id", spam_topic_id)
            logger.info(_("Spam topic created with ID: {}").format(spam_topic_id))

            # Send a pin message to the spam topic (silently)
            pin_msg = self.bot.send_message(
                self.group_id,
                _("This topic is used to collect spam messages detected by keywords.\n"
                  "Messages here are automatically forwarded from users who sent spam content."),
                message_thread_id=spam_topic_id,
                disable_notification=True
            )
            self.bot.pin_chat_message(self.group_id, pin_msg.message_id)
        except Exception as e:
            logger.error(_("Failed to create spam topic: {}").format(str(e)))
            raise

    def reset_spam_topic(self):
        """Reset spam topic by creating a new one."""
        try:
            # Clear old setting
            self.database.set_setting('spam_topic', None)
            self.cache.delete("spam_topic_id")

            # Create new topic
            self._create_spam_topic()
            return True
        except Exception as e:
            logger.error(_("Failed to reset spam topic: {}").format(str(e)))
            return False

    def _ensure_blocked_topic(self):
        """Ensure blocked messages topic exists, create if not."""
        self._create_or_load_blocked_topic()

    def _create_or_load_blocked_topic(self):
        """Create or load blocked messages topic."""
        blocked_topic_id = self.database.get_setting('blocked_topic')

        # If blocked topic ID is not set or is None, create a new topic
        if blocked_topic_id is None or blocked_topic_id == 'None':
            self._create_blocked_topic()
        else:
            # Load existing blocked topic ID into cache
            try:
                blocked_topic_id = int(blocked_topic_id)
                self.cache.set("blocked_topic_id", blocked_topic_id)
                logger.info(_("Blocked Messages topic loaded: {}").format(blocked_topic_id))
            except (ValueError, TypeError):
                logger.error(_("Invalid Blocked Messages topic ID in database: {}").format(blocked_topic_id))
                self._create_blocked_topic()

    def _create_blocked_topic(self):
        """Create a new Blocked Messages topic."""
        try:
            from telebot.apihelper import create_forum_topic
            logger.info(_("Creating Blocked Messages topic..."))
            topic = create_forum_topic(
                chat_id=self.group_id,
                name="🚫 Blocked Messages",
                token=self.bot.token
            )
            blocked_topic_id = topic["message_thread_id"]
            self.database.set_setting('blocked_topic', str(blocked_topic_id))
            self.cache.set("blocked_topic_id", blocked_topic_id)
            logger.info(_("Blocked Messages topic created with ID: {}").format(blocked_topic_id))

            # Send a pin message to the blocked topic (silently)
            pin_msg = self.bot.send_message(
                self.group_id,
                _("This topic is used to collect messages from blocked users.\n"
                  "Messages here are automatically forwarded from blocked users.\n"
                  "Admins can reply to a message and use /unban to unblock the user."),
                message_thread_id=blocked_topic_id,
                disable_notification=True
            )
            self.bot.pin_chat_message(self.group_id, pin_msg.message_id)
        except Exception as e:
            logger.error(_("Failed to create Blocked Messages topic: {}").format(str(e)))
            raise

    def reset_blocked_topic(self):
        """Reset blocked messages topic by creating a new one."""
        try:
            # Clear old setting
            self.database.set_setting('blocked_topic', None)
            self.cache.delete("blocked_topic_id")

            # Create new topic
            self._create_blocked_topic()
            return True
        except Exception as e:
            logger.error(_("Failed to reset Blocked Messages topic: {}").format(str(e)))
            return False

    def _dispatch_message(self, message):
        """Dispatch a rate-limited update after it has left the ingress queue."""
        if getattr(message, "content_type", None) == "text" and message.text:
            command = message.text.split(maxsplit=1)[0].split("@", 1)[0].lower()
            command_handlers = {
                "/ban": self.command_handler.ban_user,
                "/unban": self.command_handler.unban_user,
                "/terminate": self.command_handler.handle_terminate,
                "/delete": self.command_handler.delete_message,
                "/verify": self.command_handler.handle_verify,
            }
            if command in {"/start", "/help"}:
                self.command_handler.help_command(message, self.admin_handler.menu)
                return
            if handler := command_handlers.get(command):
                if (getattr(message.chat, "type", None) == "private"
                        and not self.message_handler.can_process_private_action(message)):
                    return
                handler(message)
                return
        self.message_handler.handle_message(message)

    def _dispatch_edited_message(self, message):
        """Mirror edits only after the private sender still passes verification."""
        if (getattr(message.chat, "type", None) == "private"
                and not self.message_handler.can_process_private_action(message)):
            return
        self.command_handler.handle_edit(message)

    def push_edited_message(self, message):
        """Queue edits so they cannot bypass private-message limits."""
        self.message_queue_manager.put(message, self._dispatch_edited_message)

    def push_reaction(self, message):
        """Queue reaction mirroring work with bounded group capacity."""
        self.message_queue_manager.put(message, self.command_handler.handle_reaction)

    def _cleanup_expired_rate_limit_blocks(self):
        """Periodically remove expired automatic rate-limit blocks."""
        cleanup_key = "rate_limit_block_cleanup"
        if self.cache.get(cleanup_key):
            return

        db = self.database.get_connection()
        try:
            cursor = db.cursor()
            cursor.execute(
                """DELETE FROM blocked_users
                   WHERE block_reason = 'rate_limit'
                     AND blocked_until IS NOT NULL
                     AND blocked_until <= CURRENT_TIMESTAMP"""
            )
            if cursor.rowcount:
                logger.info("Removed %s expired rate-limit blocks", cursor.rowcount)
        finally:
            db.close()
        self.cache.set(cleanup_key, True, 3600)

    def _is_rate_limit_verified(self, user_id: int) -> bool:
        """Use the cached verification state without database work on ingress."""
        return self.cache.get(f"verified_{user_id}") is True

    def _is_rate_limit_priority(self, user_id: int) -> bool:
        """Return whether an admin-replied user is still inside the activity window."""
        return self.cache.get(f"priority_user_{user_id}") is True

    def _touch_rate_limit_priority(self, user_id: int):
        """Extend priority while the replied user keeps the conversation active."""
        key = f"priority_user_{user_id}"
        if self.cache.get(key) is True:
            self.cache.set(key, True, expire=args.priority_inactivity_seconds)

    def mark_user_replied(self, user_id: int):
        """Promote a verified user after an administrator successfully replies."""
        self.cache.set(
            f"priority_user_{user_id}", True,
            expire=args.priority_inactivity_seconds,
        )
        self.message_queue_manager.mark_user_replied(user_id)
        logger.info("User %s promoted to active conversation limits", user_id)

    def _block_rate_limited_user(self, message):
        """Temporarily block repeated ingress-rate-limit violators."""
        if getattr(message.chat, "type", None) != "private":
            return

        self._cleanup_expired_rate_limit_blocks()
        user = message.from_user
        db = self.database.get_connection()
        try:
            cursor = db.cursor()
            existing = cursor.execute(
                "SELECT blocked_until FROM blocked_users WHERE user_id = ?",
                (user.id,)
            ).fetchone()
            permanent_block = existing and existing[0] is None
            cursor.execute(
                "DELETE FROM verified_users WHERE user_id = ?", (user.id,))
            if not permanent_block:
                cursor.execute(
                    """INSERT INTO blocked_users
                       (user_id, username, first_name, last_name, block_reason, blocked_at, blocked_until)
                       VALUES (?, ?, ?, ?, 'rate_limit', CURRENT_TIMESTAMP,
                               datetime('now', '+' || ? || ' seconds'))
                       ON CONFLICT(user_id) DO UPDATE SET
                           username = excluded.username,
                           first_name = excluded.first_name,
                           last_name = excluded.last_name,
                           block_reason = 'rate_limit',
                           blocked_at = CURRENT_TIMESTAMP,
                           blocked_until = excluded.blocked_until
                       WHERE blocked_users.blocked_until IS NOT NULL""",
                    (user.id, user.username, user.first_name, user.last_name,
                     args.abuse_block_seconds),
                )
        finally:
            db.close()
        self.cache.delete(f"verified_{user.id}")
        self.cache.delete(f"priority_user_{user.id}")
        self.message_queue_manager.revoke_user_priority(user.id)

    def push_messages(self, message):
        """Push messages to the queue for processing."""
        self.message_queue_manager.put(message)

    def get_queue_stats(self) -> dict:
        """Get current message queue statistics."""
        return self.message_queue_manager.get_stats()

    def stop(self):
        """Stop the bot and cleanup resources."""
        logger.info(_("Stopping bot..."))
        self.webapp_service.stop()
        self.message_queue_manager.stop()
        self.bot.stop_bot()
        logger.info(_("Bot stopped"))
