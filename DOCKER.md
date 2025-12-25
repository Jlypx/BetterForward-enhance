# Docker部署指南

## 📦 构建镜像

### 方式1：使用构建脚本

```bash
chmod +x build.sh
./build.sh
```

### 方式2：手动构建

```bash
docker build -t betterforward:latest .
```

## 🚀 运行容器

### 基本运行

```bash
docker run -d \
  --name betterforward \
  -e TOKEN=你的机器人Token \
  -e GROUP_ID=你的群组ID \
  -e LANGUAGE=zh_CN \
  -v $(pwd)/data:/app/data \
  betterforward:latest
```

### 完整配置

```bash
docker run -d \
  --name betterforward \
  --restart unless-stopped \
  -e TOKEN=你的机器人Token \
  -e GROUP_ID=你的群组ID \
  -e LANGUAGE=zh_CN \
  -e TG_API=https://api.telegram.org \
  -e WORKER=2 \
  -v $(pwd)/data:/app/data \
  betterforward:latest
```

## 📊 容器管理

### 查看日志
```bash
docker logs -f betterforward
```

### 停止容器
```bash
docker stop betterforward
```

### 启动容器
```bash
docker start betterforward
```

### 重启容器
```bash
docker restart betterforward
```

### 删除容器
```bash
docker rm -f betterforward
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
   docker stop betterforward
   docker rm betterforward
   ```

2. 重新构建镜像：
   ```bash
   docker build -t betterforward:latest .
   ```

3. 启动新容器（使用相同命令）

## 🐛 故障排查

### 查看完整日志
```bash
docker logs betterforward
```

### 进入容器调试
```bash
docker exec -it betterforward sh
```

### 检查环境变量
```bash
docker exec betterforward env
```

### 查看数据库
```bash
docker exec -it betterforward sqlite3 /app/data/storage.db
```

## 💡 Docker Compose (推荐)

创建 `docker-compose.yml`：

```yaml
version: '3.8'

services:
  betterforward:
    build: .
    container_name: betterforward
    restart: unless-stopped
    environment:
      - TOKEN=你的机器人Token
      - GROUP_ID=你的群组ID
      - LANGUAGE=zh_CN
      - TG_API=https://api.telegram.org
      - WORKER=2
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
