#!/bin/bash
# 一键安装脚本
# 文件位置: /opt/inquiry-system/deploy/install.sh
# 使用方法: sudo bash install.sh

set -e

echo "================================================"
echo "  企业内部历史询价复用系统 - 安装脚本"
echo "================================================"

# 检查root权限
if [ "$EUID" -ne 0 ]; then
    echo "请使用 root 权限运行此脚本"
    exit 1
fi

# 配置变量
APP_DIR="/opt/inquiry-system"
APP_USER="www-data"
PYTHON_VERSION="3.10"

# 1. 安装系统依赖
echo ""
echo "[1/7] 安装系统依赖..."
apt update
apt install -y python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python3-pip nginx mysql-server supervisor

# 2. 创建目录结构
echo ""
echo "[2/7] 创建目录结构..."
mkdir -p $APP_DIR/{app/backend,app/frontend,data/database,data/uploads,logs/nginx,logs/gunicorn,config,scripts}
mkdir -p /opt/backup

# 3. 复制应用代码
echo ""
echo "[3/7] 复制应用代码..."
# 假设当前目录是部署包
cp -r backend/* $APP_DIR/app/backend/
cp -r frontend/* $APP_DIR/app/frontend/

# 4. 创建Python虚拟环境
echo ""
echo "[4/7] 创建Python虚拟环境..."
python${PYTHON_VERSION} -m venv $APP_DIR/venv
source $APP_DIR/venv/bin/activate
pip install --upgrade pip
pip install -r $APP_DIR/app/backend/requirements.txt
pip install gunicorn gevent pymysql cryptography
deactivate

# 5. 配置MySQL数据库
echo ""
echo "[5/7] 配置MySQL数据库..."
read -p "请输入MySQL root密码: " -s MYSQL_ROOT_PASS
echo ""
read -p "请输入新数据库名 [inquiry_system]: " DB_NAME
DB_NAME=${DB_NAME:-inquiry_system}
read -p "请输入新数据库用户 [inquiry]: " DB_USER
DB_USER=${DB_USER:-inquiry}
read -p "请输入新数据库用户密码: " -s DB_PASS
echo ""

mysql -u root -p$MYSQL_ROOT_PASS << EOF
CREATE DATABASE IF NOT EXISTS $DB_NAME CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON $DB_NAME.* TO '$DB_USER'@'localhost';
FLUSH PRIVILEGES;
EOF

# 导入数据库结构
mysql -u $DB_USER -p$DB_PASS $DB_NAME < $APP_DIR/deploy/mysql/schema.sql

# 6. 配置应用
echo ""
echo "[6/7] 配置应用..."

# 生成密钥
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

# 创建环境变量文件
cat > $APP_DIR/config/env << EOF
export SECRET_KEY="$SECRET_KEY"
export DATABASE_URL="mysql+pymysql://$DB_USER:$DB_PASS@localhost:3306/$DB_NAME?charset=utf8mb4"
export FLASK_ENV="production"
EOF

# 复制配置文件
cp $APP_DIR/deploy/config/nginx.conf.example /etc/nginx/sites-available/inquiry-system
ln -sf /etc/nginx/sites-available/inquiry-system /etc/nginx/sites-enabled/
cp $APP_DIR/deploy/config/gunicorn.conf.py.example $APP_DIR/config/gunicorn.conf.py
cp $APP_DIR/deploy/config/supervisor.conf.example /etc/supervisor/conf.d/inquiry-system.conf
cp $APP_DIR/deploy/scripts/*.sh $APP_DIR/scripts/
chmod +x $APP_DIR/scripts/*.sh

# 7. 设置权限
echo ""
echo "[7/7] 设置权限..."
chown -R $APP_USER:$APP_USER $APP_DIR
chmod -R 755 $APP_DIR
chmod 600 $APP_DIR/config/env

# 完成
echo ""
echo "================================================"
echo "  安装完成！"
echo "================================================"
echo ""
echo "下一步操作："
echo "1. 修改 Nginx 配置中的域名: /etc/nginx/sites-available/inquiry-system"
echo "2. 申请 SSL 证书: certbot --nginx -d your-domain.com"
echo "3. 启动服务: supervisorctl start inquiry-system"
echo ""
echo "默认管理员账号："
echo "  手机号: 13800138000"
echo "  密码: 138000"
echo ""
