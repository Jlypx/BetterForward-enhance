"""Message handling module."""

import html
import sqlite3
import time

from telebot.apihelper import ApiTelegramException, create_forum_topic
from telebot.formatting import apply_html_entities
from telebot.types import Message

from src.config import logger, _
from src.utils.helpers import escape_markdown


class MessageHandler:
    """Handles message forwarding between users and group."""

    def __init__(self, bot, group_id: int, db_path: str, cache, captcha_manager, auto_response_manager,
                 spam_detector_manager=None, bot_instance=None):
        self.bot = bot
        self.group_id = group_id
        self.db_path = db_path
        self.cache = cache
        self.captcha_manager = captcha_manager
        self.auto_response_manager = auto_response_manager
        self.spam_detector_manager = spam_detector_manager
        self.bot_instance = bot_instance

    def check_valid_chat(self, message: Message) -> bool:
        """Check if message is in valid chat context."""
        return message.chat.id == self.group_id and message.message_thread_id is None

    def handle_message(self, message: Message):
        """Main message handler."""
        # Not responding in General topic
        if self.check_valid_chat(message):
            return

        if message.text:
            msg_text = apply_html_entities(message.text, message.entities, None) if message.entities else html.escape(
                message.text)
        else:
            msg_text = None

        if message.caption:
            msg_caption = apply_html_entities(message.caption, message.entities,
                                              None) if message.entities else html.escape(message.caption)
        else:
            msg_caption = None

        with sqlite3.connect(self.db_path) as db:
            cursor = db.cursor()

            if message.chat.id != self.group_id:
                self._handle_user_message(message, msg_text, msg_caption, cursor, db)
            else:
                self._handle_group_message(message, msg_text, msg_caption, cursor, db)

    def _handle_user_message(self, message: Message, msg_text: str, msg_caption: str, cursor, db):
        """Handle messages from users."""
        start_time = time.time()

        logger.info(
            _("Received message from {}, content: {}, type: {}").format(
                message.from_user.id, message.text, message.content_type))

        # Captcha handler
        if not self._check_captcha(message, cursor, db):
            processing_time = (time.time() - start_time) * 1000
            logger.info(_("Message from user {} blocked by captcha ({:.2f}ms)").format(
                message.from_user.id, processing_time))
            return

        # Check if the user is blocked
        is_blocked = cursor.execute("SELECT 1 FROM blocked_users WHERE user_id = ? LIMIT 1",
                                    (message.from_user.id,)).fetchone() is not None

        if is_blocked:
            # Update user info in blocked_users table
            cursor.execute(
                "UPDATE blocked_users SET username = ?, first_name = ?, last_name = ? WHERE user_id = ?",
                (message.from_user.username, message.from_user.first_name,
                 message.from_user.last_name, message.from_user.id)
            )

            processing_time = (time.time() - start_time) * 1000
            logger.info(_("Message from blocked user {} rejected ({:.2f}ms)").format(
                message.from_user.id, processing_time))

            # Send auto-reply if enabled
            if self.cache.get("setting_blocked_user_reply_enabled") == "enable":
                reply_message = self.cache.get("setting_blocked_user_reply_message")
                if reply_message:
                    try:
                        self.bot.send_message(message.chat.id, reply_message)
                        logger.info(_("Sent auto-reply to blocked user {}").format(message.from_user.id))
                    except Exception as e:
                        logger.error(_("Failed to send auto-reply to blocked user {}: {}").format(
                            message.from_user.id, str(e)))

            return

        # Check for spam using detector manager
        is_spam_detected = False
        spam_info = None

        if self.spam_detector_manager:
            is_spam_detected, spam_info = self.spam_detector_manager.detect_spam(message)

            if is_spam_detected:
                # Get spam topic ID from cache
                spam_topic_id = self.cache.get("spam_topic_id")
                if spam_topic_id is None:
                    # Fallback to main topic if spam topic not configured
                    spam_topic_id = None
                    logger.warning(_("Spam topic not configured, using main topic"))

                # Forward directly to spam topic without creating user thread
                try:
                    fwd_msg = self._send_message_by_type(message, msg_text, msg_caption,
                                                         self.group_id, spam_topic_id, None, silent=True)

                    # Build alert message based on detection info
                    alert_msg = f"🚫 {_('[Spam Detected]')}\n"
                    alert_msg += f"{_('User ID')}: {message.from_user.id}\n"

                    if spam_info:
                        if "detector" in spam_info:
                            alert_msg += f"{_('Detector')}: {spam_info['detector']}\n"
                        if "method" in spam_info:
                            alert_msg += f"{_('Method')}: {spam_info['method']}\n"
                        if "matched" in spam_info:
                            alert_msg += f"{_('Matched')}: {spam_info['matched']}\n"
                        if "confidence" in spam_info:
                            alert_msg += f"{_('Confidence')}: {spam_info['confidence']:.2%}\n"

                    self.bot.send_message(
                        self.group_id,
                        alert_msg,
                        message_thread_id=spam_topic_id,
                        reply_to_message_id=fwd_msg.message_id,
                        disable_notification=True
                    )
                except ApiTelegramException as e:
                    # If spam topic not found, try to recreate it
                    if "message thread not found" in str(e).lower() or "topic" in str(e).lower():
                        logger.warning(_("Spam topic not found, attempting to recreate..."))
                        if self.bot_instance:
                            try:
                                self.bot_instance._create_spam_topic()
                                spam_topic_id = self.cache.get("spam_topic_id")
                                logger.info(_("Spam topic recreated, retrying message forward..."))

                                # Retry forwarding
                                fwd_msg = self._send_message_by_type(message, msg_text, msg_caption,
                                                                     self.group_id, spam_topic_id, None, silent=True)

                                # Build and send alert message
                                alert_msg = f"🚫 {_('[Spam Detected]')}\n"
                                alert_msg += f"{_('User ID')}: {message.from_user.id}\n"
                                if spam_info:
                                    if "detector" in spam_info:
                                        alert_msg += f"{_('Detector')}: {spam_info['detector']}\n"
                                    if "method" in spam_info:
                                        alert_msg += f"{_('Method')}: {spam_info['method']}\n"
                                    if "matched" in spam_info:
                                        alert_msg += f"{_('Matched')}: {spam_info['matched']}\n"

                                self.bot.send_message(
                                    self.group_id,
                                    alert_msg,
                                    message_thread_id=spam_topic_id,
                                    reply_to_message_id=fwd_msg.message_id,
                                    disable_notification=True
                                )
                            except Exception as retry_error:
                                logger.error(
                                    _("Failed to recreate spam topic and forward message: {}").format(str(retry_error)))
                                # Fallback to main topic
                                self.bot.send_message(
                                    self.group_id,
                                    f"⚠️ {_('[Spam - Topic Error]')}\n{_('User ID')}: {message.from_user.id}",
                                    message_thread_id=None,
                                    disable_notification=True
                                )
                        else:
                            logger.error(_("Cannot recreate spam topic: bot instance not available"))
                    else:
                        logger.error(_("Failed to forward spam message: {}").format(str(e)))

                # Log processing time
                processing_time = (time.time() - start_time) * 1000
                logger.info(_("Spam message from user {} processed in {:.2f}ms (matched: {})").format(
                    message.from_user.id, processing_time, spam_info.get('matched', 'unknown')))

                # Done, return early
                return

        # Auto response
        auto_response = self._handle_auto_response(message)

        # Forward message to group
        thread_id = self._get_or_create_thread(message, cursor, db)
        if thread_id is None:
            return

        # Forward the message
        fwd_msg = self._forward_to_group(message, msg_text, msg_caption, thread_id, cursor)
        if fwd_msg is None:
            return

        if auto_response is not None:
            self.bot.send_message(self.group_id, _("[Auto Response]") + auto_response,
                                  message_thread_id=thread_id)

        # Log processing time
        processing_time = (time.time() - start_time) * 1000  # Convert to milliseconds
        logger.info(_("Message from user {} processed in {:.2f}ms").format(
            message.from_user.id, processing_time))

    def _check_captcha(self, message: Message, cursor, db) -> bool:
        """Check and handle captcha verification."""
        if self.cache.get("setting_captcha") == "disable":
            return True

        user_id = message.from_user.id

        # FIRST: Check if user is blocked - if so, don't generate any captcha
        is_blocked = cursor.execute(
            "SELECT 1 FROM blocked_users WHERE user_id = ? LIMIT 1",
            (user_id,)
        ).fetchone() is not None

        if is_blocked:
            # Check if user is in appeal verification mode
            appeal_verification = self.cache.get(f"appeal_verification_{user_id}")
            if appeal_verification:
                # User is answering appeal verification captcha
                if self.captcha_manager.verify_captcha(user_id, message.text):
                    # Correct answer - proceed with appeal submission
                    logger.info(_("User {} passed appeal verification").format(user_id))
                    self.cache.delete(f"appeal_verification_{user_id}")
                    self.cache.delete(f"captcha_{user_id}")

                    # Submit appeal
                    self._submit_appeal(user_id, message.from_user, db, cursor)
                    return False
                else:
                    # Wrong answer
                    self.bot.send_message(
                        message.chat.id,
                        _("❌ Incorrect answer. Appeal verification failed.\n\n"
                          "Please try again by clicking the Appeal button."),
                        reply_to_message_id=message.message_id
                    )
                    self.cache.delete(f"appeal_verification_{user_id}")
                    self.cache.delete(f"captcha_{user_id}")
                    return False
            else:
                # User is blocked, send message with appeal button
                import json
                from telebot import types
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton(
                    _("Appeal"),
                    callback_data=json.dumps({"action": "appeal_request", "user_id": user_id})
                ))
                self.bot.send_message(
                    message.chat.id,
                    _("❌ Your account has been blocked. If you believe this is a mistake, you can submit an appeal (one-time opportunity)."),
                    reply_markup=markup
                )
                return False

        # Captcha Handler - User is answering a captcha
        if (captcha := self.cache.get(f"captcha_{user_id}")) is not None:
            if not self.captcha_manager.verify_captcha(user_id, message.text):
                # Wrong answer - record attempt
                attempt_count = self.captcha_manager.record_attempt(user_id, db)
                logger.info(_("User {} entered incorrect answer (attempt {}/3)").format(user_id, attempt_count))

                # Check if user has exceeded max attempts
                if attempt_count >= 3:
                    # Auto-block user
                    self.captcha_manager.block_user_by_attempts(
                        user_id,
                        message.from_user.username,
                        message.from_user.first_name,
                        message.from_user.last_name,
                        db
                    )
                    logger.warning(_("User {} auto-blocked after 3 failed attempts").format(user_id))

                    # Send notification with appeal button
                    import json
                    from telebot import types
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton(
                        _("Appeal"),
                        callback_data=json.dumps({"action": "appeal_request", "user_id": user_id})
                    ))
                    self.bot.send_message(
                        user_id,
                        _("❌ You have been blocked after 3 failed verification attempts.\n\n"
                          "If you believe this is a mistake, you can submit an appeal (one-time opportunity)."),
                        reply_markup=markup
                    )
                    self.cache.delete(f"captcha_{user_id}")
                    return False

                # Generate new captcha (don't reuse the same one)
                captcha_type = self.cache.get("setting_captcha")
                new_captcha = self.captcha_manager.generate_captcha(user_id, captcha_type)

                if new_captcha:  # For math/image captcha
                    self.bot.send_message(
                        message.chat.id,
                        _("❌ Incorrect answer ({}/3 attempts).\n\nPlease try again:\n{}").format(
                            attempt_count, new_captcha
                        ),
                        reply_to_message_id=message.message_id
                    )
                return False

            # Correct answer - verify user
            logger.info(_("User {} passed the captcha").format(user_id))
            self.bot.send_message(message.chat.id, _("✅ Verification successful! You can now send messages."))
            self.captcha_manager.set_user_verified(user_id, db)
            self.captcha_manager.reset_attempts(user_id, db)  # Reset attempt counter
            self.cache.delete(f"captcha_{user_id}")
            return False

        # Check if the user is verified
        if not self.captcha_manager.is_user_verified(user_id, db):
            logger.info(_("User {} is not verified").format(user_id))

            # Rate limiting - prevent spam verification requests
            rate_limit_key = f"captcha_rate_limit_{user_id}"
            if self.cache.get(rate_limit_key):
                logger.info(_("User {} rate limited for captcha requests").format(user_id))
                return False

            # Set rate limit (10 seconds)
            self.cache.set(rate_limit_key, True, 10)

            # First, reply to user's message to make it clear the message was not sent
            self.bot.send_message(message.chat.id,
                                  _("⚠️ Your message was not sent. Please complete verification first."),
                                  reply_to_message_id=message.message_id)

            captcha_type = self.cache.get("setting_captcha")
            match captcha_type:
                case "button":
                    self.captcha_manager.generate_captcha(user_id, captcha_type)
                    return False
                case "math":
                    captcha = self.captcha_manager.generate_captcha(user_id, captcha_type)
                    self.bot.send_message(message.chat.id,
                                          _("Please solve the following math problem and send the answer:\n\n") + captcha)
                    return False
                case "image":
                    self.captcha_manager.generate_captcha(user_id, captcha_type)
                    return False
                case _:
                    logger.error(_("Invalid captcha setting"))
                    self.bot.send_message(self.group_id,
                                          _("Invalid captcha setting") + f": {self.cache.get('setting_captcha')}")
                    return False
        return True

    def _submit_appeal(self, user_id: int, user, db, cursor):
        """Submit an appeal request after successful verification."""
        import json
        from telebot import types

        # Check if user has already appealed
        existing_appeal = cursor.execute(
            "SELECT status FROM appeal_requests WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if existing_appeal:
            status = existing_appeal[0]
            if status == 'pending':
                self.bot.send_message(user_id, _("Your appeal is already pending review"))
                return
            elif status == 'approved':
                self.bot.send_message(user_id, _("Your appeal was already approved"))
                return
            elif status == 'rejected':
                self.bot.send_message(user_id, _("Your appeal was already rejected. No further appeals allowed."))
                return

        # Record appeal request
        cursor.execute(
            """INSERT INTO appeal_requests (user_id, appeal_time, status)
               VALUES (?, CURRENT_TIMESTAMP, 'pending')""",
            (user_id,)
        )
        db.commit()

        # Notify user
        self.bot.send_message(
            user_id,
            _("✅ Your appeal has been submitted and is pending admin review.\n\n"
              "You will be notified once a decision is made.")
        )

        # Get user info
        username = user.username or "N/A"
        first_name = user.first_name or ""
        last_name = user.last_name or ""
        full_name = f"{first_name} {last_name}".strip() or "N/A"

        appeal_mode = self.cache.get("setting_appeal_mode") or "manual"

        if appeal_mode == "auto":
            # Auto-approve: unblock user immediately but mark as "on watch"
            cursor.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))
            cursor.execute(
                """UPDATE appeal_requests
                   SET status = 'approved', handled_at = CURRENT_TIMESTAMP
                   WHERE user_id = ?""",
                (user_id,)
            )
            db.commit()

            self.bot.send_message(
                user_id,
                _("✅ Your appeal has been automatically approved.\n\n"
                  "⚠️ Note: If you fail verification again, you will be permanently blocked.")
            )

            self.bot.send_message(
                self.group_id,
                _("🔔 Auto-Appeal Notification\n\n"
                  "User: {} (ID: {})\n"
                  "Username: @{}\n"
                  "Status: ✅ Automatically approved\n\n"
                  "⚠️ User is now on watch. Next violation will result in permanent block.").format(
                    full_name, user_id, username
                )
            )
        else:
            # Manual mode: send to admin for approval
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton(
                    _("✅ Approve"),
                    callback_data=json.dumps({"action": "approve_appeal", "user_id": user_id})
                ),
                types.InlineKeyboardButton(
                    _("❌ Reject"),
                    callback_data=json.dumps({"action": "reject_appeal", "user_id": user_id})
                )
            )

            self.bot.send_message(
                self.group_id,
                _("🔔 New Appeal Request\n\n"
                  "User: {} (ID: {})\n"
                  "Username: @{}\n"
                  "Reason: Auto-blocked after 3 failed verification attempts\n\n"
                  "Please review and decide:").format(full_name, user_id, username),
                reply_markup=markup
            )

    def _handle_auto_response(self, message: Message):
        """Handle automatic responses."""
        if (auto_response_result := self.auto_response_manager.match_auto_response(message.text)) is not None:
            match auto_response_result["type"]:
                case "text":
                    self.bot.send_message(message.chat.id, auto_response_result["response"])
                case "photo":
                    self.bot.send_photo(message.chat.id, photo=auto_response_result["response"])
                case "sticker":
                    self.bot.send_sticker(message.chat.id, sticker=auto_response_result["response"])
                case "video":
                    self.bot.send_video(message.chat.id, video=auto_response_result["response"])
                case "document":
                    self.bot.send_document(message.chat.id, document=auto_response_result["response"])
                case _:
                    logger.error(_("Unsupported message type") + auto_response_result["type"])
            return auto_response_result["response"]
        return None

    def _get_or_create_thread(self, message: Message, cursor, db) -> int:
        """Get or create a thread for the user."""
        userid = message.from_user.id
        if (thread_id := self.cache.get(f"chat_{userid}_threadid")) is None:
            result = cursor.execute("SELECT thread_id FROM topics WHERE user_id = ? LIMIT 1", (userid,))
            thread_id = result.fetchone()
            if thread_id is None:
                # Create a new thread
                logger.info(_("Creating a new thread for user {}").format(userid))
                try:
                    topic = create_forum_topic(chat_id=self.group_id,
                                               name=f"{message.from_user.first_name} | {userid}",
                                               token=self.bot.token)
                except Exception as e:
                    logger.error(e)
                    return None
                cursor.execute("INSERT INTO topics (user_id, thread_id) VALUES (?, ?)",
                               (userid, topic["message_thread_id"]))
                db.commit()
                thread_id = topic["message_thread_id"]

                # Send and pin user info message asynchronously to avoid blocking
                try:
                    username = _("Not set") if message.from_user.username is None else f"@{message.from_user.username}"
                    last_name = "" if message.from_user.last_name is None else f" {message.from_user.last_name}"
                    pin_message = self.bot.send_message(self.group_id,
                                                        f"User ID: [{userid}](tg://openmessage?user_id={userid})\n"
                                                        f"Full Name: {escape_markdown(f'{message.from_user.first_name}{last_name}')}\n"
                                                        f"Username: {escape_markdown(username)}\n",
                                                        message_thread_id=thread_id, parse_mode='markdown')
                    self.bot.pin_chat_message(self.group_id, pin_message.message_id)
                except Exception as e:
                    # Don't fail message forwarding if pinning fails
                    logger.warning(f"Failed to pin info message for user {userid}: {e}")
            else:
                thread_id = thread_id[0]
            self.cache.set(f"chat_{userid}_threadid", thread_id)
        return thread_id

    def _forward_to_group(self, message: Message, msg_text: str, msg_caption: str,
                          thread_id: int, cursor) -> Message:
        """Forward a message to the group."""
        try:
            reply_id = self._get_reply_id(message, thread_id, cursor, in_group=False)
            fwd_msg = self._send_message_by_type(message, msg_text, msg_caption,
                                                 self.group_id, thread_id, reply_id)
            cursor.execute(
                "INSERT INTO messages (received_id, forwarded_id, topic_id, in_group) VALUES (?, ?, ?, ?)",
                (message.message_id, fwd_msg.message_id, thread_id, False))
            return fwd_msg
        except ApiTelegramException as e:
            if "message thread not found" in str(e):
                cursor.execute("DELETE FROM topics WHERE thread_id = ?", (thread_id,))
                cursor.connection.commit()
                self.cache.delete(f"threadid_{thread_id}_userid")
                self.cache.delete(f"chat_{message.from_user.id}_threadid")
                # Re-queue the message
                return None
            logger.error(_("Failed to forward message from user {}").format(message.from_user.id))
            logger.error(e)
            self.bot.send_message(self.group_id,
                                  _("Failed to forward message from user {}").format(message.from_user.id),
                                  message_thread_id=None)
            self.bot.forward_message(self.group_id, message.chat.id, message_id=message.message_id)
            return None

    def _handle_group_message(self, message: Message, msg_text: str, msg_caption: str, cursor, db):
        """Handle messages from group to users."""
        if (user_id := self.cache.get(f"threadid_{message.message_thread_id}_userid")) is None:
            result = cursor.execute("SELECT user_id FROM topics WHERE thread_id = ? LIMIT 1",
                                    (message.message_thread_id,))
            user_id = result.fetchone()
            user_id = user_id[0] if user_id is not None else None

        if user_id is not None:
            self.cache.set(f"threadid_{message.message_thread_id}_userid", user_id)
            reply_id = self._get_reply_id(message, message.message_thread_id, cursor, in_group=True)

            try:
                fwd_msg = self._send_message_by_type(message, msg_text, msg_caption,
                                                     user_id, None, reply_id)
                cursor.execute(
                    "INSERT INTO messages (received_id, forwarded_id, topic_id, in_group) VALUES (?, ?, ?, ?)",
                    (message.message_id, fwd_msg.message_id, message.message_thread_id, True))
            except ApiTelegramException as e:
                logger.error(_("Failed to forward message to user {}").format(user_id))
                logger.error(e)
                self.bot.send_message(self.group_id,
                                      _("[Alert]") + _("Failed to forward message to user {}").format(
                                          user_id) + "\n" + str(e),
                                      message_thread_id=message.message_thread_id)
        else:
            self.bot.send_message(self.group_id, _("Chat not found, please remove this topic manually"),
                                  message_thread_id=message.message_thread_id)
            try:
                from telebot.apihelper import close_forum_topic
                close_forum_topic(chat_id=self.group_id, message_thread_id=message.message_thread_id,
                                  token=self.bot.token)
            except ApiTelegramException:
                pass

    def _get_reply_id(self, message: Message, topic_id: int, cursor, in_group: bool):
        """Get the reply message ID if replying to a message."""
        if message.reply_to_message is None:
            return None

        if message.reply_to_message.from_user.id == message.from_user.id:
            cursor.execute(
                "SELECT forwarded_id FROM messages WHERE received_id = ? AND topic_id = ? AND in_group = ? LIMIT 1",
                (message.reply_to_message.message_id, topic_id, in_group))
        else:
            cursor.execute(
                "SELECT received_id FROM messages WHERE forwarded_id = ? AND topic_id = ? AND in_group = ? LIMIT 1",
                (message.reply_to_message.message_id, topic_id, not in_group))

        if (result := cursor.fetchone()) is not None:
            return int(result[0])
        return None

    def _send_message_by_type(self, message: Message, msg_text: str, msg_caption: str,
                              chat_id: int, thread_id: int = None, reply_id: int = None,
                              silent: bool = False) -> Message:
        """Send a message based on its type."""
        match message.content_type:
            case "photo":
                return self.bot.send_photo(chat_id=chat_id, photo=message.photo[-1].file_id,
                                           caption=msg_caption, message_thread_id=thread_id,
                                           reply_to_message_id=reply_id, parse_mode='HTML',
                                           disable_notification=silent)
            case "text":
                return self.bot.send_message(chat_id=chat_id, text=msg_text,
                                             message_thread_id=thread_id,
                                             reply_to_message_id=reply_id, parse_mode='HTML',
                                             disable_notification=silent)
            case "sticker":
                return self.bot.send_sticker(chat_id=chat_id, sticker=message.sticker.file_id,
                                             message_thread_id=thread_id,
                                             reply_to_message_id=reply_id,
                                             disable_notification=silent)
            case "video":
                return self.bot.send_video(chat_id=chat_id, video=message.video.file_id,
                                           caption=msg_caption, message_thread_id=thread_id,
                                           reply_to_message_id=reply_id, parse_mode='HTML',
                                           disable_notification=silent)
            case "document":
                return self.bot.send_document(chat_id=chat_id, document=message.document.file_id,
                                              caption=msg_caption, message_thread_id=thread_id,
                                              reply_to_message_id=reply_id, parse_mode='HTML',
                                              disable_notification=silent)
            case "audio":
                return self.bot.send_audio(chat_id=chat_id, audio=message.audio.file_id,
                                           caption=msg_caption, message_thread_id=thread_id,
                                           reply_to_message_id=reply_id, parse_mode='HTML',
                                           disable_notification=silent)
            case "voice":
                return self.bot.send_voice(chat_id=chat_id, voice=message.voice.file_id,
                                           caption=msg_caption, message_thread_id=thread_id,
                                           reply_to_message_id=reply_id, parse_mode='HTML',
                                           disable_notification=silent)
            case "animation":
                return self.bot.send_animation(chat_id=chat_id, animation=message.animation.file_id,
                                               caption=msg_caption, message_thread_id=thread_id,
                                               reply_to_message_id=reply_id, parse_mode='HTML',
                                               disable_notification=silent)
            case "contact":
                return self.bot.send_contact(chat_id=chat_id,
                                             phone_number=message.contact.phone_number,
                                             first_name=message.contact.first_name,
                                             last_name=message.contact.last_name,
                                             message_thread_id=thread_id,
                                             reply_to_message_id=reply_id,
                                             disable_notification=silent)
            case _:
                logger.error(_("Unsupported message type") + message.content_type)
                raise ValueError(_("Unsupported message type") + message.content_type)
