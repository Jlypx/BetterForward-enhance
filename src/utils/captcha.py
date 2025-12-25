"""Captcha functionality for BetterForward."""

import json
import os
import random
from datetime import datetime
from io import BytesIO

from diskcache import Cache
from PIL import Image, ImageDraw, ImageFont
from telebot import types

from src.config import _ as gettext_


class CaptchaManager:
    """Manages captcha generation and verification."""

    def __init__(self, bot, cache: Cache):
        self.bot = bot
        self.cache = cache

    def generate_captcha(self, user_id: int, captcha_type: str = "math"):
        """Generate a captcha for the user."""
        match captcha_type:
            case "math":
                num1 = random.randint(1, 50)
                num2 = random.randint(1, 50)
                answer = num1 + num2
                self.cache.set(f"captcha_{user_id}", answer, 300)
                return f"{num1} + {num2} = ?"
            case "button":
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton(
                    "Click to verify",
                    callback_data=json.dumps({"action": "verify_button", "user_id": user_id})
                ))
                self.bot.send_message(user_id, gettext_("Please click the button to verify."),
                                      reply_markup=markup)
                return None
            case "image":
                return self._generate_image_captcha(user_id)
            case _:
                raise ValueError(gettext_("Invalid captcha setting"))

    def verify_captcha(self, user_id: int, answer: str) -> bool:
        """Verify a captcha answer."""
        captcha = self.cache.get(f"captcha_{user_id}")
        if captcha is None:
            return False
        return str(answer) == str(captcha)

    def is_user_verified(self, user_id: int, db) -> bool:
        """Check if a user is verified."""
        verified = self.cache.get(f"verified_{user_id}")
        if verified is None:
            cursor = db.cursor()
            result = cursor.execute("SELECT 1 FROM verified_users WHERE user_id = ? LIMIT 1",
                                    (user_id,))
            verified = result.fetchone() is not None
            self.cache.set(f"verified_{user_id}", verified, 1800)
        return verified

    def set_user_verified(self, user_id: int, db):
        """Mark a user as verified."""
        cursor = db.cursor()
        cursor.execute("INSERT OR REPLACE INTO verified_users (user_id) VALUES (?)", (user_id,))
        db.commit()
        self.cache.set(f"verified_{user_id}", True, 1800)

    def remove_user_verification(self, user_id: int, db):
        """Remove user verification status."""
        cursor = db.cursor()
        cursor.execute("DELETE FROM verified_users WHERE user_id = ?", (user_id,))
        db.commit()
        self.cache.delete(f"verified_{user_id}")

    # ========== Verification Attempts Tracking ==========

    def record_attempt(self, user_id: int, db) -> int:
        """Record a failed verification attempt and return the current count."""
        cursor = db.cursor()

        # Get current attempt count
        result = cursor.execute(
            "SELECT attempt_count FROM verification_attempts WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if result:
            new_count = result[0] + 1
            cursor.execute(
                """UPDATE verification_attempts
                   SET attempt_count = ?, last_attempt_time = CURRENT_TIMESTAMP
                   WHERE user_id = ?""",
                (new_count, user_id)
            )
        else:
            new_count = 1
            cursor.execute(
                """INSERT INTO verification_attempts
                   (user_id, attempt_count, last_attempt_time)
                   VALUES (?, ?, CURRENT_TIMESTAMP)""",
                (user_id, new_count)
            )

        db.commit()
        return new_count

    def get_attempt_count(self, user_id: int, db) -> int:
        """Get the number of failed verification attempts for a user."""
        cursor = db.cursor()
        result = cursor.execute(
            "SELECT attempt_count FROM verification_attempts WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        return result[0] if result else 0

    def reset_attempts(self, user_id: int, db):
        """Reset verification attempt count for a user (called on successful verification)."""
        cursor = db.cursor()
        cursor.execute("DELETE FROM verification_attempts WHERE user_id = ?", (user_id,))
        db.commit()

    def is_blocked_by_attempts(self, user_id: int, db) -> bool:
        """Check if a user is blocked due to too many failed attempts."""
        cursor = db.cursor()
        result = cursor.execute(
            "SELECT blocked_by_attempts FROM verification_attempts WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        return bool(result and result[0]) if result else False

    def block_user_by_attempts(self, user_id: int, username: str, first_name: str,
                               last_name: str, db) -> bool:
        """Block a user due to too many failed verification attempts."""
        cursor = db.cursor()

        # Mark in verification_attempts table
        cursor.execute(
            """UPDATE verification_attempts
               SET blocked_by_attempts = 1
               WHERE user_id = ?""",
            (user_id,)
        )

        # Add to blocked_users table
        cursor.execute(
            """INSERT OR REPLACE INTO blocked_users
               (user_id, username, first_name, last_name, block_reason, blocked_at)
               VALUES (?, ?, ?, ?, 'auto_attempts', CURRENT_TIMESTAMP)""",
            (user_id, username, first_name, last_name)
        )

        # Remove verification status
        cursor.execute("DELETE FROM verified_users WHERE user_id = ?", (user_id,))
        self.cache.delete(f"verified_{user_id}")

        db.commit()
        return True

    # ========== Image Captcha ==========

    def _generate_image_captcha(self, user_id: int) -> str:
        """Generate an image captcha with 4 digits."""
        # Generate random 4-digit number
        captcha_text = ''.join([str(random.randint(0, 9)) for _ in range(4)])
        self.cache.set(f"captcha_{user_id}", captcha_text, 300)

        # Create image
        width, height = 200, 80
        image = Image.new('RGB', (width, height), color='white')
        draw = ImageDraw.Draw(image)

        # Try to load a font, fallback to default if not available
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
        except Exception:
            # Fallback to default font
            font = ImageFont.load_default()

        # Draw text with slight randomization
        x = 20
        for char in captcha_text:
            # Random y offset for each character
            y = random.randint(15, 25)
            # Random color (dark)
            color = (random.randint(0, 100), random.randint(0, 100), random.randint(0, 100))
            draw.text((x, y), char, font=font, fill=color)
            x += 40

        # Add noise lines
        for _ in range(5):
            x1, y1 = random.randint(0, width), random.randint(0, height)
            x2, y2 = random.randint(0, width), random.randint(0, height)
            draw.line([(x1, y1), (x2, y2)],
                     fill=(random.randint(100, 200), random.randint(100, 200), random.randint(100, 200)),
                     width=2)

        # Add noise points
        for _ in range(100):
            x, y = random.randint(0, width), random.randint(0, height)
            draw.point((x, y),
                      fill=(random.randint(100, 200), random.randint(100, 200), random.randint(100, 200)))

        # Save to BytesIO
        bio = BytesIO()
        image.save(bio, 'PNG')
        bio.seek(0)

        # Send image to user
        self.bot.send_photo(user_id, bio, caption=gettext_("Please enter the 4 digits shown in the image:"))

        return None  # Return None because the captcha was already sent

