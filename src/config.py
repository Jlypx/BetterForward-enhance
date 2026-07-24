"""Configuration module for BetterForward."""

import argparse
import gettext
import logging
import os
import signal

import telebot.apihelper


def positive_int(value: str) -> int:
    """Parse a strictly positive integer command-line value."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    """Parse a strictly positive floating-point command-line value."""
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


parser = argparse.ArgumentParser(description="BetterForward - Telegram message forwarding bot")
parser.add_argument("-token", type=str, required=True, help="Telegram bot token")
parser.add_argument("-group_id", type=str, required=True, help="Group ID")
parser.add_argument("-language", type=str, default="en_US", help="Language",
                    choices=["en_US", "zh_CN", "ja_JP"])
parser.add_argument("-tg_api", type=str, required=False, default="", help="Telegram API URL")
parser.add_argument("-workers", "-worker", dest="workers", type=positive_int, default=5,
                    help="Number of worker threads for message processing (default: 5)")
parser.add_argument("-queue_size", type=positive_int, default=1000,
                    help="Maximum pending private messages (default: 1000)")
parser.add_argument("-group_queue_size", type=positive_int, default=200,
                    help="Maximum pending group messages (default: 200)")
parser.add_argument("-per_user_queue_size", type=positive_int, default=5,
                    help="Maximum queued messages per processing user (default: 5)")
parser.add_argument("-unverified_rate", type=positive_float, default=0.1,
                    help="Unverified-user messages per second (default: 0.1)")
parser.add_argument("-unverified_burst", type=positive_int, default=1,
                    help="Unverified-user burst size (default: 1)")
parser.add_argument("-verified_rate", type=positive_float, default=1 / 6,
                    help="Verified-user messages per second (default: 0.1667)")
parser.add_argument("-verified_burst", type=positive_int, default=3,
                    help="Verified-user burst size (default: 3)")
parser.add_argument("-priority_rate", type=positive_float, default=0.5,
                    help="Admin-replied user messages per second (default: 0.5)")
parser.add_argument("-priority_burst", type=positive_int, default=10,
                    help="Admin-replied user burst size (default: 10)")
parser.add_argument("-priority_inactivity_seconds", type=positive_int, default=86400,
                    help="Idle time before admin-replied users return to normal limits (default: 86400)")
parser.add_argument("-global_rate", type=positive_float, default=10.0,
                    help="Global private-message rate per second (default: 10)")
parser.add_argument("-global_burst", type=positive_int, default=20,
                    help="Global private-message burst size (default: 20)")
parser.add_argument("-abuse_block_threshold", type=positive_int, default=20,
                    help="Rate-limit violations before a temporary block (default: 20)")
parser.add_argument("-abuse_block_seconds", type=positive_int, default=3600,
                    help="Temporary rate-limit block duration in seconds (default: 3600)")
parser.add_argument("-rate_limit_state_size", type=positive_int, default=10000,
                    help="Maximum local rate-limit records (default: 10000)")
parser.add_argument("-redis_url", type=str, default="",
                    help="Optional Redis URL for shared multi-instance rate limits")
parser.add_argument("-redis_prefix", type=str, default="betterforward",
                    help="Redis key prefix for rate-limit state (default: betterforward)")
parser.add_argument("-webapp_enabled", choices=["enable", "disable"], default="disable",
                    help="Initial Turnstile WebApp state before runtime configuration")
parser.add_argument("-webapp_public_url", type=str, default="",
                    help="Initial public HTTPS URL for the verification WebApp")
parser.add_argument("-turnstile_site_key", type=str, default="",
                    help="Initial Cloudflare Turnstile Site Key")
parser.add_argument("-turnstile_secret_key", type=str, default="",
                    help="Initial Cloudflare Turnstile Secret Key")
parser.add_argument("-turnstile_hostname", type=str, default="",
                    help="Initial expected Turnstile response hostname")
parser.add_argument("-webapp_host", type=str, default="0.0.0.0",
                    help="Initial WebApp listen host (default: 0.0.0.0)")
parser.add_argument("-webapp_port", type=positive_int, default=8080,
                    help="Initial WebApp listen port (default: 8080)")
parser.add_argument("-webapp_auth_max_age", type=positive_int, default=300,
                    help="Maximum Telegram initData age in seconds (default: 300)")
args = parser.parse_args()

logger = logging.getLogger()
logger.setLevel("INFO")
BASIC_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
formatter = logging.Formatter(BASIC_FORMAT, DATE_FORMAT)
chlr = logging.StreamHandler()
chlr.setFormatter(formatter)
logger.addHandler(chlr)

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
locale_dir = os.path.join(project_root, "locale")
gettext.bindtextdomain("BetterForward", locale_dir)
gettext.textdomain("BetterForward")
try:
    _ = gettext.translation("BetterForward", locale_dir, languages=[args.language]).gettext
except FileNotFoundError:
    _ = gettext.gettext

stop = False

if args.tg_api != "":
    telebot.apihelper.API_URL = f"{args.tg_api}/bot{{0}}/{{1}}"


def handle_sigterm(*_):
    """Stop polling and queue workers when the process receives a termination signal."""
    global stop
    stop = True
    raise KeyboardInterrupt()


signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)
