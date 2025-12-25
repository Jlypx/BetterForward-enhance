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

        self.bot.answer_callback_query(call.id)

        # User end callbacks
        if action == "verify_button":
            self._handle_verify_button(call, data)
            return

        if action == "appeal_request":
            self._handle_appeal_request(call, data)
            return

        # Admin end callbacks
        if call.message.chat.id != self.group_id:
            return

        self._handle_admin_callback(call, action, data)

    def _handle_verify_button(self, call: types.CallbackQuery, data: dict):
        """Handle button captcha verification."""
        user_id = data.get("user_id")
        if user_id:
            import sqlite3
            db_path = "./data/storage.db"
            with sqlite3.connect(db_path) as db:
                self.captcha_manager.set_user_verified(user_id, db)
                self.captcha_manager.reset_attempts(user_id, db)  # Reset attempts on manual verification
            self.bot.answer_callback_query(call.id)
            self.bot.send_message(user_id, _("Verification successful, you can now send messages"))
            self.bot.delete_message(call.message.chat.id, call.message.message_id)
        else:
            self.bot.answer_callback_query(call.id)
            self.bot.send_message(call.message.chat.id, _("Invalid user ID"))

    def _handle_appeal_request(self, call: types.CallbackQuery, data: dict):
        """Handle user appeal request after being auto-blocked. Requires verification first."""
        user_id = data.get("user_id")
        if not user_id:
            self.bot.answer_callback_query(call.id, _("Invalid user ID"))
            return

        import sqlite3

        with sqlite3.connect(self.db_path) as db:
            cursor = db.cursor()

            # Check if user has already appealed
            existing_appeal = cursor.execute(
                "SELECT status FROM appeal_requests WHERE user_id = ?",
                (user_id,)
            ).fetchone()

            if existing_appeal:
                status = existing_appeal[0]
                if status == 'pending':
                    self.bot.answer_callback_query(call.id, _("Your appeal is already pending review"))
                    return
                elif status == 'approved':
                    self.bot.answer_callback_query(call.id, _("Your appeal was already approved"))
                    return
                elif status == 'rejected':
                    self.bot.answer_callback_query(call.id, _("Your appeal was already rejected. No further appeals allowed."))
                    return

            # Check if user is actually blocked
            is_blocked = cursor.execute(
                "SELECT block_reason FROM blocked_users WHERE user_id = ?",
                (user_id,)
            ).fetchone()

            if not is_blocked:
                self.bot.answer_callback_query(call.id, _("You are not blocked"))
                return

            # Set appeal verification mode flag
            self.admin_handler.cache.set(f"appeal_verification_{user_id}", True, 300)  # 5 minutes timeout

            # Generate captcha challenge based on current settings
            captcha_type = self.admin_handler.cache.get("setting_captcha") or "math"
            from src.utils.captcha import CaptchaManager
            captcha_manager = CaptchaManager(self.bot, self.admin_handler.cache)  # Fixed: bot first, cache second

            # Generate the captcha
            self.bot.answer_callback_query(call.id, _("Please complete verification to submit appeal"))

            match captcha_type:
                case "button":
                    captcha_manager.generate_captcha(user_id, captcha_type)
                    self.bot.send_message(
                        user_id,
                        _("🔐 Appeal Verification Required\n\n"
                          "To submit your appeal, please complete the verification challenge below.")
                    )
                case "math":
                    captcha = captcha_manager.generate_captcha(user_id, captcha_type)
                    self.bot.send_message(
                        user_id,
                        _("🔐 Appeal Verification Required\n\n"
                          "To submit your appeal, please solve the following math problem and send the answer:\n\n") + captcha
                    )
                case "image":
                    captcha_manager.generate_captcha(user_id, captcha_type)
                    self.bot.send_message(
                        user_id,
                        _("🔐 Appeal Verification Required\n\n"
                          "To submit your appeal, please complete the verification challenge below.")
                    )
                case _:
                    # Default to math if setting is invalid
                    captcha = captcha_manager.generate_captcha(user_id, "math")
                    self.bot.send_message(
                        user_id,
                        _("🔐 Appeal Verification Required\n\n"
                          "To submit your appeal, please solve the following math problem and send the answer:\n\n") + captcha
                    )


            logger.info(_("Appeal request submitted by user {}").format(user_id))

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
                self.admin_handler.process_add_auto_reply(call.message)
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
            case "broadcast_message":
                self.admin_handler.broadcast_message(call.message)
            case "confirm_broadcast":
                self.bot.delete_message(self.group_id, call.message.message_id)
                self.admin_handler.confirm_broadcast_message(call)
            case "cancel_broadcast":
                self.bot.delete_message(self.group_id, call.message.message_id)
                self.bot.send_message(self.group_id, _("Broadcast cancelled"))
                self.admin_handler.cancel_broadcast()
            case "time_zone_settings":
                self.admin_handler.time_zone_settings_menu(call.message)
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

