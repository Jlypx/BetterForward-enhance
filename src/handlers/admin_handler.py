"""Admin functionality handling module."""

import json
import re
import sqlite3
import httpx
from datetime import datetime
from urllib.parse import urlsplit

import pytz
from telebot import types
from telebot.apihelper import ApiTelegramException
from telebot.types import Message

from src.config import logger, _
from src.utils.blocking import (
    format_block_duration,
    format_block_expiry,
    remaining_block_seconds,
)


class AdminHandler:
    """Handles administrative functions like settings, menus, and broadcasts."""

    def __init__(self, bot, group_id: int, db_path: str, cache, database, auto_response_manager,
                 spam_keyword_manager=None, bot_instance=None):
        self.bot = bot
        self.group_id = group_id
        self.db_path = db_path
        self.cache = cache
        self.database = database
        self.auto_response_manager = auto_response_manager
        self.spam_keyword_manager = spam_keyword_manager
        self.bot_instance = bot_instance
        # Get timezone from cache
        tz_str = self.cache.get("setting_time_zone")
        self.time_zone = pytz.timezone(tz_str) if tz_str else pytz.UTC

    def check_valid_chat(self, message: Message) -> bool:
        """Check if message is in valid chat context."""
        return message.chat.id == self.group_id and message.message_thread_id is None

    def _edit_panel(self, message: Message, text: str, markup=None):
        """Update an existing admin panel and remember it for command entry points."""
        return self._edit_panel_by_id(message.chat.id, message.message_id, text, markup)

    def _edit_panel_by_id(self, chat_id: int, message_id: int, text: str, markup=None):
        """Update one admin panel without creating another group message."""
        try:
            updated = self.bot.edit_message_text(
                text, chat_id, message_id, reply_markup=markup)
            self.cache.set("admin_panel_message_id", message_id)
            return updated
        except ApiTelegramException as error:
            logger.warning("Could not update admin panel %s: %s", message_id, error)
            return None

    def _delete_admin_input(self, message: Message):
        """Remove an administrator setting value once it has been read."""
        if not self.check_valid_chat(message):
            return
        try:
            self.bot.delete_message(message.chat.id, message.message_id)
        except ApiTelegramException:
            logger.debug("Could not delete admin input message %s", message.message_id)

    def _panel_back_button(self, action: str = "menu"):
        return types.InlineKeyboardButton(
            "⬅️" + _("Back"), callback_data=json.dumps({"action": action}))

    def update_time_zone(self):
        """Update the timezone from cache and propagate to auto_response_manager."""
        tz_str = self.cache.get("setting_time_zone")
        if tz_str:
            self.time_zone = pytz.timezone(tz_str)
            self.auto_response_manager.update_time_zone(self.time_zone)
        else:
            self.time_zone = pytz.UTC

    def _append_temporary_block_status(self, text: str, blocked_until) -> str:
        """Append expiry and remaining time for an active temporary block."""
        expiry = format_block_expiry(blocked_until, self.time_zone)
        if not expiry:
            return text
        text += f"{_('Temporary block expires at')}: {expiry}\n"
        text += f"{_('Remaining block time')}: {format_block_duration(remaining_block_seconds(blocked_until))}\n"
        return text

    def menu(self, message: Message, edit: bool = False):
        """Display the main admin menu."""
        if not self.check_valid_chat(message):
            return

        markup = types.InlineKeyboardMarkup()
        buttons = [
            types.InlineKeyboardButton("💬" + _("Auto Reply"),
                                       callback_data=json.dumps({"action": "auto_reply"})),
            types.InlineKeyboardButton("📙" + _("Default Message"),
                                       callback_data=json.dumps({"action": "default_msg"})),
            types.InlineKeyboardButton("⛔" + _("Banned Users"),
                                       callback_data=json.dumps({"action": "ban_user"})),
            types.InlineKeyboardButton("🚫" + _("Spam Keywords"),
                                       callback_data=json.dumps({"action": "spam_keywords"})),
            types.InlineKeyboardButton("🚷" + _("Blocked User Reply"),
                                       callback_data=json.dumps({"action": "blocked_reply_settings"})),
            types.InlineKeyboardButton("🔒" + _("Captcha Settings"),
                                       callback_data=json.dumps({"action": "captcha_settings"})),
            types.InlineKeyboardButton("🛡" + _("Turnstile WebApp"),
                                       callback_data=json.dumps({"action": "turnstile_settings"})),
            types.InlineKeyboardButton("📝" + _("Appeal Management"),
                                       callback_data=json.dumps({"action": "appeal_management"})),
            types.InlineKeyboardButton("🌍" + _("Time Zone Settings"),
                                       callback_data=json.dumps({"action": "time_zone_settings"})),
            types.InlineKeyboardButton("📢" + _("Broadcast Message"),
                                       callback_data=json.dumps({"action": "broadcast_message"})),
            types.InlineKeyboardButton("📡" + _("Show Host IP Info"),
                                       callback_data=json.dumps({"action": "show_host_ip"}))
        ]

        for i in range(0, len(buttons), 2):
            markup.row(*buttons[i:i + 2])

        if edit:
            self._edit_panel(message, _("Menu"), markup)
            return

        panel_id = self.cache.get("admin_panel_message_id")
        if panel_id and self._edit_panel_by_id(self.group_id, panel_id, _("Menu"), markup):
            self._delete_admin_input(message)
            return
        panel = self.bot.send_message(
            self.group_id, _("Menu"), reply_markup=markup, message_thread_id=None)
        self.cache.set("admin_panel_message_id", panel.message_id)
        self._delete_admin_input(message)

    # Auto Reply Management
    def auto_reply_menu(self, message: Message):
        """Display auto reply controls in the existing admin panel."""
        if self.check_valid_chat(message):
            self.auto_reply_menu_by_id(message.chat.id, message.message_id)

    def _clear_auto_response_draft(self):
        for key in (
                "auto_response_key", "auto_response_value", "auto_response_regex",
                "auto_response_type", "auto_response_start_time", "auto_response_end_time"):
            self.cache.delete(key)

    def add_auto_response(self, message: Message):
        """Start an auto-reply draft in the existing admin panel."""
        if not self.check_valid_chat(message):
            return
        self._clear_auto_response_draft()
        prompt = self._edit_panel(
            message,
            _("Let's set up an automatic response.\nSend /cancel to cancel this operation.\n\n"
              "Please send the keywords or regular expression that should trigger this response."),
        )
        if prompt:
            self.bot.register_next_step_handler(
                prompt, self.add_auto_response_type, message.message_id)

    def add_auto_response_type(self, message: Message, panel_message_id: int):
        """Store an auto-reply trigger and remove the intermediate input."""
        if not self.check_valid_chat(message):
            return
        text = message.text if message.content_type == "text" else None
        self._delete_admin_input(message)
        if text is None:
            self._clear_auto_response_draft()
            self._edit_panel_by_id(
                message.chat.id, panel_message_id, _("Invalid input"),
                types.InlineKeyboardMarkup().add(self._panel_back_button("auto_reply")))
            return
        if text.startswith("/cancel"):
            self._clear_auto_response_draft()
            self.auto_reply_menu_by_id(message.chat.id, panel_message_id, _("Operation cancelled"))
            return

        self.cache.set("auto_response_key", text, 300)
        try:
            re.compile(text)
            is_regex = True
        except re.error:
            is_regex = False
        self.cache.set("auto_response_regex", is_regex, 300)
        self._prompt_auto_response_value(message.chat.id, panel_message_id)

    def auto_reply_menu_by_id(self, chat_id: int, message_id: int, notice: str | None = None):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "➕" + _("Add Auto Reply"),
            callback_data=json.dumps({"action": "start_add_auto_reply"})))
        markup.add(types.InlineKeyboardButton(
            "⚙️" + _("Manage Existing Auto Reply"),
            callback_data=json.dumps({"action": "manage_auto_reply"})))
        markup.add(self._panel_back_button())
        text = _("Auto Reply") if not notice else notice + "\n\n" + _("Auto Reply")
        return self._edit_panel_by_id(chat_id, message_id, text, markup)

    def _prompt_auto_response_value(self, chat_id: int, panel_message_id: int):
        key = self.cache.get("auto_response_key")
        if key is None:
            self.auto_reply_menu_by_id(
                chat_id, panel_message_id,
                _("The operation has timed out. Please initiate the process again."))
            return
        if self.cache.get("auto_response_regex"):
            try:
                re.compile(key)
            except re.error:
                self._clear_auto_response_draft()
                self.auto_reply_menu_by_id(chat_id, panel_message_id, _("Invalid regular expression"))
                return
        prompt = self._edit_panel_by_id(
            chat_id,
            panel_message_id,
            _("Please send the response content. It can be text, stickers, photos and so on."),
        )
        if prompt:
            self.bot.register_next_step_handler(
                prompt, self.add_auto_response_time, panel_message_id)

    def add_auto_response_time(self, message: Message, panel_message_id: int):
        """Store auto-reply content and offer button-based time restrictions."""
        if not self.check_valid_chat(message):
            return
        if isinstance(message.text, str) and message.text.startswith("/cancel"):
            self._delete_admin_input(message)
            self._clear_auto_response_draft()
            self.auto_reply_menu_by_id(message.chat.id, panel_message_id, _("Operation cancelled"))
            return
        if self.cache.get("auto_response_key") is None:
            self._delete_admin_input(message)
            self.auto_reply_menu_by_id(
                message.chat.id, panel_message_id,
                _("The operation has timed out. Please initiate the process again."))
            return

        match message.content_type:
            case "photo":
                value, response_type = message.photo[-1].file_id, "photo"
            case "text":
                value, response_type = message.text, "text"
            case "sticker":
                value, response_type = message.sticker.file_id, "sticker"
            case "video":
                value, response_type = message.video.file_id, "video"
            case "document":
                value, response_type = message.document.file_id, "document"
            case _:
                self._delete_admin_input(message)
                self.auto_reply_menu_by_id(message.chat.id, panel_message_id, _("Unsupported message type"))
                return

        self._delete_admin_input(message)
        self.cache.set("auto_response_value", value, 300)
        self.cache.set("auto_response_type", response_type, 300)
        self.cache.set("auto_response_key", self.cache.get("auto_response_key"), 300)
        self.cache.set("auto_response_regex", self.cache.get("auto_response_regex"), 300)
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton(
                "✅" + _("Yes"),
                callback_data=json.dumps({"action": "set_auto_response_time", "value": "yes"})),
            types.InlineKeyboardButton(
                "❌" + _("No"),
                callback_data=json.dumps({"action": "set_auto_response_time", "value": "no"})),
        )
        self._edit_panel_by_id(
            message.chat.id, panel_message_id,
            _("Do you want to set a start and end time for this auto response?"), markup)

    def handle_auto_response_time_callback(self, message: Message, data: dict):
        """Handle time restrictions without replacing the admin panel."""
        value = data.get("value")
        if value == "no":
            self.cache.set("auto_response_start_time", None, 300)
            self.cache.set("auto_response_end_time", None, 300)
            self._finish_auto_response(message.chat.id, message.message_id)
            return
        if value == "yes":
            prompt = self._edit_panel(
                message, _("Please enter the start time in HH:MM format (24-hour clock):"))
            if prompt:
                self.bot.register_next_step_handler(
                    prompt, self.set_auto_response_start_time, message.message_id)

    def set_auto_response_start_time(self, message: Message, panel_message_id: int):
        """Store the auto-reply start time and remove the input."""
        if not self.check_valid_chat(message):
            return
        value = message.text if isinstance(message.text, str) else ""
        self._delete_admin_input(message)
        if value.startswith("/cancel"):
            self._clear_auto_response_draft()
            self.auto_reply_menu_by_id(message.chat.id, panel_message_id, _("Operation cancelled"))
            return
        try:
            start_time = datetime.strptime(value, "%H:%M").time()
        except ValueError:
            prompt = self._edit_panel_by_id(
                message.chat.id, panel_message_id,
                _("Invalid time format.") + "\n" +
                _("Please enter the start time in HH:MM format (24-hour clock):"))
            if prompt:
                self.bot.register_next_step_handler(
                    prompt, self.set_auto_response_start_time, panel_message_id)
            return
        self.cache.set("auto_response_start_time", start_time, 300)
        prompt = self._edit_panel_by_id(
            message.chat.id, panel_message_id,
            _("Please enter the end time in HH:MM format (24-hour clock):"))
        if prompt:
            self.bot.register_next_step_handler(
                prompt, self.set_auto_response_end_time, panel_message_id)

    def set_auto_response_end_time(self, message: Message, panel_message_id: int):
        """Store the end time and finish the draft in the existing panel."""
        if not self.check_valid_chat(message):
            return
        value = message.text if isinstance(message.text, str) else ""
        self._delete_admin_input(message)
        if value.startswith("/cancel"):
            self._clear_auto_response_draft()
            self.auto_reply_menu_by_id(message.chat.id, panel_message_id, _("Operation cancelled"))
            return
        try:
            end_time = datetime.strptime(value, "%H:%M").time()
        except ValueError:
            prompt = self._edit_panel_by_id(
                message.chat.id, panel_message_id,
                _("Invalid time format.") + "\n" +
                _("Please enter the end time in HH:MM format (24-hour clock):"))
            if prompt:
                self.bot.register_next_step_handler(
                    prompt, self.set_auto_response_end_time, panel_message_id)
            return
        self.cache.set("auto_response_end_time", end_time, 300)
        self._finish_auto_response(message.chat.id, panel_message_id)

    def _finish_auto_response(self, chat_id: int, panel_message_id: int):
        """Persist an auto-reply draft and replace the panel with its result."""
        key = self.cache.pop("auto_response_key")
        value = self.cache.pop("auto_response_value")
        is_regex = self.cache.pop("auto_response_regex")
        response_type = self.cache.pop("auto_response_type")
        start_time = self.cache.pop("auto_response_start_time")
        end_time = self.cache.pop("auto_response_end_time")
        if None in [key, value, is_regex, response_type]:
            self.auto_reply_menu_by_id(chat_id, panel_message_id, _("Invalid action"))
            return
        if start_time is not None and end_time is not None:
            start_time, end_time = start_time.strftime("%H:%M"), end_time.strftime("%H:%M")
        self.auto_response_manager.add_auto_response(
            key, value, is_regex, response_type, start_time, end_time)
        self.auto_reply_menu_by_id(chat_id, panel_message_id, _("Auto reply added"))


    def manage_auto_reply(self, message: Message, page: int = 1, page_size: int = 5):
        """Display paginated list of auto replies."""
        result = self.auto_response_manager.get_auto_responses_paginated(page, page_size)

        markup = types.InlineKeyboardMarkup()
        back_button = types.InlineKeyboardButton("⬅️" + _("Back"),
                                                 callback_data=json.dumps({"action": "auto_reply"}))

        text = _("Auto Reply List:") + "\n"
        text += _("Total: {}").format(result["total"]) + "\n"
        text += _("Page: {}").format(page) + "/" + str(result["total_pages"]) + "\n\n"

        id_buttons = []
        for auto_response in result["responses"]:
            text += "-" * 20 + "\n"
            text += f"ID: {auto_response['id']}\n"
            text += _("Trigger: {}").format(auto_response['key']) + "\n"
            text += _("Response: {}").format(
                auto_response['value'] if auto_response['type'] == "text" else auto_response['type']) + "\n"
            text += _("Is regex: {}").format("✅" if auto_response['is_regex'] else "❌") + "\n"
            text += _("Active time: ")
            if auto_response['start_time'] and auto_response['end_time']:
                text += "{}~{}".format(auto_response['start_time'], auto_response['end_time']) + "\n\n"
            else:
                text += _("Disabled") + "\n\n"
            id_buttons.append(types.InlineKeyboardButton(
                text=f"#{auto_response['id']}",
                callback_data=json.dumps({"action": "select_auto_reply", "id": auto_response['id']})))

        if id_buttons:
            markup.row(*id_buttons)

        # Add pagination buttons
        if 1 < page < result["total_pages"]:
            markup.row(
                types.InlineKeyboardButton("⬅️" + _("Previous Page"),
                                           callback_data=json.dumps({"action": "manage_auto_reply",
                                                                     "page": page - 1})),
                types.InlineKeyboardButton("➡️" + _("Next Page"),
                                           callback_data=json.dumps({"action": "manage_auto_reply",
                                                                     "page": page + 1})))
        elif page > 1:
            markup.add(types.InlineKeyboardButton("⬅️" + _("Previous Page"),
                                                  callback_data=json.dumps({"action": "manage_auto_reply",
                                                                            "page": page - 1})))
        elif page < result["total_pages"]:
            markup.add(types.InlineKeyboardButton("➡️" + _("Next Page"),
                                                  callback_data=json.dumps({"action": "manage_auto_reply",
                                                                            "page": page + 1})))

        markup.add(back_button)
        self.bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=markup)

    def select_auto_reply(self, message: Message, response_id: int):
        """Display details of a specific auto reply."""
        auto_response = self.auto_response_manager.get_auto_response(response_id)
        if auto_response is None:
            self.bot.send_message(self.group_id, _("Auto reply not found"))
            return

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("❌" + _("Delete"),
                                              callback_data=json.dumps({"action": "delete_auto_reply",
                                                                        "id": response_id})))
        markup.add(types.InlineKeyboardButton("⬅️" + _("Back"),
                                              callback_data=json.dumps({"action": "manage_auto_reply"})))

        text = _("Trigger: {}").format(auto_response["key"]) + "\n"
        text += _("Response: {}").format(
            auto_response["value"] if auto_response["type"] == "text" else auto_response["type"]) + "\n"
        text += _("Is regex: {}").format("✅" if auto_response["is_regex"] else "❌") + "\n"
        text += _("Active time: ")
        if auto_response['start_time'] and auto_response['end_time']:
            text += "{}~{}".format(auto_response['start_time'], auto_response['end_time']) + "\n\n"
        else:
            text += _("Disabled") + "\n\n"

        self.bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=markup)

    def delete_auto_reply(self, message: Message, response_id: int):
        """Delete an auto reply."""
        self.auto_response_manager.delete_auto_response(response_id)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⬅️" + _("Back"),
                                              callback_data=json.dumps({"action": "manage_auto_reply"})))
        self.bot.edit_message_text(_("Auto reply deleted"), chat_id=message.chat.id,
                                   message_id=message.message_id, reply_markup=markup)

    # Ban User Management
    def manage_ban_user(self, message: Message, page: int = 1, page_size: int = 10):
        """Display list of banned users with pagination."""
        with sqlite3.connect(self.db_path) as db:
            db_cursor = db.cursor()

            # Get total count
            db_cursor.execute(
                """SELECT COUNT(*) FROM blocked_users
                   WHERE blocked_until IS NULL OR blocked_until > CURRENT_TIMESTAMP"""
            )
            total = db_cursor.fetchone()[0]
            total_pages = (total + page_size - 1) // page_size if total > 0 else 1
            page = max(1, min(page, total_pages))

            # Query from blocked_users table with pagination
            offset = (page - 1) * page_size
            db_cursor.execute("""
                              SELECT user_id, username, first_name, last_name, blocked_at, blocked_until
                              FROM blocked_users
                              WHERE blocked_until IS NULL OR blocked_until > CURRENT_TIMESTAMP
                              ORDER BY blocked_at DESC LIMIT ?
                              OFFSET ?
                              """, (page_size, offset))
            banned_users = db_cursor.fetchall()

            markup = types.InlineKeyboardMarkup()
            back_button = types.InlineKeyboardButton("⬅️" + _("Back"),
                                                     callback_data=json.dumps({"action": "menu"}))

            text = _("Banned User List:") + "\n"
            text += _("Total: {}").format(total) + "\n"
            text += _("Page: {}").format(page) + "/" + str(total_pages) + "\n\n"

            if not banned_users:
                text += _("No banned users") + "\n"
            else:
                for user in banned_users:
                    user_id, username, first_name, last_name, blocked_at, blocked_until = user
                    text += "-" * 20 + "\n"
                    text += f"User ID: {user_id}\n"

                    # Display name info if available
                    if first_name or last_name:
                        full_name = f"{first_name or ''} {last_name or ''}".strip()
                        text += f"{_('Name')}: {full_name}\n"
                    if username:
                        text += f"{_('Username')}: @{username}\n"

                    text += f"{_('Blocked at')}: {blocked_at}\n"
                    text = self._append_temporary_block_status(text, blocked_until)

                    markup.add(types.InlineKeyboardButton(
                        text=f"ID: {user_id}",
                        callback_data=json.dumps({"action": "select_ban_user", "id": user_id})))

            # Add pagination buttons
            if 1 < page < total_pages:
                markup.row(
                    types.InlineKeyboardButton("⬅️" + _("Previous Page"),
                                               callback_data=json.dumps({"action": "ban_user", "page": page - 1})),
                    types.InlineKeyboardButton("➡️" + _("Next Page"),
                                               callback_data=json.dumps({"action": "ban_user", "page": page + 1})))
            elif page > 1:
                markup.add(types.InlineKeyboardButton("⬅️" + _("Previous Page"),
                                                      callback_data=json.dumps(
                                                          {"action": "ban_user", "page": page - 1})))
            elif page < total_pages:
                markup.add(types.InlineKeyboardButton("➡️" + _("Next Page"),
                                                      callback_data=json.dumps(
                                                          {"action": "ban_user", "page": page + 1})))

            markup.add(back_button)
            self.bot.send_message(text=text,
                                  chat_id=message.chat.id,
                                  message_thread_id=None,
                                  reply_markup=markup)

    def select_ban_user(self, message: Message, user_id: int):
        """Display options for a banned user."""
        with sqlite3.connect(self.db_path) as db:
            db_cursor = db.cursor()
            # Get user info from blocked_users
            db_cursor.execute(
                "SELECT username, first_name, last_name, blocked_at, blocked_until "
                "FROM blocked_users WHERE user_id = ? "
                "AND (blocked_until IS NULL OR blocked_until > CURRENT_TIMESTAMP) LIMIT 1",
                (user_id,)
            )
            user_info = db_cursor.fetchone()

            if user_info is None:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("⬅️" + _("Back"),
                                                      callback_data=json.dumps({"action": "ban_user"})))
                self.bot.edit_message_text(_("User not found"), message.chat.id, message.message_id,
                                           reply_markup=markup)
                return

            username, first_name, last_name, blocked_at, blocked_until = user_info

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("❌" + _("Unban"),
                                              callback_data=json.dumps({"action": "unban_user",
                                                                        "id": user_id})))
        markup.add(types.InlineKeyboardButton("⬅️" + _("Back"),
                                              callback_data=json.dumps({"action": "ban_user"})))

        # Build user info text
        text = _("Blocked User Details") + "\n\n"
        text += f"User ID: {user_id}\n"
        if first_name or last_name:
            full_name = f"{first_name or ''} {last_name or ''}".strip()
            text += f"{_('Name')}: {full_name}\n"
        if username:
            text += f"{_('Username')}: @{username}\n"
        text += f"{_('Blocked at')}: {blocked_at}\n"
        text = self._append_temporary_block_status(text, blocked_until)

        self.bot.edit_message_text(text, message.chat.id, message.message_id,
                                   reply_markup=markup)

    # Default Message Settings
    def _render_default_message_panel(self, chat_id: int, message_id: int, notice: str | None = None):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "✏️" + _("Edit Message"),
            callback_data=json.dumps({"action": "edit_default_msg"})))
        markup.add(types.InlineKeyboardButton(
            "🔄️" + _("Set to Default"),
            callback_data=json.dumps({"action": "empty_default_msg"})))
        markup.add(self._panel_back_button())
        text = _("Default Message") + "\n" + _(
            "The default message is an auto-reply to the commands /help and /start")
        if notice:
            text = notice + "\n\n" + text
        return self._edit_panel_by_id(chat_id, message_id, text, markup)

    def default_msg_menu(self, message: Message):
        """Display default message settings in the current admin panel."""
        if self.check_valid_chat(message):
            self._render_default_message_panel(message.chat.id, message.message_id)

    def edit_default_msg(self, message: Message):
        """Prompt for a default message without creating a separate prompt."""
        prompt = self._edit_panel(
            message,
            _("Let's set up the default message.\nSend /cancel to cancel this operation.\n\n"
              "Please send me the response."),
        )
        if prompt:
            self.bot.register_next_step_handler(
                prompt, self.edit_default_msg_handle, message.message_id)

    def edit_default_msg_handle(self, message: Message, panel_message_id: int):
        """Store the default message and remove the administrator's input."""
        if not self.check_valid_chat(message):
            return
        text = message.text if isinstance(message.text, str) else None
        self._delete_admin_input(message)
        if text is None:
            self._render_default_message_panel(
                message.chat.id, panel_message_id, _("Invalid input"))
            return
        if text.startswith("/cancel"):
            self._render_default_message_panel(
                message.chat.id, panel_message_id, _("Operation cancelled"))
            return

        self.database.set_setting("default_message", text)
        self._render_default_message_panel(
            message.chat.id, panel_message_id, _("Default message has been updated."))

    def empty_default_msg(self, message: Message):
        """Reset the default message to the built-in reply."""
        self.database.set_setting("default_message", None)
        self._render_default_message_panel(
            message.chat.id, message.message_id, _("Default message has been restored."))

    # Captcha Settings
    def captcha_settings_menu(self, message: Message, notice: str | None = None):
        """Display captcha choices in the current admin panel."""
        if not self.check_valid_chat(message):
            return
        captcha_list = {
            _("Math Captcha"): "math",
            _("Image Captcha"): "image",
        }
        if self.bot_instance and self.bot_instance.webapp_service.is_enabled():
            captcha_list[_("Cloudflare Turnstile")] = "webapp"

        markup = types.InlineKeyboardMarkup()
        current = self.cache.get("setting_captcha")
        for label, value in captcha_list.items():
            icon = "✅" + _("(Selected) ") if current == value else "⚪"
            markup.add(types.InlineKeyboardButton(
                icon + label,
                callback_data=json.dumps({"action": "set_captcha", "value": value})))
        markup.add(types.InlineKeyboardButton(
            _("Configure Turnstile WebApp"),
            callback_data=json.dumps({"action": "turnstile_settings"})))
        markup.add(self._panel_back_button())
        text = _("Captcha Settings")
        if notice:
            text = notice + "\n\n" + text
        self._edit_panel(message, text, markup)

    def set_captcha(self, message: Message, value: str):
        """Set the captcha type and refresh the same settings panel."""
        if value not in {"math", "image", "webapp"}:
            logger.warning("Rejected invalid captcha setting: %s", value)
            return
        if (value == "webapp" and
                (not self.bot_instance or not self.bot_instance.webapp_service.is_enabled())):
            self.captcha_settings_menu(message, _("Turnstile WebApp is not enabled"))
            return
        self.database.set_setting("captcha", value)
        self.cache.set("setting_captcha", value)
        self.captcha_settings_menu(message, _("Captcha settings updated"))

    def _turnstile_markup(self, running: bool):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            _("Disable") if running else _("Enable"),
            callback_data=json.dumps({
                "action": "set_turnstile_enabled",
                "value": "disable" if running else "enable",
            })))
        labels = {
            "public_url": _("Public URL"),
            "site_key": _("Site Key"),
            "secret_key": _("Secret Key"),
            "hostname": _("Expected hostname"),
            "host": _("Listen host"),
            "port": _("Listen port"),
            "auth_max_age": _("Telegram data max age"),
        }
        for field, label in labels.items():
            markup.add(types.InlineKeyboardButton(
                "✏️ " + label,
                callback_data=json.dumps({
                    "action": "edit_turnstile_setting", "field": field,
                })))
        markup.add(self._panel_back_button())
        return markup

    def _render_turnstile_panel(self, chat_id: int, message_id: int, notice: str | None = None):
        if not self.bot_instance:
            return None
        settings = self.bot_instance.get_turnstile_settings()
        running = self.bot_instance.webapp_service.is_enabled()
        secret_status = _("Configured") if settings.get("secret_key") else _("Not configured")
        site_key = settings.get("site_key") or _("Not configured")
        if len(site_key) > 12:
            site_key = site_key[:6] + "..." + site_key[-4:]

        text = _("Turnstile WebApp Settings") + "\n\n"
        text += f"{_('Status')}: {_('Running') if running else _('Disabled')}\n"
        text += f"{_('Public URL')}: {settings.get('public_url') or _('Not configured')}\n"
        text += f"{_('Site Key')}: {site_key}\n"
        text += f"{_('Secret Key')}: {secret_status}\n"
        text += f"{_('Expected hostname')}: {settings.get('hostname') or _('Not enforced')}\n"
        text += f"{_('Listener')}: {settings.get('host')}:{settings.get('port')}\n"
        text += f"{_('Telegram data max age')}: {settings.get('auth_max_age')}s"
        if notice:
            text = notice + "\n\n" + text
        return self._edit_panel_by_id(
            chat_id, message_id, text, self._turnstile_markup(running))

    def turnstile_settings_menu(self, message: Message, notice: str | None = None):
        """Display persistent runtime Turnstile WebApp settings in one panel."""
        if self.check_valid_chat(message) and self.bot_instance:
            self._render_turnstile_panel(message.chat.id, message.message_id, notice)

    def set_turnstile_enabled(self, message: Message, value: str):
        """Enable or disable the persisted WebApp service and refresh the panel."""
        if value not in {"enable", "disable"} or not self.bot_instance:
            return
        ok, error = self.bot_instance.update_turnstile_setting("enabled", value)
        notice = _("Turnstile WebApp settings updated") if ok else (
            _("Turnstile WebApp update failed") + f": {error}")
        self._render_turnstile_panel(message.chat.id, message.message_id, notice)

    def edit_turnstile_setting(self, message: Message, field: str):
        """Prompt for one runtime WebApp setting in the existing panel."""
        prompts = {
            "public_url": _("Send the public HTTPS WebApp URL without query parameters."),
            "site_key": _("Send the Cloudflare Turnstile Site Key."),
            "secret_key": _("Send the Cloudflare Turnstile Secret Key. The message will be deleted."),
            "hostname": _("Send the expected Turnstile hostname, or - to disable hostname enforcement."),
            "host": _("Send the local listen host, for example 0.0.0.0."),
            "port": _("Send the local listen port (1-65535)."),
            "auth_max_age": _("Send the maximum Telegram authorization age in seconds (30-3600)."),
        }
        if field not in prompts or not self.check_valid_chat(message):
            return
        prompt = self._edit_panel(
            message, prompts[field] + "\n\n" + _("Send /cancel to cancel this operation."))
        if prompt:
            self.bot.register_next_step_handler(
                prompt, self.process_turnstile_setting, field, message.message_id)

    def process_turnstile_setting(self, message: Message, field: str, panel_message_id: int):
        """Validate, persist, and hot-reload one WebApp setting without chat clutter."""
        if not self.check_valid_chat(message) or not isinstance(message.text, str):
            return
        value = message.text.strip()
        self._delete_admin_input(message)
        if value.startswith("/cancel"):
            self._render_turnstile_panel(
                message.chat.id, panel_message_id, _("Operation cancelled"))
            return

        error = self._validate_turnstile_setting(field, value)
        if error:
            self._render_turnstile_panel(message.chat.id, panel_message_id, error)
            return
        if field == "hostname" and value == "-":
            value = ""

        ok, reload_error = self.bot_instance.update_turnstile_setting(field, value)
        notice = _("Turnstile WebApp settings updated") if ok else (
            _("Turnstile WebApp update failed") + f": {reload_error}")
        self._render_turnstile_panel(message.chat.id, panel_message_id, notice)

    @staticmethod
    def _validate_turnstile_setting(field: str, value: str):
        if field == "public_url":
            parsed = urlsplit(value)
            if (parsed.scheme != "https" or not parsed.hostname
                    or parsed.query or parsed.fragment):
                return _("Public URL must use HTTPS and must not contain a query or fragment")
        elif field in {"site_key", "secret_key", "host"} and not value:
            return _("This setting cannot be empty")
        elif field == "hostname" and value != "-":
            if not re.fullmatch(r"[A-Za-z0-9.-]+", value):
                return _("Invalid hostname")
        elif field == "port":
            if not value.isdigit() or not 1 <= int(value) <= 65535:
                return _("Port must be between 1 and 65535")
        elif field == "auth_max_age":
            if not value.isdigit() or not 30 <= int(value) <= 3600:
                return _("Telegram data max age must be between 30 and 3600 seconds")
        return None

    # Time Zone Settings
    def _time_zone_markup(self):
        markup = types.InlineKeyboardMarkup()
        zones = (("UTC", "UTC"), ("Asia/Shanghai", "Shanghai"),
                 ("Asia/Tokyo", "Tokyo"), ("Europe/London", "London"),
                 ("America/New_York", "New York"))
        for value, label in zones:
            markup.add(types.InlineKeyboardButton(
                label, callback_data=json.dumps({"action": "set_time_zone", "value": value})))
        markup.add(types.InlineKeyboardButton(
            _("Custom time zone"), callback_data=json.dumps({"action": "edit_time_zone"})))
        markup.add(self._panel_back_button())
        return markup

    def _render_time_zone_panel(self, chat_id: int, message_id: int, notice: str | None = None):
        current_time_zone = self.database.get_setting("time_zone") or "UTC"
        text = _("Current time zone: {}").format(current_time_zone)
        if notice:
            text = notice + "\n\n" + text
        return self._edit_panel_by_id(
            chat_id, message_id, text, self._time_zone_markup())

    def time_zone_settings_menu(self, message: Message):
        """Display quick time-zone choices in the current panel."""
        if self.check_valid_chat(message):
            self._render_time_zone_panel(message.chat.id, message.message_id)

    def edit_time_zone(self, message: Message):
        """Prompt for a custom time zone in the current panel."""
        prompt = self._edit_panel(
            message,
            _("Current time zone: {}.\nPlease enter the new time zone (e.g., Europe/London):\n\n"
              "Send /cancel to cancel this operation.").format(
                  self.database.get_setting("time_zone") or "UTC"),
        )
        if prompt:
            self.bot.register_next_step_handler(
                prompt, self.validate_time_zone, message.message_id)

    def validate_time_zone(self, message: Message, panel_message_id: int):
        """Validate a custom time zone and remove the intermediate input."""
        if not self.check_valid_chat(message):
            return
        time_zone = message.text if isinstance(message.text, str) else ""
        self._delete_admin_input(message)
        if time_zone.startswith("/cancel"):
            self._render_time_zone_panel(
                message.chat.id, panel_message_id, _("Operation cancelled"))
            return
        if time_zone in pytz.all_timezones:
            self.set_time_zone(message, time_zone, panel_message_id)
            return
        self._render_time_zone_panel(
            message.chat.id, panel_message_id, _("Invalid time zone. Please try again:"))

    def set_time_zone(self, message: Message, value: str, panel_message_id: int | None = None):
        """Set the time zone and refresh the existing settings panel."""
        target_message_id = panel_message_id if panel_message_id is not None else message.message_id
        if value not in pytz.all_timezones:
            self._render_time_zone_panel(
                message.chat.id, target_message_id, _("Invalid time zone. Please try again:"))
            return
        self.database.set_setting("time_zone", value)
        self.cache.set("setting_time_zone", value)
        self.time_zone = pytz.timezone(value)
        self.auto_response_manager.update_time_zone(self.time_zone)
        if self.bot_instance:
            self.bot_instance.update_self_time_zone()
        self._render_time_zone_panel(
            message.chat.id,
            panel_message_id if panel_message_id is not None else message.message_id,
            _("Time zone updated to {}").format(value),
        )

    # Broadcast Message
    def _broadcast_result_markup(self):
        markup = types.InlineKeyboardMarkup()
        markup.add(self._panel_back_button())
        return markup

    def broadcast_message(self, message: Message):
        """Start a broadcast draft in the current admin panel."""
        if not self.check_valid_chat(message):
            return
        self.cache.set("broadcast_panel_message_id", message.message_id, 300)
        prompt = self._edit_panel(
            message,
            _("Please send the content you want to broadcast.\nSend /cancel to cancel this operation."),
        )
        if prompt:
            self.bot.register_next_step_handler(
                prompt, self.handle_broadcast_message, message.message_id)

    def show_host_ip(self, message: Message):
        """Show host IP information in the current admin panel."""
        if not self.check_valid_chat(message):
            return
        try:
            headers = {"User-Agent": "curl/8.4.0", "Accept": "*/*"}
            with httpx.Client(http2=True, headers=headers, verify=True) as client:
                response = client.get("https://ipapi.co/json", timeout=5)
            response.raise_for_status()
            data = response.json()
            ip = data.get("ip", _("Unknown"))
            country = data.get("country_name", _("Unknown"))
            city = data.get("city", _("Unknown"))
        except (httpx.HTTPStatusError, httpx.RequestError) as error:
            logger.error("Failed to retrieve IP information: %s", error)
            self._edit_panel(
                message, _("Failed to retrieve IP information"), self._broadcast_result_markup())
            return
        text = _("Host IP Information") + "\n\n"
        text += _("IP Address: {}").format(ip) + "\n"
        text += _("Country: {}").format(country) + "\n"
        text += _("City: {}").format(city)
        self._edit_panel(message, text, self._broadcast_result_markup())

    def handle_broadcast_message(self, message: Message, panel_message_id: int):
        """Create one temporary preview and remove the administrator input."""
        if not self.check_valid_chat(message):
            return
        if isinstance(message.text, str) and message.text.startswith("/cancel"):
            self._delete_admin_input(message)
            self.cancel_broadcast_panel(message.chat.id, panel_message_id, _("Operation cancelled"))
            return

        content_type = message.content_type
        if content_type == "text":
            content = message.text
        elif content_type == "photo":
            content = message.photo[-1].file_id
        elif content_type == "document":
            content = message.document.file_id
        elif content_type == "video":
            content = message.video.file_id
        elif content_type == "sticker":
            content = message.sticker.file_id
        else:
            self._delete_admin_input(message)
            self._edit_panel_by_id(
                message.chat.id, panel_message_id, _("Unsupported message type"),
                self._broadcast_result_markup())
            return

        self._delete_admin_input(message)
        self.cache.set("broadcast_content", content, 300)
        self.cache.set("broadcast_content_type", content_type, 300)
        self.cache.set("broadcast_panel_message_id", panel_message_id, 300)
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton(
                "✅" + _("Confirm"), callback_data=json.dumps({"action": "confirm_broadcast"})),
            types.InlineKeyboardButton(
                "❌" + _("Cancel"), callback_data=json.dumps({"action": "cancel_broadcast"})),
        )
        if content_type == "text":
            preview = self.bot.send_message(self.group_id, content, reply_markup=markup)
        elif content_type == "photo":
            preview = self.bot.send_photo(self.group_id, content, reply_markup=markup)
        elif content_type == "document":
            preview = self.bot.send_document(self.group_id, content, reply_markup=markup)
        elif content_type == "video":
            preview = self.bot.send_video(self.group_id, content, reply_markup=markup)
        else:
            preview = self.bot.send_sticker(self.group_id, content, reply_markup=markup)
        self.cache.set("broadcast_preview_message_id", preview.message_id, 300)

    def _clear_broadcast_draft(self):
        for key in (
                "broadcast_content", "broadcast_content_type", "broadcast_panel_message_id",
                "broadcast_preview_message_id"):
            self.cache.delete(key)

    def cancel_broadcast_panel(self, chat_id: int, panel_message_id: int, notice: str):
        self._clear_broadcast_draft()
        self._edit_panel_by_id(chat_id, panel_message_id, notice, self._broadcast_result_markup())

    def confirm_broadcast_message(self, message: Message):
        """Send a confirmed broadcast and clean up the temporary preview."""
        content = self.cache.get("broadcast_content")
        content_type = self.cache.get("broadcast_content_type")
        panel_message_id = self.cache.get("broadcast_panel_message_id")
        if content is None or content_type is None or panel_message_id is None:
            self._delete_preview(message)
            return

        with sqlite3.connect(self.db_path) as db:
            users = db.execute("SELECT user_id FROM topics").fetchall()
        for (user_id,) in users:
            try:
                if content_type == "text":
                    self.bot.send_message(user_id, content)
                elif content_type == "photo":
                    self.bot.send_photo(user_id, content)
                elif content_type == "document":
                    self.bot.send_document(user_id, content)
                elif content_type == "video":
                    self.bot.send_video(user_id, content)
                else:
                    self.bot.send_sticker(user_id, content)
            except ApiTelegramException:
                logger.exception("Failed to send broadcast to user %s", user_id)

        self._delete_preview(message)
        self._clear_broadcast_draft()
        self._edit_panel_by_id(
            message.chat.id, panel_message_id,
            _("Broadcast message sent successfully."), self._broadcast_result_markup())

    def _delete_preview(self, message: Message):
        try:
            self.bot.delete_message(message.chat.id, message.message_id)
        except ApiTelegramException:
            logger.debug("Could not delete broadcast preview %s", message.message_id)

    def cancel_broadcast(self, message: Message):
        """Cancel a broadcast preview and restore the original admin panel."""
        panel_message_id = self.cache.get("broadcast_panel_message_id")
        self._delete_preview(message)
        self._clear_broadcast_draft()
        if panel_message_id is not None:
            self._edit_panel_by_id(
                message.chat.id, panel_message_id,
                _("Broadcast cancelled"), self._broadcast_result_markup())

    # Spam Keywords Management
    def _render_spam_keywords_panel(self, chat_id: int, message_id: int, notice: str | None = None):
        if not self.spam_keyword_manager:
            return self._edit_panel_by_id(
                chat_id, message_id, _("Spam keywords management is not available"))

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "➕" + _("Add Keyword"),
            callback_data=json.dumps({"action": "add_spam_keyword"})))
        markup.add(types.InlineKeyboardButton(
            "📋" + _("View Keywords"),
            callback_data=json.dumps({"action": "view_spam_keywords"})))
        markup.add(types.InlineKeyboardButton(
            "🔄" + _("Reset Spam Topic"),
            callback_data=json.dumps({"action": "reset_spam_topic"})))
        markup.add(self._panel_back_button())

        keyword_count = self.spam_keyword_manager.get_keyword_count()
        spam_topic_id = self.cache.get("spam_topic_id")
        text = _("Spam Keywords Management") + "\n\n"
        text += _("Total keywords: {}").format(keyword_count) + "\n"
        text += _("Spam Topic ID: {}").format(
            spam_topic_id if spam_topic_id else _("Not set")) + "\n\n"
        text += _("Messages containing these keywords will be forwarded to the spam topic silently.")
        if notice:
            text = notice + "\n\n" + text
        return self._edit_panel_by_id(chat_id, message_id, text, markup)

    def spam_keywords_menu(self, message: Message):
        """Display spam keyword controls in the existing admin panel."""
        if self.check_valid_chat(message):
            self._render_spam_keywords_panel(message.chat.id, message.message_id)

    def add_spam_keyword(self, message: Message):
        """Prompt for a keyword in the current panel."""
        if not self.check_valid_chat(message):
            return
        prompt = self._edit_panel(
            message,
            _("Please send the keyword you want to add to the spam filter.\n"
              "Send /cancel to cancel this operation."),
        )
        if prompt:
            self.bot.register_next_step_handler(
                prompt, self.process_add_spam_keyword, message.message_id)

    def process_add_spam_keyword(self, message: Message, panel_message_id: int):
        """Save a keyword and remove the administrator's intermediate message."""
        if not self.check_valid_chat(message):
            logger.warning(
                "Keyword add attempt from wrong context: chat_id=%s, thread_id=%s",
                message.chat.id, message.message_thread_id)
            return

        text = message.text if message.content_type == "text" else None
        self._delete_admin_input(message)
        if text is None:
            self._render_spam_keywords_panel(
                message.chat.id, panel_message_id, _("Invalid input"))
            return
        if text.startswith("/cancel"):
            self._render_spam_keywords_panel(
                message.chat.id, panel_message_id, _("Operation cancelled"))
            return

        keyword = text.strip()
        if not keyword:
            self._render_spam_keywords_panel(
                message.chat.id, panel_message_id, _("Keyword cannot be empty"))
            return

        try:
            if self.spam_keyword_manager.add_keyword(keyword):
                notice = _("Keyword added: {}").format(keyword)
            else:
                notice = _("Keyword already exists or is invalid")
            self._render_spam_keywords_panel(message.chat.id, panel_message_id, notice)
        except Exception as error:
            logger.exception("Could not add spam keyword")
            self._render_spam_keywords_panel(
                message.chat.id, panel_message_id,
                _("Failed to add keyword: {}").format(str(error)))


    def view_spam_keywords(self, message: Message, page: int = 1, page_size: int = 10):
        """Display paginated list of spam keywords."""
        if not self.spam_keyword_manager:
            return

        keywords = self.spam_keyword_manager.get_all_keywords()
        total = len(keywords)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1

        # Ensure page is valid
        page = max(1, min(page, total_pages))

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_keywords = keywords[start_idx:end_idx]

        # Store keywords in cache for callback access
        self.cache.set("spam_keywords_page", keywords, 300)

        markup = types.InlineKeyboardMarkup()
        back_button = types.InlineKeyboardButton("⬅️" + _("Back"),
                                                 callback_data=json.dumps({"action": "spam_keywords"}))

        text = _("Spam Keywords List:") + "\n"
        text += _("Total: {}").format(total) + "\n"
        text += _("Page: {}").format(page) + "/" + str(total_pages) + "\n\n"

        if not page_keywords:
            text += _("No keywords found") + "\n"
        else:
            keyword_buttons = []
            for idx, keyword in enumerate(page_keywords, start=start_idx):
                text += f"{idx + 1}. {keyword}\n"
                # Use index instead of keyword to avoid callback_data size limit
                keyword_buttons.append(types.InlineKeyboardButton(
                    text=f"#{idx + 1}",
                    callback_data=json.dumps({"action": "select_spam_keyword", "idx": idx})))

            # Add keyword selection buttons (max 5 per row)
            for i in range(0, len(keyword_buttons), 5):
                markup.row(*keyword_buttons[i:i + 5])

        # Add pagination buttons
        if 1 < page < total_pages:
            markup.row(
                types.InlineKeyboardButton("⬅️" + _("Previous Page"),
                                           callback_data=json.dumps({"action": "view_spam_keywords",
                                                                     "page": page - 1})),
                types.InlineKeyboardButton("➡️" + _("Next Page"),
                                           callback_data=json.dumps({"action": "view_spam_keywords",
                                                                     "page": page + 1})))
        elif page > 1:
            markup.add(types.InlineKeyboardButton("⬅️" + _("Previous Page"),
                                                  callback_data=json.dumps({"action": "view_spam_keywords",
                                                                            "page": page - 1})))
        elif page < total_pages:
            markup.add(types.InlineKeyboardButton("➡️" + _("Next Page"),
                                                  callback_data=json.dumps({"action": "view_spam_keywords",
                                                                            "page": page + 1})))

        markup.add(back_button)
        self.bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=markup)

    def select_spam_keyword(self, message: Message, idx: int):
        """Display options for a specific spam keyword."""
        # Get keyword from cache
        keywords = self.cache.get("spam_keywords_page")
        if keywords is None or idx >= len(keywords):
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️" + _("Back"),
                                                  callback_data=json.dumps({"action": "view_spam_keywords"})))
            self.bot.edit_message_text(_("Keyword not found or expired"),
                                       message.chat.id, message.message_id,
                                       reply_markup=markup)
            return

        keyword = keywords[idx]

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("❌" + _("Delete"),
                                              callback_data=json.dumps({"action": "delete_spam_keyword",
                                                                        "idx": idx})))
        markup.add(types.InlineKeyboardButton("⬅️" + _("Back"),
                                              callback_data=json.dumps({"action": "view_spam_keywords"})))

        text = _("Keyword: {}").format(keyword) + "\n"
        text += _("Select an action:")

        self.bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=markup)

    def delete_spam_keyword(self, message: Message, idx: int):
        """Delete a spam keyword."""
        # Get keyword from cache
        keywords = self.cache.get("spam_keywords_page")
        if keywords is None or idx >= len(keywords):
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️" + _("Back"),
                                                  callback_data=json.dumps({"action": "view_spam_keywords"})))
            self.bot.edit_message_text(_("Keyword not found or expired"),
                                       message.chat.id, message.message_id,
                                       reply_markup=markup)
            return

        keyword = keywords[idx]

        if self.spam_keyword_manager.remove_keyword(keyword):
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️" + _("Back"),
                                                  callback_data=json.dumps({"action": "view_spam_keywords"})))
            self.bot.edit_message_text(_("Keyword deleted: {}").format(keyword),
                                       chat_id=message.chat.id,
                                       message_id=message.message_id,
                                       reply_markup=markup)
        else:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️" + _("Back"),
                                                  callback_data=json.dumps({"action": "view_spam_keywords"})))
            self.bot.edit_message_text(_("Failed to delete keyword"),
                                       chat_id=message.chat.id,
                                       message_id=message.message_id,
                                       reply_markup=markup)

    # Blocked User Reply Settings
    def _render_blocked_reply_panel(self, chat_id: int, message_id: int, notice: str | None = None):
        current_enabled = self.database.get_setting("blocked_user_reply_enabled")
        current_message = self.database.get_setting("blocked_user_reply_message")
        markup = types.InlineKeyboardMarkup()
        if current_enabled == "enable":
            markup.add(types.InlineKeyboardButton(
                "🔕 " + _("Disable Auto Reply"),
                callback_data=json.dumps({"action": "set_blocked_reply_enabled", "value": "disable"})))
        else:
            markup.add(types.InlineKeyboardButton(
                "🔔 " + _("Enable Auto Reply"),
                callback_data=json.dumps({"action": "set_blocked_reply_enabled", "value": "enable"})))
        markup.add(types.InlineKeyboardButton(
            "✏️ " + _("Edit Reply Message"),
            callback_data=json.dumps({"action": "edit_blocked_reply_message"})))
        markup.add(types.InlineKeyboardButton(
            "🗑️ " + _("Clear Reply Message"),
            callback_data=json.dumps({"action": "clear_blocked_reply_message"})))
        markup.add(self._panel_back_button())

        text = _("Blocked User Auto Reply Settings") + "\n\n"
        text += _("Status: {}").format(
            _("Enabled") if current_enabled == "enable" else _("Disabled")) + "\n"
        text += _("Current message: {}").format(
            current_message if current_message else _("Not set (no reply will be sent)")) + "\n\n"
        text += _("When enabled, blocked users will receive this message when they try to send messages.")
        if notice:
            text = notice + "\n\n" + text
        return self._edit_panel_by_id(chat_id, message_id, text, markup)

    def blocked_reply_settings_menu(self, message: Message):
        """Display blocked-user auto-reply controls in the existing panel."""
        if self.check_valid_chat(message):
            self._render_blocked_reply_panel(message.chat.id, message.message_id)

    def set_blocked_reply_enabled(self, message: Message, value: str):
        """Toggle blocked-user auto reply without creating a result message."""
        if value not in {"enable", "disable"}:
            return
        self.database.set_setting("blocked_user_reply_enabled", value)
        self.cache.set("setting_blocked_user_reply_enabled", value)
        status = _("Enabled") if value == "enable" else _("Disabled")
        self._render_blocked_reply_panel(
            message.chat.id, message.message_id,
            _("Blocked user auto-reply has been {}.").format(status))

    def edit_blocked_reply_message(self, message: Message):
        """Prompt for the blocked-user reply in the current panel."""
        prompt = self._edit_panel(
            message,
            _("Please send the message to reply to blocked users.\n"
              "Send /cancel to cancel this operation.\n\n"
              "Note: You can send an empty message to disable auto-reply."),
        )
        if prompt:
            self.bot.register_next_step_handler(
                prompt, self.process_edit_blocked_reply_message, message.message_id)

    def process_edit_blocked_reply_message(self, message: Message, panel_message_id: int):
        """Store a blocked-user reply and delete the intermediate input."""
        if not self.check_valid_chat(message):
            logger.warning(
                "Blocked reply edit from wrong context: chat_id=%s, thread_id=%s",
                message.chat.id, message.message_thread_id)
            return

        text = message.text if message.content_type == "text" else None
        self._delete_admin_input(message)
        if text is None:
            self._render_blocked_reply_panel(
                message.chat.id, panel_message_id, _("Invalid input"))
            return
        if text.startswith("/cancel"):
            self._render_blocked_reply_panel(
                message.chat.id, panel_message_id, _("Operation cancelled"))
            return

        reply_message = text.strip() or None
        self.database.set_setting("blocked_user_reply_message", reply_message)
        self.cache.set("setting_blocked_user_reply_message", reply_message)
        notice = (_("Blocked user reply message updated: {}").format(reply_message)
                  if reply_message else
                  _("Blocked user reply message cleared. No auto-reply will be sent."))
        self._render_blocked_reply_panel(message.chat.id, panel_message_id, notice)

    def clear_blocked_reply_message(self, message: Message):
        """Clear the blocked-user reply and retain its settings panel."""
        self.database.set_setting("blocked_user_reply_message", None)
        self.cache.set("setting_blocked_user_reply_message", None)
        self._render_blocked_reply_panel(
            message.chat.id, message.message_id, _("Blocked user reply message cleared."))

    # Spam Topic Management
    def reset_spam_topic(self, message: Message):
        """Reset spam topic."""
        if not self.bot_instance:
            self.bot.send_message(self.group_id, _("Bot instance not available"))
            return

        # Confirm action
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "✅" + _("Confirm Reset"),
            callback_data=json.dumps({"action": "confirm_reset_spam_topic"})
        ))
        markup.add(types.InlineKeyboardButton(
            "❌" + _("Cancel"),
            callback_data=json.dumps({"action": "spam_keywords"})
        ))

        self.bot.edit_message_text(
            _("Are you sure you want to reset the spam topic?\n"
              "This will create a new spam topic. The old topic will not be deleted."),
            message.chat.id, message.message_id,
            reply_markup=markup)

    def confirm_reset_spam_topic(self, message: Message):
        """Confirm and execute spam topic reset."""
        if not self.bot_instance:
            self.bot.edit_message_text(_("Bot instance not available"),
                                       message.chat.id, message.message_id)
            return

        self.bot.edit_message_text(_("Resetting spam topic..."),
                                   message.chat.id, message.message_id)

        try:
            if self.bot_instance.reset_spam_topic():
                spam_topic_id = self.cache.get("spam_topic_id")
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("⬅️" + _("Back"),
                                                      callback_data=json.dumps({"action": "spam_keywords"})))
                self.bot.edit_message_text(
                    _("Spam topic reset successfully.\nNew Topic ID: {}").format(spam_topic_id),
                    message.chat.id, message.message_id,
                    reply_markup=markup)
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("⬅️" + _("Back"),
                                                      callback_data=json.dumps({"action": "spam_keywords"})))
                self.bot.edit_message_text(_("Failed to reset spam topic"),
                                           message.chat.id, message.message_id,
                                           reply_markup=markup)
        except Exception as e:
            logger.error(f"Error resetting spam topic: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️" + _("Back"),
                                                  callback_data=json.dumps({"action": "spam_keywords"})))
            self.bot.edit_message_text(_("Failed to reset spam topic: {}").format(str(e)),
                                       message.chat.id, message.message_id,
                                       reply_markup=markup)

    # ========== Appeal Management ==========

    def appeal_management_menu(self, message: Message):
        """Display appeal management menu."""
        markup = types.InlineKeyboardMarkup()

        # Get pending appeals count
        with sqlite3.connect(self.db_path) as db:
            cursor = db.cursor()
            pending_count = cursor.execute(
                "SELECT COUNT(*) FROM appeal_requests WHERE status = 'pending'"
            ).fetchone()[0]

        # Get current appeal mode
        appeal_mode = self.cache.get("setting_appeal_mode") or "manual"
        mode_text = _("🔧 Manual Review") if appeal_mode == "manual" else _("🤖 Auto-Approve")

        markup.add(types.InlineKeyboardButton(
            _("📋 View Pending Appeals ({})").format(pending_count),
            callback_data=json.dumps({"action": "view_pending_appeals"})
        ))
        markup.add(types.InlineKeyboardButton(
            _("📊 View All Appeals"),
            callback_data=json.dumps({"action": "view_all_appeals"})
        ))
        markup.add(types.InlineKeyboardButton(
            _("Current Mode: {}").format(mode_text),
            callback_data=json.dumps({"action": "toggle_appeal_mode"})
        ))
        markup.add(types.InlineKeyboardButton(
            "⬅️" + _("Back"),
            callback_data=json.dumps({"action": "menu"})
        ))

        help_text = _(
            "📝 Appeal Management\n\n"
            "🔧 Manual Review: Admins must approve/reject each appeal\n"
            "🤖 Auto-Approve: Appeals are automatically approved, but users remain under watch\n\n"
            "Click buttons below to manage:"
        )

        self.bot.edit_message_text(
            help_text,
            message.chat.id,
            message.message_id,
            reply_markup=markup
        )

    def view_pending_appeals(self, message: Message):
        """Display list of pending appeals."""
        with sqlite3.connect(self.db_path) as db:
            cursor = db.cursor()
            appeals = cursor.execute(
                """SELECT ar.user_id, ar.appeal_time, bu.username, bu.first_name, bu.last_name
                   FROM appeal_requests ar
                   JOIN blocked_users bu ON ar.user_id = bu.user_id
                   WHERE ar.status = 'pending'
                   ORDER BY ar.appeal_time DESC
                   LIMIT 10"""
            ).fetchall()

        if not appeals:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(
                "⬅️" + _("Back"),
                callback_data=json.dumps({"action": "appeal_management"})
            ))
            self.bot.edit_message_text(
                _("✅ No pending appeals"),
                message.chat.id,
                message.message_id,
                reply_markup=markup
            )
            return

        text = _("📋 Pending Appeals:\n\n")
        for user_id, appeal_time, username, first_name, last_name in appeals:
            full_name = f"{first_name or ''} {last_name or ''}".strip() or "N/A"
            username_str = f"@{username}" if username else "N/A"
            text += f"👤 {full_name} (ID: {user_id})\n"
            text += f"   Username: {username_str}\n"
            text += f"   Time: {appeal_time}\n\n"

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "⬅️" + _("Back"),
            callback_data=json.dumps({"action": "appeal_management"})
        ))

        self.bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=markup)

    def view_all_appeals(self, message: Message):
        """Display statistics of all appeals."""
        with sqlite3.connect(self.db_path) as db:
            cursor = db.cursor()

            # Get counts by status
            pending = cursor.execute(
                "SELECT COUNT(*) FROM appeal_requests WHERE status = 'pending'"
            ).fetchone()[0]
            approved = cursor.execute(
                "SELECT COUNT(*) FROM appeal_requests WHERE status = 'approved'"
            ).fetchone()[0]
            rejected = cursor.execute(
                "SELECT COUNT(*) FROM appeal_requests WHERE status = 'rejected'"
            ).fetchone()[0]

            # Get recent appeals
            recent = cursor.execute(
                """SELECT user_id, status, appeal_time
                   FROM appeal_requests
                   ORDER BY appeal_time DESC
                   LIMIT 5"""
            ).fetchall()

        text = _(
            "📊 Appeal Statistics\n\n"
            "⏳ Pending: {}\n"
            "✅ Approved: {}\n"
            "❌ Rejected: {}\n"
            "📈 Total: {}\n\n"
            "Recent Appeals:\n"
        ).format(pending, approved, rejected, pending + approved + rejected)

        for user_id, status, appeal_time in recent:
            status_icon = {"pending": "⏳", "approved": "✅", "rejected": "❌"}.get(status, "❓")
            text += f"{status_icon} User {user_id} - {appeal_time}\n"

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "⬅️" + _("Back"),
            callback_data=json.dumps({"action": "appeal_management"})
        ))

        self.bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=markup)

    def toggle_appeal_mode(self, message: Message):
        """Toggle between manual and auto appeal mode."""
        current_mode = self.cache.get("setting_appeal_mode") or "manual"
        new_mode = "auto" if current_mode == "manual" else "manual"

        # Update database and cache
        self.database.set_setting("appeal_mode", new_mode)
        self.cache.set("setting_appeal_mode", new_mode)

        mode_text = _("🤖 Auto-Approve") if new_mode == "auto" else _("🔧 Manual Review")

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "⬅️" + _("Back"),
            callback_data=json.dumps({"action": "appeal_management"})
        ))

        self.bot.edit_message_text(
            _("✅ Appeal mode changed to: {}").format(mode_text),
            message.chat.id,
            message.message_id,
            reply_markup=markup
        )

