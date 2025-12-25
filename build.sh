#!/bin/bash
# Docker构建脚本

set -e

echo "🔍 检查Docker是否可用..."
if ! command -v docker &> /dev/null; then
    echo "❌ Docker未安装或不可用"
    echo "请先安装Docker: https://docs.docker.com/get-docker/"
    exit 1
fi

echo "📦 开始构建BetterForward Docker镜像..."

# 构建镜像
docker build -t betterforward:latest .

echo "✅ 构建完成！"
echo ""
echo "🚀 运行方式："
echo "docker run -d \\"
echo "  --name betterforward \\"
echo "  -e TOKEN=your_bot_token \\"
echo "  -e GROUP_ID=your_group_id \\"
echo "  -e LANGUAGE=zh_CN \\"
echo "  -v ./data:/app/data \\"
echo "  betterforward:latest"
echo ""
echo "📋 查看日志："
echo "docker logs -f betterforward"
