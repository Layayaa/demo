-- MySQL 数据库初始化脚本
-- 使用方法: mysql -u root -p < schema.sql

CREATE DATABASE IF NOT EXISTS inquiry_system
CHARACTER SET utf8mb4
COLLATE utf8mb4_unicode_ci;

USE inquiry_system;

-- ============================================
-- 用户表（对应 backend/models.py: User）
-- ============================================
CREATE TABLE IF NOT EXISTS user (
    id INT AUTO_INCREMENT PRIMARY KEY,
    phone VARCHAR(11) UNIQUE NOT NULL COMMENT '手机号',
    password_hash VARCHAR(128) NOT NULL COMMENT '密码哈希',
    real_name VARCHAR(100) COMMENT '真实姓名',
    department VARCHAR(100) COMMENT '部门',
    role VARCHAR(20) DEFAULT 'user' COMMENT '角色: admin/user',
    is_active BOOLEAN DEFAULT TRUE COMMENT '账号状态',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    last_login DATETIME COMMENT '最后登录时间',
    INDEX idx_phone (phone),
    INDEX idx_department (department),
    INDEX idx_role (role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户表';

-- ============================================
-- 文件管理表（对应 backend/models.py: InquiryFile）
-- ============================================
CREATE TABLE IF NOT EXISTS inquiry_file (
    file_id INT AUTO_INCREMENT PRIMARY KEY,
    file_name VARCHAR(255) NOT NULL COMMENT '原始文件名',
    stored_file_name VARCHAR(255) COMMENT '磁盘存储文件名',
    upload_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '上传时间',
    upload_user VARCHAR(100) COMMENT '上传人',
    department VARCHAR(100) COMMENT '填报部门',
    engineer_name VARCHAR(100) NOT NULL COMMENT '填报工程师',
    batch_no VARCHAR(50) COMMENT '文件批次号',
    parse_status VARCHAR(20) DEFAULT 'success' COMMENT '解析状态',
    record_count INT DEFAULT 0 COMMENT '解析记录数',
    validity_months INT DEFAULT 12 COMMENT '有效期月数',
    CONSTRAINT ck_inquiry_file_engineer_nonempty CHECK (CHAR_LENGTH(TRIM(engineer_name)) > 0),
    INDEX idx_upload_time (upload_time),
    INDEX idx_department (department),
    INDEX idx_engineer (engineer_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='文件管理表';

-- ============================================
-- 查询日志表（对应 backend/models.py: QueryLog）
-- ============================================
CREATE TABLE IF NOT EXISTS query_log (
    log_id INT AUTO_INCREMENT PRIMARY KEY,
    material_name VARCHAR(255) COMMENT '查询的材料名称',
    query_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '查询时间',
    engineer_name VARCHAR(100) COMMENT '查询工程师',
    department VARCHAR(100) COMMENT '查询部门',
    status VARCHAR(20) DEFAULT 'completed' COMMENT '状态',
    note TEXT COMMENT '备注',
    INDEX idx_query_time (query_time),
    INDEX idx_query_engineer (engineer_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='查询日志表';

-- ============================================
-- 审计留痕表（对应 backend/models.py: UploadAudit）
-- ============================================
CREATE TABLE IF NOT EXISTS upload_audit (
    audit_id INT AUTO_INCREMENT PRIMARY KEY,
    file_id INT COMMENT '关联文件ID',
    upload_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '上传时间',
    upload_user VARCHAR(100) COMMENT '上传人',
    department VARCHAR(100) COMMENT '填报部门',
    engineer_name VARCHAR(100) NOT NULL COMMENT '填报工程师',
    status VARCHAR(20) DEFAULT 'completed' COMMENT '状态',
    note TEXT COMMENT '备注',
    CONSTRAINT ck_upload_audit_engineer_nonempty CHECK (CHAR_LENGTH(TRIM(engineer_name)) > 0),
    INDEX idx_upload_audit_file (file_id),
    INDEX idx_upload_audit_time (upload_time),
    INDEX idx_upload_audit_engineer (engineer_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='上传审计表';

-- ============================================
-- 询价记录表（对应 backend/models.py: PriceRecord）
-- ============================================
CREATE TABLE IF NOT EXISTS price_record (
    record_id INT AUTO_INCREMENT PRIMARY KEY,
    file_id INT COMMENT '关联文件ID',
    reference_count INT DEFAULT 0 COMMENT '引用计数',
    valid_until DATE COMMENT '有效期至',
    project_name VARCHAR(255) COMMENT '项目名称',
    material_name VARCHAR(255) NOT NULL COMMENT '材料名称',
    specification VARCHAR(255) COMMENT '规格型号',
    unit VARCHAR(50) COMMENT '单位',
    price DOUBLE COMMENT '单价',
    is_tax_included VARCHAR(10) COMMENT '是否含税',
    supplier VARCHAR(255) COMMENT '供应商/来源',
    region VARCHAR(100) COMMENT '地区',
    quote_date DATE COMMENT '报价时间',
    remark TEXT COMMENT '备注',
    department VARCHAR(100) COMMENT '填报部门',
    engineer_name VARCHAR(100) NOT NULL COMMENT '填报工程师',
    inquiry_type VARCHAR(50) COMMENT '询价类别',
    CONSTRAINT ck_price_record_engineer_nonempty CHECK (CHAR_LENGTH(TRIM(engineer_name)) > 0),
    INDEX idx_material (material_name),
    INDEX idx_specification (specification),
    INDEX idx_region (region),
    INDEX idx_quote_date (quote_date),
    INDEX idx_engineer (engineer_name),
    INDEX idx_supplier (supplier),
    INDEX idx_file_id (file_id),
    FULLTEXT INDEX ft_material (material_name, specification, supplier, remark)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='询价记录表';

-- ============================================
-- 默认管理员由应用启动逻辑自动创建：
-- backend/app.py -> create_initial_admin()
-- ============================================
