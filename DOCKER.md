# Docker部署指南

## 📦 构建镜像

### 方式1：使用构建脚本

```bash
chmod +x build.sh
./build.sh
```

### 方式2：手动构建

```bash
docker build -t betterforward-enhance:latest .
```

## 🚀 运行容器

### 基本运行

```bash
docker run -d \
  --name betterforward-enhance \
  -e TOKEN=你的机器人Token \
  -e GROUP_ID=你的群组ID \
  -e LANGUAGE=zh_CN \
  -v $(pwd)/data:/app/data \
  betterforward-enhance:latest
```

### 完整配置

```bash
docker run -d \
  --name betterforward-enhance \
  --restart unless-stopped \
  -e TOKEN=你的机器人Token \
  -e GROUP_ID=你的群组ID \
  -e LANGUAGE=zh_CN \
  -e TG_API=https://api.telegram.org \
  -e WORKERS=2 \
  -v $(pwd)/data:/app/data \
  betterforward-enhance:latest
```

## 🛡️ 消息洪泛防护

默认启用以下限制：私聊队列 1000 条、群组优先队列 200 条、同一用户积压 5 条；未验证用户每 10 秒 1 条，普通已验证用户每分钟 10 条且最多瞬时 3 条。管理员成功回复已验证用户后，该用户进入活跃会话档位，每分钟 30 条且最多瞬时 10 条，并在私聊队列中优先处理；连续空闲 24 小时后自动回落到普通已验证档位。单用户连续 20 次超额后会临时封禁 1 小时。

可通过 `QUEUE_SIZE`、`GROUP_QUEUE_SIZE`、`PER_USER_QUEUE_SIZE`、`UNVERIFIED_RATE`、`VERIFIED_RATE`、`PRIORITY_RATE`、`PRIORITY_BURST`、`PRIORITY_INACTIVITY_SECONDS`、`GLOBAL_RATE`、`ABUSE_BLOCK_THRESHOLD` 和 `ABUSE_BLOCK_SECONDS` 调整。队列满或超额的消息会直接丢弃，不会触发机器人回复或数据库写入。

### 可选 Redis 多实例限流

单实例不需要 Redis。多实例部署时，构建镜像时显式安装可选依赖：

```bash
docker build --build-arg ENABLE_REDIS=true -t betterforward-enhance:latest .
```

随后为所有实例配置相同的 `REDIS_URL` 和 `REDIS_PREFIX`。Redis 不可用时机器人会自动降级为本地限流并记录错误日志。

### Telegram WebApp + Cloudflare Turnstile

Turnstile WebApp 默认关闭，且不增加额外 Python 依赖。配置会保存到 `data/storage.db`，运行中修改后立即生效；环境变量只用于第一次配置前的默认值。

1. 在 Cloudflare Turnstile 创建 Managed Widget，添加验证域名并取得 Site Key 与 Secret Key。
2. 使用 Caddy、Nginx 或 Cloudflare Tunnel 将公开 HTTPS 地址反向代理到 `127.0.0.1:8080`。Telegram Mini App 的公开地址必须使用 HTTPS。
3. 在机器人管理菜单打开 `Turnstile WebApp`，依次设置 Public URL、Site Key、Secret Key、Expected hostname、监听地址和端口。
4. 启用 WebApp，然后在 `Captcha Settings` 中选择 `Cloudflare Turnstile`。

Caddy 示例：

```caddyfile
verify.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

对应 Public URL 为 `https://verify.example.com`，Expected hostname 为 `verify.example.com`。如果使用 Docker Compose，默认只将 WebApp 端口发布到宿主机回环地址：

```yaml
ports:
  - "127.0.0.1:8080:8080"
```

也可以在第一次启动前设置 `WEBAPP_ENABLED`、`WEBAPP_PUBLIC_URL`、`TURNSTILE_SITE_KEY`、`TURNSTILE_SECRET_KEY`、`TURNSTILE_HOSTNAME`、`WEBAPP_HOST`、`WEBAPP_PORT` 和 `WEBAPP_AUTH_MAX_AGE`。一旦在运行时保存配置，后续启动以 SQLite 中的值为准。

Secret Key 不会显示在管理状态页，管理员发送密钥的 Telegram 消息会立即删除。密钥仍以明文保存在 SQLite 中，因此应限制 `data` 目录和备份文件的访问权限。

## 📊 容器管理

### 查看日志
```bash
docker logs -f betterforward-enhance
```

### 停止容器
```bash
docker stop betterforward-enhance
```

### 启动容器
```bash
docker start betterforward-enhance
```

### 重启容器
```bash
docker restart betterforward-enhance
```

### 删除容器
```bash
docker rm -f betterforward-enhance
```

## 🌍 语言设置

支持的语言：
- `en_US` - English (默认)
- `zh_CN` - 简体中文
- `ja_JP` - 日本語

## 📁 数据持久化

建议挂载 `/app/data` 目录以保存：
- 数据库文件 (`storage.db`)
- 缓存文件
- 日志文件

## 🔄 更新

1. 停止并删除旧容器：
   ```bash
   docker stop betterforward-enhance
   docker rm betterforward-enhance
   ```

2. 重新构建镜像：
   ```bash
   docker build -t betterforward-enhance:latest .
   ```

3. 启动新容器（使用相同命令）

## 🐛 故障排查

### 查看完整日志
```bash
docker logs betterforward-enhance
```

### 进入容器调试
```bash
docker exec -it betterforward-enhance sh
```

### 检查环境变量
```bash
docker exec betterforward-enhance env
```

### 查看数据库
```bash
docker exec -it betterforward-enhance sqlite3 /app/data/storage.db
```

## 💡 Docker Compose (推荐)

创建 `docker-compose.yml`：

```yaml
version: '3.8'

services:
  betterforward-enhance:
    build: .
    container_name: betterforward-enhance
    restart: unless-stopped
    environment:
      - TOKEN=你的机器人Token
      - GROUP_ID=你的群组ID
      - LANGUAGE=zh_CN
      - TG_API=https://api.telegram.org
      - WORKERS=2
    volumes:
      - ./data:/app/data
```

运行：
```bash
docker-compose up -d
```

查看日志：
```bash
docker-compose logs -f
```
