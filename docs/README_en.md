# BetterForward Enhance

[中文说明](README.md)

BetterForward Enhance is a Telegram message-forwarding and safety-management bot built for groups with topics. Each user conversation is mapped to a dedicated topic in an admin group, so a team can respond without exposing personal accounts.

## Features

- One topic per user for private-message forwarding
- Collaborative replies, bans, and conversation termination
- English, Simplified Chinese, and Japanese interfaces
- Scheduled and regex-aware automatic responses
- Image, math, and Cloudflare Turnstile human verification
- Keyword spam isolation with an extensible detector interface
- Private and group message queues, rate limits, and temporary abuse blocks
- Broadcast messages to all users

## Quick Start

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Create a Telegram group with topics enabled, add the bot as an administrator, and obtain the group ID.
3. Clone this repository and set `TOKEN`, `GROUP_ID`, and `LANGUAGE` in `docker-compose.yml`.
4. Start the service:

```bash
docker compose up -d
```

The published image is `ghcr.io/jlypx/betterforward-enhance:latest`. After startup, private messages to the bot are forwarded to each user's topic in the admin group. Send `/help` in the group main topic to open the administration menu.

## Configuration

- `LANGUAGE` accepts `en_US`, `zh_CN`, and `ja_JP`.
- `WORKERS` controls message-processing threads and defaults to `2`.
- `REDIS_URL` and `REDIS_PREFIX` enable shared rate limits across instances; build with `--build-arg ENABLE_REDIS=true` to install the optional dependency.
- The Turnstile WebApp is disabled by default. Configure its public HTTPS URL, site key, secret key, and listener from the bot administration menu. Saved runtime settings take precedence over environment defaults.

See the [Docker deployment guide](../DOCKER.md) for complete Docker, rate-limit, and Turnstile instructions.

## Feedback and Security

Report bugs and feature requests through [GitHub Issues](https://github.com/Jlypx/BetterForward-enhance/issues). Report vulnerabilities privately under the [security policy](SECURITY.md).
