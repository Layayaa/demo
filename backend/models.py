"""
数据库模型定义
企业内部历史询价复用系统
四库分离：inquiry_file.db（文件管理）+ price_record.db（询价明细）+ upload_audit.db（审计留痕）+ user.db（用户管理）
"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


# ============ 文件数据库 (inquiry_file.db) ============

class InquiryFile(db.Model):
    """管理每次上传的原始文件"""
    __bind_key__ = 'inquiry_file'
    __tablename__ = 'inquiry_file'

    file_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    file_name = db.Column(db.String(255), nullable=False, comment='原始文件名')
    stored_file_name = db.Column(db.String(255), comment='磁盘存储文件名')
    upload_time = db.Column(db.DateTime, default=datetime.now, comment='上传时间')
    upload_user = db.Column(db.String(100), comment='上传人')
    department = db.Column(db.String(100), comment='填报部门')
    engineer_name = db.Column(db.String(100), nullable=False, comment='填报工程师')
    batch_no = db.Column(db.String(50), comment='文件批次号')
    parse_status = db.Column(db.String(20), default='success', comment='解析状态')
    record_count = db.Column(db.Integer, default=0, comment='解析记录数')
    validity_months = db.Column(db.Integer, default=12, comment='有效期月数')

    def to_dict(self):
        return {
            'file_id': self.file_id,
            'file_name': self.file_name,
            'stored_file_name': self.stored_file_name,
            'upload_time': self.upload_time.strftime('%Y-%m-%d %H:%M:%S') if self.upload_time else None,
            'upload_user': self.upload_user,
            'department': self.department,
            'engineer_name': self.engineer_name,
            'batch_no': self.batch_no,
            'parse_status': self.parse_status,
            'record_count': self.record_count,
            'validity_months': self.validity_months
        }


class QueryLog(db.Model):
    """查询历史记录"""
    __bind_key__ = 'inquiry_file'
    __tablename__ = 'query_log'

    log_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    material_name = db.Column(db.String(255), comment='查询的材料名称')
    query_time = db.Column(db.DateTime, default=datetime.now, comment='查询时间')
    engineer_name = db.Column(db.String(100), comment='查询工程师')
    department = db.Column(db.String(100), comment='查询部门')
    status = db.Column(db.String(20), default='completed', comment='状态')
    note = db.Column(db.Text, comment='备注')


# ============ 审计数据库 (upload_audit.db) ============

class UploadAudit(db.Model):
    """形成上传与责任留痕"""
    __bind_key__ = 'upload_audit'
    __tablename__ = 'upload_audit'

    audit_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    file_id = db.Column(db.Integer, comment='关联文件ID')
    upload_time = db.Column(db.DateTime, default=datetime.now, comment='上传时间')
    upload_user = db.Column(db.String(100), comment='上传人')
    department = db.Column(db.String(100), comment='填报部门')
    engineer_name = db.Column(db.String(100), nullable=False, comment='填报工程师')
    status = db.Column(db.String(20), default='completed', comment='状态')
    note = db.Column(db.Text, comment='备注')

    def to_dict(self):
        return {
            'audit_id': self.audit_id,
            'file_id': self.file_id,
            'upload_time': self.upload_time.strftime('%Y-%m-%d %H:%M:%S') if self.upload_time else None,
            'upload_user': self.upload_user,
            'department': self.department,
            'engineer_name': self.engineer_name,
            'status': self.status,
            'note': self.note
        }


# ============ 业务数据库 (price_record.db) ============

class PriceRecord(db.Model):
    """管理每条询价明细记录"""
    __bind_key__ = 'price_record'
    __tablename__ = 'price_record'

    record_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    file_id = db.Column(db.Integer, comment='关联文件ID')
    reference_count = db.Column(db.Integer, default=0, comment='引用计数')
    valid_until = db.Column(db.Date, comment='有效期至')

    # 基础价格字段
    project_name = db.Column(db.String(255), comment='项目名称')
    material_name = db.Column(db.String(255), nullable=False, comment='材料名称')
    specification = db.Column(db.String(255), comment='规格型号')
    unit = db.Column(db.String(50), comment='单位')
    price = db.Column(db.Float, comment='单价')
    is_tax_included = db.Column(db.String(10), comment='是否含税')
    supplier = db.Column(db.String(255), comment='供应商/来源')
    region = db.Column(db.String(100), comment='地区')
    quote_date = db.Column(db.Date, comment='报价时间')
    remark = db.Column(db.Text, comment='备注')

    # 责任字段
    department = db.Column(db.String(100), comment='填报部门')
    engineer_name = db.Column(db.String(100), nullable=False, comment='填报工程师')
    engineer_user_id = db.Column(db.Integer, comment='关联用户ID')

    # 业务标识字段
    inquiry_type = db.Column(db.String(50), comment='询价类别')

    def to_dict(self):
        # 计算有效期状态
        validity_status = 'unknown'
        if self.valid_until:
            from datetime import date
            today = date.today()
            if self.valid_until < today:
                validity_status = 'expired'
            elif (self.valid_until - today).days <= 30:
                validity_status = 'expiring_soon'
            else:
                validity_status = 'valid'

        return {
            'record_id': self.record_id,
            'file_id': self.file_id,
            'reference_count': self.reference_count,
            'valid_until': self.valid_until.strftime('%Y-%m-%d') if self.valid_until else None,
            'validity_status': validity_status,
            'project_name': self.project_name,
            'material_name': self.material_name,
            'specification': self.specification,
            'unit': self.unit,
            'price': self.price,
            'is_tax_included': self.is_tax_included,
            'supplier': self.supplier,
            'region': self.region,
            'quote_date': self.quote_date.strftime('%Y-%m-%d') if self.quote_date else None,
            'remark': self.remark,
            'department': self.department,
            'engineer_name': self.engineer_name,
            'engineer_user_id': self.engineer_user_id,
            'inquiry_type': self.inquiry_type,
            # 来源追溯信息 - 需要跨库查询
            'source_file_name': None,  # 由业务层填充
            'source_upload_time': None
        }


# ============ 用户数据库 (user.db) ============

class User(db.Model):
    """用户管理表"""
    __bind_key__ = 'user'
    __tablename__ = 'user'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(64), unique=True, comment='用户名')
    phone = db.Column(db.String(11), unique=True, nullable=False, comment='手机号')
    password_hash = db.Column(db.String(128), nullable=False, comment='密码哈希')
    real_name = db.Column(db.String(100), comment='真实姓名')
    department = db.Column(db.String(100), comment='部门')
    role = db.Column(db.String(20), default='user', comment='角色: admin/user')
    is_active = db.Column(db.Boolean, default=True, comment='账号状态')
    created_at = db.Column(db.DateTime, default=datetime.now, comment='创建时间')
    last_login = db.Column(db.DateTime, comment='最后登录时间')

    def set_password(self, password):
        """设置密码（加密存储）"""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """验证密码"""
        return check_password_hash(self.password_hash, password)

    def get_default_password(self):
        """获取默认密码（手机号后六位）"""
        return self.phone[-6:] if self.phone else ''

    @property
    def is_admin(self):
        """是否为管理员"""
        return self.role == 'admin'

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'phone': self.phone,
            'real_name': self.real_name,
            'department': self.department,
            'role': self.role,
            'is_active': self.is_active,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else None,
            'last_login': self.last_login.strftime('%Y-%m-%d %H:%M:%S') if self.last_login else None
        }

    # Flask-Login 所需方法
    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)


class EngineerBinding(db.Model):
    """工程师名与用户账号绑定"""
    __bind_key__ = 'user'
    __tablename__ = 'engineer_binding'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    engineer_name_raw = db.Column(db.String(100), nullable=False, comment='原始工程师名')
    engineer_name_norm = db.Column(db.String(100), nullable=False, index=True, comment='标准化工程师名')
    user_id = db.Column(db.Integer, nullable=False, index=True, comment='关联用户ID')
    bind_type = db.Column(db.String(20), default='auto', comment='绑定方式: auto/manual')
    confidence = db.Column(db.Float, default=1.0, comment='匹配置信度')
    created_at = db.Column(db.DateTime, default=datetime.now, comment='创建时间')
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')

    def to_dict(self):
        return {
            'id': self.id,
            'engineer_name_raw': self.engineer_name_raw,
            'engineer_name_norm': self.engineer_name_norm,
            'user_id': self.user_id,
            'bind_type': self.bind_type,
            'confidence': self.confidence,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else None,
            'updated_at': self.updated_at.strftime('%Y-%m-%d %H:%M:%S') if self.updated_at else None
        }
