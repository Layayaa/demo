#!/bin/bash
# 停止脚本
# 文件位置: /opt/inquiry-system/scripts/stop.sh

set -e

echo "正在停止询价系统..."

# 停止服务
sudo supervisorctl stop inquiry-system

echo "✓ 服务已停止"
