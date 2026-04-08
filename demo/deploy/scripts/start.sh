#!/bin/bash
# 启动脚本
# 文件位置: /opt/inquiry-system/scripts/start.sh

set -e

APP_DIR="/opt/inquiry-system"
LOG_DIR="$APP_DIR/logs"

echo "正在启动询价系统..."

# 加载环境变量
if [ -f "$APP_DIR/config/env" ]; then
    source "$APP_DIR/config/env"
fi

# 启动服务
sudo supervisorctl start inquiry-system

# 检查服务状态
sleep 3
if sudo supervisorctl status inquiry-system | grep -q "RUNNING"; then
    echo "✓ 服务启动成功"
    echo "访问地址: https://$(hostname)"
else
    echo "✗ 服务启动失败，请检查日志"
    tail -n 20 $LOG_DIR/app.err.log
    exit 1
fi
