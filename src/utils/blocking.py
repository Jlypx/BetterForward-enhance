"""Shared helpers for temporary-block status and presentation."""

import math
from datetime import datetime, timezone

from src.config import _


BLOCKED_REPLY_COOLDOWN_SECONDS = 60


def remaining_block_seconds(blocked_until) -> int | None:
    """Return the number of seconds until an SQLite UTC timestamp expires."""
    if not blocked_until:
        return None
    try:
        expires_at = datetime.fromisoformat(str(blocked_until).replace("Z", "+00:00"))
    except ValueError:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return max(0, math.ceil((expires_at - datetime.now(timezone.utc)).total_seconds()))


def format_block_duration(seconds: int | None) -> str:
    """Format a remaining temporary-block duration for a user-facing message."""
    total_seconds = max(1, int(seconds or 0))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return _("{days}d {hours}h {minutes}m").format(
            days=days, hours=hours, minutes=minutes)
    if hours:
        return _("{hours}h {minutes}m").format(hours=hours, minutes=minutes)
    if minutes:
        return _("{minutes}m {seconds}s").format(minutes=minutes, seconds=seconds)
    return _("{seconds}s").format(seconds=seconds)


def temporary_block_message(seconds: int | None) -> str:
    """Build the one-minute-rate-limited temporary-block notice."""
    return _(
        "❌ Your account is temporarily blocked because you sent messages too quickly. "
        "Please try again in {}."
    ).format(format_block_duration(seconds))


def format_block_expiry(blocked_until, time_zone) -> str | None:
    """Render an SQLite UTC expiry timestamp in the administrator time zone."""
    if not blocked_until:
        return None
    try:
        expires_at = datetime.fromisoformat(str(blocked_until).replace("Z", "+00:00"))
    except ValueError:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at.astimezone(time_zone).strftime("%Y-%m-%d %H:%M:%S %Z")
