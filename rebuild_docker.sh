#!/bin/bash
# 重新构建Docker镜像（无警告版本）

echo "🔨 重新构建BetterForward Enhance Docker镜像..."
echo ""

# 删除旧镜像（如果存在）
if docker images | grep -q betterforward-enhance; then
    echo "🗑️  删除旧镜像..."
    docker rmi betterforward-enhance:latest 2>/dev/null || true
fi

# 构建新镜像
docker build -t betterforward-enhance:latest .

echo ""
echo "✅ 构建完成！"
echo ""
echo "📊 镜像信息："
docker images | grep betterforward-enhance
