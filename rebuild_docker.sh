#!/bin/bash
# 重新构建Docker镜像（无警告版本）

echo "🔨 重新构建BetterForward Docker镜像..."
echo ""

# 删除旧镜像（如果存在）
if docker images | grep -q betterforward; then
    echo "🗑️  删除旧镜像..."
    docker rmi betterforward:latest 2>/dev/null || true
fi

# 构建新镜像
docker build -t betterforward:latest .

echo ""
echo "✅ 构建完成！"
echo ""
echo "📊 镜像信息："
docker images | grep betterforward
