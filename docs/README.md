# BetterForward Enhance

[English](README_en.md)

BetterForward Enhance 是面向 Telegram 话题群的私信转发与安全管理机器人。它将每位用户的消息映射到管理群中的独立话题，使多个管理员可以在不暴露个人账号的前提下处理会话。

## 功能

- 按用户创建独立话题并转发消息
- 多管理员协作回复、封禁和终止会话
- 中文、英文和日文界面
- 自动回复、定时自动回复和正则匹配
- 图片、数学题和 Cloudflare Turnstile 人机验证
- 关键词垃圾消息隔离，以及可扩展的检测器接口
- 私聊和群组消息队列、限流和临时封禁
- 向全部用户广播消息

## 快速部署

1. 使用 [@BotFather](https://t.me/BotFather) 创建机器人并获取 Token。
2. 创建已启用话题的 Telegram 群组，将机器人添加为管理员，并取得群组 ID。
3. 克隆本仓库，编辑 `docker-compose.yml` 中的 `TOKEN`、`GROUP_ID` 和 `LANGUAGE`。
4. 启动服务：

```bash
docker compose up -d
```

镜像发布到 `ghcr.io/jlypx/betterforward-enhance:latest`。首次运行后，机器人私聊消息会转发到管理群内对应的用户话题。管理员在群组主话题中发送 `/help` 可打开管理菜单。

## 配置

- `LANGUAGE` 支持 `en_US`、`zh_CN` 和 `ja_JP`。
- `WORKERS` 控制消息处理线程数，默认值为 `2`。
- `REDIS_URL` 和 `REDIS_PREFIX` 仅用于多实例共享限流；构建镜像时需添加 `--build-arg ENABLE_REDIS=true`。
- Turnstile WebApp 默认关闭。可在机器人管理菜单配置公开 HTTPS 地址、Site Key、Secret Key 和监听端口；运行时保存的配置优先于环境变量。

完整的 Docker 部署、限流和 Turnstile 配置见 [Docker 部署指南](../DOCKER.md)。

## 反馈与安全

功能建议和缺陷请提交到 [GitHub Issues](https://github.com/Jlypx/BetterForward-enhance/issues)。安全问题请按 [安全策略](SECURITY.md) 私下报告。
