"""Callback query handling module."""

import json

from telebot import types

from src.config import logger, _


class CallbackHandler:
    """Handles callback queries from inline keyboards."""

    def __init__(self, bot, group_id: int, admin_handler, command_handler, captcha_manager, spam_detector=None,
                 db_path: str = "./data/storage.db"):
        self.bot = bot
        self.group_id = group_id
        self.admin_handler = admin_handler
        self.command_handler = command_handler
        self.captcha_manager = captcha_manager
        self.spam_detector = spam_detector
        self.db_path = db_path

    def handle_callback_query(self, call: types.CallbackQuery):
        """Main callback query handler."""
        if call.data == "null":
            logger.error(_("Invalid callback data received"))
            return

        try:
            data = json.loads(call.data)
            action = data["action"]
        except json.JSONDecodeError:
            logger.error(_("Invalid JSON data received"))
            return

        # User end callbacks answer themselves so they can provide a meaningful status.
        if action == "verify_button":
            self._handle_verify_button(call, data)
            return

        if action == "appeal_request":
            self._handle_appeal_request(call, data)
            return

        self.bot.answer_callback_query(call.id)

        # Admin end callbacks
        if call.message.chat.id != self.group_id:
            return

        self._handle_admin_callback(call, action, data)

    def _handle_verify_button(self, call: types.CallbackQuery, data: dict):
        """Reject legacy reusable verification buttons."""
        logger.info("Rejected deprecated button captcha for user %s", call.from_user.id)
        self.bot.answer_callback_query(call.id, _("This verification button has expired"))
        try:
            self.bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass

    def _handle_appeal_request(self, call: types.CallbackQuery, data: dict):
        """Start a user-bound verification challenge before submitting an appeal."""
        try:
            user_id = int(data.get("user_id"))
        except (TypeError, ValueError):
            self.bot.answer_callback_query(call.id, _("Invalid user ID"), show_alert=True)
            return
        if call.from_user.id != user_id or call.message.chat.id != user_id:
            logger.warning("Rejected appeal callback from user %s", call.from_user.id)
            self.bot.answer_callback_query(call.id, _("Invalid user ID"), show_alert=True)
            return

        import sqlite3

        try:
            with sqlite3.connect(self.db_path) as db:
                cursor = db.cursor()
                existing_appeal = cursor.execute(
                    "SELECT status FROM appeal_requests WHERE user_id = ?",
                    (user_id,)
                ).fetchone()
                if existing_appeal:
                    status = existing_appeal[0]
                    if status == "pending":
                        self.bot.answer_callback_query(
                            call.id, _("Your appeal is already pending review"), show_alert=True)
                        return
                    if status == "approved":
                        self.bot.answer_callback_query(
                            call.id, _("Your appeal was already approved"), show_alert=True)
                        return
                    if status == "rejected":
                        self.bot.answer_callback_query(
                            call.id,
                            _("Your appeal was already rejected. No further appeals allowed."),
                            show_alert=True,
                        )
                        return

                is_blocked = cursor.execute(
                    """SELECT block_reason FROM blocked_users
                       WHERE user_id = ?
                         AND (blocked_until IS NULL OR blocked_until > CURRENT_TIMESTAMP)""",
                    (user_id,)
                ).fetchone()
                if not is_blocked:
                    self.bot.answer_callback_query(call.id, _("You are not blocked"), show_alert=True)
                    return

            self.admin_handler.cache.set(f"appeal_verification_{user_id}", True, 300)
            service = self.captcha_manager.webapp_service
            captcha_type = self.admin_handler.cache.get("setting_captcha")
            if captcha_type == "webapp" and (not service or not service.is_enabled()):
                captcha_type = "image"
            if captcha_type not in {"webapp", "math", "image"}:
                captcha_type = "webapp" if service and service.is_enabled() else "math"

            if captcha_type == "webapp":
                challenge_url = service.create_challenge(user_id, purpose="appeal")
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton(
                    _("Verify"), web_app=types.WebAppInfo(url=challenge_url)))
                challenge_text = _(
                    "Appeal verification started. Complete the challenge below.")
                self.bot.edit_message_text(
                    challenge_text,
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=markup,
                )
            elif captcha_type == "math":
                captcha = self.captcha_manager.generate_captcha(user_id, "math")
                self.bot.edit_message_text(
                    _("Appeal verification started. Complete the challenge below.") + "\n\n" + captcha,
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=None,
                )
            else:
                self.captcha_manager.generate_captcha(user_id, "image")
                self.bot.edit_message_text(
                    _("Appeal verification started. Complete the challenge below."),
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=None,
                )

            self.bot.answer_callback_query(call.id, _("Please complete verification to submit appeal"))
            logger.info("Started appeal verification for user %s using %s", user_id, captcha_type)
        except Exception:
            self.admin_handler.cache.delete(f"appeal_verification_{user_id}")
            logger.exception("Could not start appeal verification for user %s", user_id)
            error_text = _("Could not start appeal verification. Please try again later.")
            self.bot.answer_callback_query(call.id, error_text, show_alert=True)
            try:
                self.bot.edit_message_text(
                    error_text,
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                logger.exception("Could not show appeal verification failure to user %s", user_id)

    def _handle_admin_callback(self, call: types.CallbackQuery, action: str, data: dict):
        """Handle admin callbacks."""
        markup = types.InlineKeyboardMarkup()
        back_button = types.InlineKeyboardButton("⬅️" + _("Back"),
                                                 callback_data=json.dumps({"action": "menu"}))

        match action:
            case "menu":
                self.admin_handler.menu(call.message, edit=True)
            case "auto_reply":
                self.admin_handler.auto_reply_menu(call.message)
            case "set_auto_response_time":
                self.admin_handler.handle_auto_response_time_callback(call.message, data)
            case "start_add_auto_reply":
                self.admin_handler.add_auto_response(call.message)
            case "add_auto_reply":
                self.admin_handler._finish_auto_response(
                    call.message.chat.id, call.message.message_id)
            case "manage_auto_reply":
                self.admin_handler.manage_auto_reply(call.message, page=data.get("page", 1))
            case "select_auto_reply":
                if "id" not in data:
                    self.bot.delete_message(self.group_id, call.message.message_id)
                    self.bot.send_message(self.group_id, _("Invalid action"), reply_markup=markup)
                    return
                self.admin_handler.select_auto_reply(call.message, data["id"])
            case "delete_auto_reply":
                if "id" not in data:
                    self.bot.delete_message(self.group_id, call.message.message_id)
                    self.bot.send_message(self.group_id, _("Invalid action"), reply_markup=markup)
                    return
                self.admin_handler.delete_auto_reply(call.message, data["id"])
            case "ban_user":
                self.admin_handler.manage_ban_user(call.message, page=data.get("page", 1))
            case "unban_user":
                if "id" not in data:
                    self.bot.delete_message(self.group_id, call.message.message_id)
                    self.bot.send_message(self.group_id, _("Invalid action"), reply_markup=markup)
                    return
                self.command_handler.unban_user(call.message, user_id=data["id"])
            case "select_ban_user":
                if "id" not in data:
                    self.bot.delete_message(self.group_id, call.message.message_id)
                    self.bot.send_message(self.group_id, _("Invalid action"), reply_markup=markup)
                    return
                self.admin_handler.select_ban_user(call.message, data["id"])
            case "default_msg":
                self.admin_handler.default_msg_menu(call.message)
            case "edit_default_msg":
                self.admin_handler.edit_default_msg(call.message)
            case "empty_default_msg":
                self.admin_handler.empty_default_msg(call.message)
            case "captcha_settings":
                self.admin_handler.captcha_settings_menu(call.message)
            case "set_captcha":
                self.admin_handler.set_captcha(call.message, data["value"])
            case "turnstile_settings":
                self.admin_handler.turnstile_settings_menu(call.message)
            case "set_turnstile_enabled":
                self.admin_handler.set_turnstile_enabled(call.message, data.get("value"))
            case "edit_turnstile_setting":
                self.admin_handler.edit_turnstile_setting(call.message, data.get("field"))
            case "broadcast_message":
                self.admin_handler.broadcast_message(call.message)
            case "confirm_broadcast":
                self.admin_handler.confirm_broadcast_message(call.message)
            case "cancel_broadcast":
                self.admin_handler.cancel_broadcast(call.message)
            case "time_zone_settings":
                self.admin_handler.time_zone_settings_menu(call.message)
            case "set_time_zone":
                self.admin_handler.set_time_zone(call.message, data.get("value", ""))
            case "edit_time_zone":
                self.admin_handler.edit_time_zone(call.message)
            case "set_verification":
                self.command_handler.set_verification_status(call.message, data.get("value", ""))
            case "confirm_terminate":
                try:
                    self.command_handler.terminate_thread(thread_id=data.get("thread_id"),
                                                          user_id=data.get("user_id"))
                except Exception:
                    logger.error(_("Failed to terminate the thread"))
                    self.bot.send_message(self.group_id, _("Failed to terminate the thread"))
            case "cancel_terminate":
                self.bot.edit_message_text(_("Operation cancelled"),
                                           call.message.chat.id, call.message.message_id)
            case "delete_banned_thread":
                if "thread_id" not in data:
                    self.bot.delete_message(self.group_id, call.message.message_id)
                    self.bot.send_message(self.group_id, _("Invalid action"), reply_markup=markup)
                    return
                self.bot.delete_message(self.group_id, call.message.message_id)
                try:
                    self.command_handler.terminate_thread(thread_id=data["thread_id"])
                    self.bot.send_message(self.group_id, _("Thread deleted"))
                except Exception as e:
                    logger.error(_("Failed to delete thread: {}").format(str(e)))
                    self.bot.send_message(self.group_id, _("Failed to delete thread"))
            case "spam_keywords":
                self.admin_handler.spam_keywords_menu(call.message)
            case "add_spam_keyword":
                self.admin_handler.add_spam_keyword(call.message)
            case "view_spam_keywords":
                self.admin_handler.view_spam_keywords(call.message, page=data.get("page", 1))
            case "select_spam_keyword":
                if "idx" not in data:
                    self.bot.delete_message(self.group_id, call.message.message_id)
                    self.bot.send_message(self.group_id, _("Invalid action"), reply_markup=markup)
                    return
                self.admin_handler.select_spam_keyword(call.message, data["idx"])
            case "delete_spam_keyword":
                if "idx" not in data:
                    self.bot.delete_message(self.group_id, call.message.message_id)
                    self.bot.send_message(self.group_id, _("Invalid action"), reply_markup=markup)
                    return
                self.admin_handler.delete_spam_keyword(call.message, data["idx"])
            case "blocked_reply_settings":
                self.admin_handler.blocked_reply_settings_menu(call.message)
            case "set_blocked_reply_enabled":
                if "value" not in data:
                    self.bot.delete_message(self.group_id, call.message.message_id)
                    self.bot.send_message(self.group_id, _("Invalid action"), reply_markup=markup)
                    return
                self.admin_handler.set_blocked_reply_enabled(call.message, data["value"])
            case "edit_blocked_reply_message":
                self.admin_handler.edit_blocked_reply_message(call.message)
            case "clear_blocked_reply_message":
                self.admin_handler.clear_blocked_reply_message(call.message)
            case "reset_spam_topic":
                self.admin_handler.reset_spam_topic(call.message)
            case "confirm_reset_spam_topic":
                self.admin_handler.confirm_reset_spam_topic(call.message)
            case "show_host_ip":
                self.admin_handler.show_host_ip(call.message)
            case "approve_appeal":
                if "user_id" not in data:
                    self.bot.delete_message(self.group_id, call.message.message_id)
                    self.bot.send_message(self.group_id, _("Invalid action"), reply_markup=markup)
                    return
                self._handle_approve_appeal(call, data["user_id"])
            case "reject_appeal":
                if "user_id" not in data:
                    self.bot.delete_message(self.group_id, call.message.message_id)
                    self.bot.send_message(self.group_id, _("Invalid action"), reply_markup=markup)
                    return
                self._handle_reject_appeal(call, data["user_id"])
            case "appeal_management":
                self.admin_handler.appeal_management_menu(call.message)
            case "view_pending_appeals":
                self.admin_handler.view_pending_appeals(call.message)
            case "view_all_appeals":
                self.admin_handler.view_all_appeals(call.message)
            case "toggle_appeal_mode":
                self.admin_handler.toggle_appeal_mode(call.message)
            case _:
                logger.error(_("Invalid action received") + action)

    def _handle_approve_appeal(self, call: types.CallbackQuery, user_id: int):
        """Handle admin approval of user appeal."""
        import sqlite3

        with sqlite3.connect(self.db_path) as db:
            cursor = db.cursor()

            # Update appeal status
            cursor.execute(
                """UPDATE appeal_requests
                   SET status = 'approved', admin_id = ?, handled_at = CURRENT_TIMESTAMP
                   WHERE user_id = ?""",
                (call.from_user.id, user_id)
            )

            # Unblock user
            cursor.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))

            # Reset verification attempts
            cursor.execute("DELETE FROM verification_attempts WHERE user_id = ?", (user_id,))

            db.commit()

            # Notify user
            self.bot.send_message(
                user_id,
                _("✅ Good news! Your appeal has been approved by an administrator.\n\n"
                  "You can now send messages again. Please complete the verification process.")
            )

            # Update admin message
            self.bot.edit_message_text(
                _("✅ Appeal Approved\n\n"
                  "User ID: {}\n"
                  "Approved by: {} (ID: {})\n"
                  "Action: User unblocked and verification attempts reset").format(
                    user_id, call.from_user.first_name, call.from_user.id
                ),
                call.message.chat.id,
                call.message.message_id
            )

            logger.info(_("Appeal approved for user {} by admin {}").format(user_id, call.from_user.id))

    def _handle_reject_appeal(self, call: types.CallbackQuery, user_id: int):
        """Handle admin rejection of user appeal."""
        import sqlite3

        with sqlite3.connect(self.db_path) as db:
            cursor = db.cursor()

            # Update appeal status
            cursor.execute(
                """UPDATE appeal_requests
                   SET status = 'rejected', admin_id = ?, handled_at = CURRENT_TIMESTAMP
                   WHERE user_id = ?""",
                (call.from_user.id, user_id)
            )

            db.commit()

            # Notify user
            self.bot.send_message(
                user_id,
                _("❌ Your appeal has been reviewed and rejected by an administrator.\n\n"
                  "The block remains in effect. No further appeals are allowed.")
            )

            # Update admin message
            self.bot.edit_message_text(
                _("❌ Appeal Rejected\n\n"
                  "User ID: {}\n"
                  "Rejected by: {} (ID: {})\n"
                  "Action: User remains blocked").format(
                    user_id, call.from_user.first_name, call.from_user.id
                ),
                call.message.chat.id,
                call.message.message_id
            )

            logger.info(_("Appeal rejected for user {} by admin {}").format(user_id, call.from_user.id))

