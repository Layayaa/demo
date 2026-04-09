#!/bin/bash
set -e

echo "========================================"
echo " 企业内部历史询价复用系统 - Docker 部署"
echo "========================================"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_DIR"

if [ ! -f .env ]; then
    echo ""
    echo "[错误] 未找到 .env 文件"
    echo "请先执行: cp .env.example .env"
    echo "然后编辑 .env 文件，修改密码和密钥"
    exit 1
fi

source .env
if [ "$SECRET_KEY" = "change_me_generate_a_random_secret_key" ] || [ -z "$SECRET_KEY" ]; then
    echo ""
    echo "[警告] SECRET_KEY 未修改，自动生成随机密钥..."
    NEW_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || openssl rand -hex 32)
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "s|SECRET_KEY=.*|SECRET_KEY=$NEW_KEY|" .env
    else
        sed -i "s|SECRET_KEY=.*|SECRET_KEY=$NEW_KEY|" .env
    fi
    echo "已自动写入 .env"
fi

echo ""
echo "[1/3] 构建 Docker 镜像..."
docker compose build --no-cache

echo ""
echo "[2/3] 启动服务..."
docker compose up -d

echo ""
echo "[3/3] 等待服务就绪..."
echo -n "等待 MySQL 启动"
for i in $(seq 1 30); do
    if docker compose exec -T mysql mysqladmin ping -h localhost -u root -p"$MYSQL_ROOT_PASSWORD" --silent 2>/dev/null; then
        echo " OK"
        break
    fi
    echo -n "."
    sleep 2
done

echo -n "等待应用启动"
for i in $(seq 1 15); do
    if curl -sf http://localhost/health > /dev/null 2>&1; then
        echo " OK"
        break
    fi
    echo -n "."
    sleep 2
done

echo ""
echo "========================================"
echo " 部署完成！"
echo ""

SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -n "$SERVER_IP" ]; then
    echo " 访问地址: http://$SERVER_IP"
else
    echo " 访问地址: http://localhost"
fi

echo ""
echo " 默认管理员: ${INITIAL_ADMIN_PHONE:-13800138000}"
echo " 默认密码: 手机号后六位（登录后请立即修改）"
echo ""
echo " 常用命令："
echo "   查看日志:   docker compose logs -f"
echo "   查看状态:   docker compose ps"
echo "   停止服务:   docker compose down"
echo "   重启服务:   docker compose restart"
echo "   更新部署:   docker compose down && docker compose up -d --build"
echo "========================================"
