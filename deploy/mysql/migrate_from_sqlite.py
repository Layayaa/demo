#!/usr/bin/env python3
"""
SQLite -> MySQL 迁移脚本
用法:
  python3 migrate_from_sqlite.py

环境变量:
  DB_HOST/DB_PORT/DB_USER/DB_PASS/DB_NAME
  SQLITE_DB_DIR (可选，默认尝试 /opt/inquiry-system/database 和 /opt/inquiry-system/data/database)
"""

import os
import sys
import sqlite3

try:
    import pymysql
except ImportError:
    print("请先安装 pymysql: pip install pymysql")
    sys.exit(1)


MYSQL_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': int(os.environ.get('DB_PORT', '3306')),
    'user': os.environ.get('DB_USER', 'inquiry'),
    'password': os.environ.get('DB_PASS', ''),
    'database': os.environ.get('DB_NAME', 'inquiry_system'),
    'charset': 'utf8mb4',
}

SQLITE_DBS = {
    'inquiry_file': 'inquiry_file.db',
    'price_record': 'price_record.db',
    'upload_audit': 'upload_audit.db',
    'user': 'user.db',
}


def resolve_sqlite_db_dir():
    candidates = [
        os.environ.get('SQLITE_DB_DIR', '').strip(),
        '/opt/inquiry-system/database',
        '/opt/inquiry-system/data/database',
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return candidate
    return candidates[1]


def connect_mysql():
    return pymysql.connect(**MYSQL_CONFIG)


def sqlite_table_columns(sqlite_conn, table_name):
    cursor = sqlite_conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]


def sanitize_engineer_name(value):
    if value is None:
        return '未提取'
    text = str(value).strip()
    if not text or text.lower() in {'nan', 'none', 'null', '-', '--'}:
        return '未提取'
    return text


def migrate_table(sqlite_path, table_name, target_columns, mysql_conn, column_aliases=None, transform=None, optional_columns=None):
    """
    迁移单个表。

    column_aliases: {target_col: [candidate_source_col_1, ...]}
    transform: Callable[[dict], dict]
    """
    if not os.path.exists(sqlite_path):
        print(f"  跳过 {table_name}: SQLite 文件不存在 -> {sqlite_path}")
        return 0

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cols = sqlite_table_columns(sqlite_conn, table_name)
    sqlite_col_set = set(sqlite_cols)

    optional_columns = set(optional_columns or [])
    source_mapping = {}
    for target_col in target_columns:
        candidates = [target_col]
        if column_aliases and target_col in column_aliases:
            candidates.extend(column_aliases[target_col])
        source_mapping[target_col] = next((c for c in candidates if c in sqlite_col_set), None)

    missing_required = [k for k, v in source_mapping.items() if v is None and k not in optional_columns]
    if missing_required:
        print(f"  跳过 {table_name}: 源表缺少列 -> {missing_required}")
        sqlite_conn.close()
        return 0

    query_cols = ', '.join(source_mapping[col] for col in target_columns if source_mapping[col] is not None)
    cursor = sqlite_conn.cursor()
    cursor.execute(f"SELECT {query_cols} FROM {table_name}")
    rows = cursor.fetchall()
    if not rows:
        print(f"  {table_name}: 无数据")
        sqlite_conn.close()
        return 0

    prepared_rows = []
    source_cols = [col for col in target_columns if source_mapping[col] is not None]
    for row in rows:
        record = {col: row[idx] for idx, col in enumerate(source_cols)}
        for target_col in target_columns:
            if target_col not in record:
                record[target_col] = None
        if transform:
            record = transform(record)
        prepared_rows.append([record[col] for col in target_columns])

    mysql_cursor = mysql_conn.cursor()
    cols = ', '.join(target_columns)
    placeholders = ', '.join(['%s'] * len(target_columns))
    sql = f"INSERT INTO {table_name} ({cols}) VALUES ({placeholders})"

    inserted = 0
    for row in prepared_rows:
        try:
            mysql_cursor.execute(sql, row)
            inserted += 1
        except pymysql.IntegrityError:
            # 主键冲突或约束冲突时跳过
            continue
        except Exception as exc:
            print(f"  {table_name} 插入失败: {exc}")

    mysql_conn.commit()
    sqlite_conn.close()
    print(f"  {table_name}: 迁移 {inserted} 条记录")
    return inserted


def normalize_inquiry_file(row):
    row['engineer_name'] = sanitize_engineer_name(row.get('engineer_name'))
    return row


def normalize_price_record(row):
    row['engineer_name'] = sanitize_engineer_name(row.get('engineer_name'))
    return row


def normalize_upload_audit(row):
    row['engineer_name'] = sanitize_engineer_name(row.get('engineer_name'))
    return row


def main():
    sqlite_db_dir = resolve_sqlite_db_dir()
    print("=" * 60)
    print("SQLite -> MySQL 数据迁移")
    print(f"SQLite 目录: {sqlite_db_dir}")
    print("=" * 60)

    try:
        mysql_conn = connect_mysql()
        print("MySQL 连接成功")
    except Exception as exc:
        print(f"MySQL 连接失败: {exc}")
        sys.exit(1)

    total = 0

    print("\n[1/5] 迁移 user")
    total += migrate_table(
        os.path.join(sqlite_db_dir, SQLITE_DBS['user']),
        'user',
        ['id', 'username', 'phone', 'password_hash', 'real_name', 'department', 'role', 'is_active', 'created_at', 'last_login'],
        mysql_conn,
        optional_columns={'username'},
    )

    print("\n[2/5] 迁移 inquiry_file")
    total += migrate_table(
        os.path.join(sqlite_db_dir, SQLITE_DBS['inquiry_file']),
        'inquiry_file',
        ['file_id', 'file_name', 'stored_file_name', 'upload_time', 'upload_user', 'department', 'engineer_name', 'batch_no', 'parse_status', 'record_count', 'validity_months'],
        mysql_conn,
        column_aliases={
            # 旧库没有该字段时会回落失败，因此这里映射到 file_name 作为兼容值
            'stored_file_name': ['file_name'],
        },
        transform=normalize_inquiry_file,
    )

    print("\n[3/5] 迁移 query_log")
    total += migrate_table(
        os.path.join(sqlite_db_dir, SQLITE_DBS['inquiry_file']),
        'query_log',
        ['log_id', 'material_name', 'query_time', 'engineer_name', 'department', 'status', 'note'],
        mysql_conn,
    )

    print("\n[4/5] 迁移 price_record")
    total += migrate_table(
        os.path.join(sqlite_db_dir, SQLITE_DBS['price_record']),
        'price_record',
        ['record_id', 'file_id', 'reference_count', 'valid_until', 'project_name', 'material_name', 'specification', 'unit', 'price', 'is_tax_included', 'supplier', 'region', 'quote_date', 'remark', 'department', 'engineer_name', 'engineer_user_id', 'inquiry_type'],
        mysql_conn,
        transform=normalize_price_record,
        optional_columns={'engineer_user_id'},
    )

    print("\n[5/5] 迁移 upload_audit")
    total += migrate_table(
        os.path.join(sqlite_db_dir, SQLITE_DBS['upload_audit']),
        'upload_audit',
        ['audit_id', 'file_id', 'upload_time', 'upload_user', 'department', 'engineer_name', 'status', 'note'],
        mysql_conn,
        column_aliases={
            # 兼容旧脚本字段名
            'upload_time': ['created_at'],
        },
        transform=normalize_upload_audit,
    )

    mysql_conn.close()
    print("\n" + "=" * 60)
    print(f"迁移完成，共迁移 {total} 条记录")
    print("=" * 60)


if __name__ == '__main__':
    main()
