#!/bin/bash
# 备份脚本
# 文件位置: /opt/inquiry-system/scripts/backup.sh
# 定时任务: 0 2 * * * /opt/inquiry-system/scripts/backup.sh

set -e

# 配置
APP_DIR="/opt/inquiry-system"
BACKUP_DIR="/opt/backup"
DATE=$(date +%Y%m%d_%H%M%S)
RETENTION_DAYS=7

# MySQL配置（从环境变量读取）
DB_USER="${DB_USER:-inquiry}"
DB_PASS="${DB_PASS:-}"
DB_NAME="${DB_NAME:-inquiry_system}"

# 创建备份目录
mkdir -p $BACKUP_DIR

echo "=== 开始备份 $(date) ==="

# 1. 备份MySQL数据库
echo "备份数据库..."
mysqldump -u $DB_USER -p$DB_PASS $DB_NAME | gzip > $BACKUP_DIR/db_$DATE.sql.gz
echo "✓ 数据库备份完成: db_$DATE.sql.gz"

# 2. 备份上传文件
echo "备份上传文件..."
tar -czf $BACKUP_DIR/uploads_$DATE.tar.gz -C $APP_DIR/data uploads/
echo "✓ 上传文件备份完成: uploads_$DATE.tar.gz"

# 3. 备份配置文件
echo "备份配置文件..."
tar -czf $BACKUP_DIR/config_$DATE.tar.gz -C $APP_DIR/config .
echo "✓ 配置文件备份完成: config_$DATE.tar.gz"

# 4. 清理旧备份（保留最近N天）
echo "清理旧备份..."
find $BACKUP_DIR -name "*.gz" -mtime +$RETENTION_DAYS -delete
echo "✓ 已清理 $RETENTION_DAYS 天前的备份"

# 5. 计算备份大小
BACKUP_SIZE=$(du -sh $BACKUP_DIR | cut -f1)
echo "备份目录大小: $BACKUP_SIZE"

# 6. 可选：上传到阿里云OSS
# 需要先配置 ossutil
# ossutil cp $BACKUP_DIR/db_$DATE.sql.gz oss://your-bucket/backup/

echo "=== 备份完成 $(date) ==="
