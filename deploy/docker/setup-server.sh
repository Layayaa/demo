#!/bin/bash
set -e

echo "========================================"
echo " 服务器环境初始化脚本"
echo " 适用于 Ubuntu 20.04 / 22.04"
echo "========================================"

if [ "$EUID" -ne 0 ]; then
    echo "请使用 sudo 运行此脚本"
    exit 1
fi

echo ""
echo "[1/4] 更新系统..."
apt-get update && apt-get upgrade -y

echo ""
echo "[2/4] 安装 Docker..."
if ! command -v docker &> /dev/null; then
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable docker
    systemctl start docker
    echo "Docker 安装完成"
else
    echo "Docker 已安装，跳过"
fi

echo ""
echo "[3/4] 配置防火墙..."
if command -v ufw &> /dev/null; then
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
    echo "y" | ufw enable || true
    echo "防火墙已配置（放行 22, 80, 443）"
else
    echo "ufw 未安装，跳过防火墙配置"
fi

echo ""
echo "[4/4] 创建应用目录..."
APP_DIR="/opt/inquiry-system"
mkdir -p "$APP_DIR"
echo "应用目录: $APP_DIR"

echo ""
echo "========================================"
echo " 环境初始化完成！"
echo ""
echo " 接下来请执行："
echo "   1. 将项目代码上传到 $APP_DIR"
echo "   2. cd $APP_DIR"
echo "   3. cp .env.example .env"
echo "   4. 编辑 .env 文件，修改密码和密钥"
echo "   5. docker compose up -d"
echo "========================================"
