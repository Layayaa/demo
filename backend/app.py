"""
企业内部历史询价复用系统 - Flask主应用
"""
import os
import sys
import json
import html
import hmac
import re
import time
import secrets
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from flask import Flask, request, jsonify, send_from_directory, redirect, url_for, session, render_template
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
import pandas as pd
from werkzeug.utils import secure_filename

from models import db, InquiryFile, PriceRecord, UploadAudit, QueryLog, User, EngineerBinding
from template_config import (
    FIELD_KEYWORDS, REQUIRED_FIELDS, DATA_CLEANING_RULES,
    match_column_to_field, build_column_mapping, detect_multi_supplier,
    clean_value, clean_price, clean_supplier, clean_date
)
from nlp_parser import NLPParser, parse_query
from smart_query_service import enrich_parsed_params, rank_records

# 创建Flask应用
app = Flask(__name__, static_folder='../frontend/static', template_folder='../frontend')

# 配置 - 四库分离
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATABASE_DIR = os.path.join(BASE_DIR, 'database')

# 确保数据库目录存在
if not os.path.exists(DATABASE_DIR):
    os.makedirs(DATABASE_DIR)

# 四个独立的数据库
INQUIRY_FILE_DB = os.path.join(DATABASE_DIR, 'inquiry_file.db')
PRICE_RECORD_DB = os.path.join(DATABASE_DIR, 'price_record.db')
UPLOAD_AUDIT_DB = os.path.join(DATABASE_DIR, 'upload_audit.db')
USER_DB = os.path.join(DATABASE_DIR, 'user.db')

# 数据库配置 - 默认 SQLite 四库分离，生产环境可通过 DATABASE_URL 切换到 MySQL
database_url = (os.environ.get('DATABASE_URL') or '').strip()
if database_url:
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_BINDS'] = {
        'inquiry_file': database_url,
        'price_record': database_url,
        'upload_audit': database_url,
        'user': database_url
    }
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{INQUIRY_FILE_DB}'
    app.config['SQLALCHEMY_BINDS'] = {
        'inquiry_file': f'sqlite:///{INQUIRY_FILE_DB}',
        'price_record': f'sqlite:///{PRICE_RECORD_DB}',
        'upload_audit': f'sqlite:///{UPLOAD_AUDIT_DB}',
        'user': f'sqlite:///{USER_DB}'
    }
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 最大16MB

# Session 配置
secret_key = os.environ.get('SECRET_KEY') or os.environ.get('FLASK_SECRET_KEY')
if not secret_key:
    secret_key = os.urandom(32).hex()
    print("[SECURITY] 未设置 SECRET_KEY，已使用进程内随机密钥。生产环境请通过环境变量设置固定值。", flush=True)
app.config['SECRET_KEY'] = secret_key
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

CSRF_HEADER_NAME = 'X-CSRF-Token'
CSRF_MUTATION_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
CSRF_EXEMPT_PATHS = {'/api/login', '/api/register'}

RATE_LIMIT_DEFAULT = (120, 60)
RATE_LIMIT_RULES = {
    '/api/login': (5, 300),
    '/api/upload': (20, 300),
    '/api/natural_query': (60, 60),
    '/api/query': (90, 60)
}
_rate_limit_buckets = defaultdict(deque)
_rate_limit_lock = threading.Lock()
ALLOWED_USER_ROLES = {'admin', 'user'}
_schema_checked = False

# 启用CORS（排除登录相关路由）
cors_origins_env = os.environ.get('CORS_ORIGINS', '').strip()
if cors_origins_env:
    cors_origins = [origin.strip() for origin in cors_origins_env.split(',') if origin.strip()]
else:
    cors_origins = ['http://localhost:5000', 'http://127.0.0.1:5000']
CORS(app, supports_credentials=True, origins=cors_origins)

# 初始化数据库
db.init_app(app)

# 初始化 Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'
login_manager.session_protection = 'strong'


@login_manager.user_loader
def load_user(user_id):
    """加载用户回调"""
    try:
        return User.query.get(int(user_id))
    except:
        return None


# 权限装饰器
def admin_required(func):
    """管理员权限装饰器"""
    from functools import wraps
    @wraps(func)
    def decorated_view(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({'success': False, 'message': '请先登录'}), 401
        if not current_user.is_admin:
            return jsonify({'success': False, 'message': '需要管理员权限'}), 403
        return func(*args, **kwargs)
    return decorated_view


def api_login_required(func):
    """API 登录验证装饰器 - 返回 JSON 而不是重定向"""
    from functools import wraps
    @wraps(func)
    def decorated_view(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({'success': False, 'message': '请先登录'}), 401
        return func(*args, **kwargs)
    return decorated_view


def normalize_engineer_name(value):
    """统一清洗工程师姓名，空值返回空字符串。"""
    if value is None:
        return ''
    text = str(value).strip()
    if not text:
        return ''
    if text.lower() in {'nan', 'none', 'null', 'n/a', '--', '-'}:
        return ''
    return text


def normalize_engineer_key(value):
    """工程师名标准化，用于用户账号关联。"""
    text = normalize_engineer_name(value).lower()
    if not text:
        return ''
    text = re.sub(r'\s+', '', text)
    text = text.replace('工程师', '').replace('工程', '').replace('老师', '')
    if text.endswith('工') and len(text) > 1:
        text = text[:-1]
    return text


def mask_phone(phone):
    phone = (phone or '').strip()
    if len(phone) != 11:
        return ''
    return f"{phone[:3]}****{phone[-4:]}"


def get_user_by_engineer_name(engineer_name):
    """根据工程师名查找已绑定用户。"""
    norm_name = normalize_engineer_key(engineer_name)
    if not norm_name:
        return None

    binding = EngineerBinding.query.filter_by(engineer_name_norm=norm_name).order_by(EngineerBinding.id.asc()).first()
    if binding:
        return User.query.get(binding.user_id)

    # 兜底：按实名标准化匹配
    users = User.query.filter(User.real_name != None, User.real_name != '').all()
    for user in users:
        if normalize_engineer_key(user.real_name) == norm_name:
            return user
    return None


def get_user_by_upload_user(upload_user):
    """根据上传人字段匹配用户账号（优先用户名，其次实名）。"""
    name = normalize_engineer_name(upload_user)
    if not name:
        return None

    lowered = name.lower()
    user = User.query.filter(
        User.username != None,
        db.func.lower(User.username) == lowered
    ).first()
    if user:
        return user

    user = User.query.filter(User.real_name == name).first()
    if user:
        return user

    return User.query.filter(
        User.real_name != None,
        db.func.lower(User.real_name) == lowered
    ).first()


def get_upload_user_display(upload_user):
    """上传人显示名：优先账号实名，兜底原始上传人字段。"""
    user = get_user_by_upload_user(upload_user)
    if user and normalize_engineer_name(user.real_name):
        return normalize_engineer_name(user.real_name)
    return normalize_engineer_name(upload_user)


def normalize_specification_text(value):
    """规格型号查询归一化：去空格和常见中英文标点。"""
    text = (value or '').strip().lower()
    if not text:
        return ''
    return re.sub(r'[\s，。！？、,.!?；;：:\-_/\\|（）()\[\]【】]+', '', text)


def build_normalized_spec_expr(column_expr):
    """数据库侧规格归一化表达式，兼容 SQLite/MySQL。"""
    expr = db.func.lower(db.func.coalesce(column_expr, ''))
    for token in [' ', '\t', '\r', '\n', '，', ',', '。', '.', '；', ';', '：', ':', '-', '_', '/', '\\', '|', '（', '）', '(', ')', '[', ']', '【', '】']:
        expr = db.func.replace(expr, token, '')
    return expr


def apply_specification_partial_filter(query, specification):
    """规格型号部分匹配：原始 like + 归一化 like + 词项 like。"""
    raw = (specification or '').strip()
    if not raw:
        return query

    normalized = normalize_specification_text(raw)
    normalized_expr = build_normalized_spec_expr(PriceRecord.specification)

    conditions = [PriceRecord.specification.like(f'%{raw}%')]
    if normalized:
        conditions.append(normalized_expr.like(f'%{normalized}%'))

    tokens = [item for item in re.split(r'[\s,，;；:/：\\\-_.]+', raw) if item]
    for token in tokens:
        if len(token) >= 2:
            conditions.append(PriceRecord.specification.like(f'%{token}%'))

    return query.filter(db.or_(*conditions))


UPLOADER_LOOKUP_NOISE = (
    '谁上传了这个报价', '这个报价谁上传的', '报价是谁上传的', '上传人是谁',
    '谁上传', '谁传的报价', '谁提交的报价', '联系上传人', '上传这个报价的人',
    '谁传的文件', '文件是谁上传的', '谁传上来的', '这份文件在谁手上',
    '这个文件在谁手里', '谁有这份文件', '谁持有这份资料', '资料在谁手上',
    '这个资料谁有', '报价', '文件', '资料', '询价', '记录', '是谁', '是谁上传的'
)


def extract_lookup_subject(query_text):
    """去除意图噪声词，提取查询主体（材料/文件关键词）。"""
    text = (query_text or '').strip()
    if not text:
        return ''

    cleaned = text
    for token in UPLOADER_LOOKUP_NOISE:
        cleaned = cleaned.replace(token, ' ')
    cleaned = re.sub(r'[\s，。！？、,.!?；;：:\-_/()（）\[\]【】]+', ' ', cleaned).strip()
    return cleaned[:100]


FOLLOW_UP_REFERENCE_TOKENS = (
    '这份', '这个', '这条', '该', '此', '上面', '刚才', '上一条', '上述', '刚刚'
)

FOLLOW_UP_REFERENCE_SUBJECTS = {
    '这份', '这个', '这条', '该', '此', '上面', '刚才', '上一条', '上述',
    '这份报价', '这个报价', '该报价', '此报价', '这份文件', '这个文件',
    '该文件', '此文件', '这份资料', '这个资料', '该资料', '此资料'
}


def _compact_text(value):
    text = (value or '').strip()
    text = re.sub(r'[\s，。！？、,.!?；;：:\-_/()（）\[\]【】]+', '', text)
    return text


def is_followup_reference_query(query_text, lookup_subject):
    """判断是否是“这份/这个/上面”这类承接上一轮的追问。"""
    compact_query = _compact_text(query_text)
    compact_subject = _compact_text(lookup_subject)

    has_reference_word = any(token in query_text for token in FOLLOW_UP_REFERENCE_TOKENS)
    if compact_subject in FOLLOW_UP_REFERENCE_SUBJECTS:
        return True

    if has_reference_word and (not compact_subject or compact_subject in FOLLOW_UP_REFERENCE_SUBJECTS):
        return True

    if has_reference_word and '谁上传' in compact_query:
        return True

    return False


def is_engineer_followup_query(query_text):
    """Detect follow-up requests asking who is responsible."""
    text = _compact_text(query_text)
    if not text:
        return False

    trigger_tokens = (
        '谁负责', '谁负责的', '负责人', '负责人是谁', '谁在负责',
        '谁跟进', '谁在跟进', '谁经手', '谁处理', '哪个工程师',
        '工程师是谁', '联系工程师'
    )
    return any(token in text for token in trigger_tokens)


SUBMISSION_FOLLOW_UP_TOKENS = (
    '这位', '这个人', '该人', '他', '她', '该上传人', '这个上传人',
    '这个部门', '该部门', '这个组', '上面那个', '上面这位', '上一位'
)


def normalize_submission_actor_key(value):
    text = normalize_engineer_name(value).lower()
    text = re.sub(r'\s+', '', text)
    return text


def normalize_submission_department_key(value):
    text = (value or '').strip().lower()
    if not text:
        return ''
    text = re.sub(r'\s+', '', text)
    text = text.replace('部门', '部')
    if text.endswith('部分') and len(text) > 2:
        text = text[:-1]
    return text


def format_submission_time_scope(parsed_params):
    display = (parsed_params or {}).get('time_display')
    if display:
        return str(display)
    return '近1年'


def filter_success_inquiry_files_for_submission(start_date=None, end_date=None, uploader_filters=None, department_filters=None):
    """按上传人/部门/时间过滤成功上传文件（用于智能统计与追溯）。"""
    uploader_filters = [normalize_submission_actor_key(item) for item in (uploader_filters or []) if normalize_submission_actor_key(item)]
    department_filters = [normalize_submission_department_key(item) for item in (department_filters or []) if normalize_submission_department_key(item)]

    query = InquiryFile.query.filter(InquiryFile.parse_status == 'success')

    if start_date:
        start_dt = datetime.combine(start_date, datetime.min.time())
        query = query.filter(InquiryFile.upload_time >= start_dt)
    if end_date:
        end_dt = datetime.combine(end_date, datetime.max.time())
        query = query.filter(InquiryFile.upload_time <= end_dt)

    rows = query.order_by(InquiryFile.upload_time.desc()).all()
    out = []
    for item in rows:
        upload_display = get_upload_user_display(item.upload_user) or normalize_engineer_name(item.upload_user) or ''
        uploader_keys = {
            normalize_submission_actor_key(upload_display),
            normalize_submission_actor_key(item.upload_user),
        }
        department_key = normalize_submission_department_key(item.department)

        if uploader_filters:
            matched = False
            for kw in uploader_filters:
                if any(key and kw in key for key in uploader_keys):
                    matched = True
                    break
            if not matched:
                continue

        if department_filters:
            if not department_key:
                continue
            if not any(kw in department_key for kw in department_filters):
                continue

        out.append(item)

    return out


FILE_TRACE_NOISE_WORDS = {
    '文件', '附件', '报价', '报价单', '询价', '询价单', '询价表', '报价表',
    '记录', '资料', '哪个文件', '哪个附件', '来自哪个文件', '来自哪个附件',
    '来源文件', '来源附件', '这份文件', '这个文件', '这份报价', '这个报价',
    '这条记录', '是谁上传的', '谁上传的', '谁上传', '上传人',
    '这条', '这个', '这份', '来自哪个', '来自哪份', '来自', '哪个', '哪份', '是谁', '上传的'
}

_ATTACHMENT_CN_TO_NUM = {
    '一': '1', '二': '2', '三': '3', '四': '4', '五': '5',
    '六': '6', '七': '7', '八': '8', '九': '9', '十': '10'
}
_ATTACHMENT_NUM_TO_CN = {v: k for k, v in _ATTACHMENT_CN_TO_NUM.items()}


def normalize_file_trace_keyword(value):
    text = (value or '').strip().lower()
    if not text:
        return ''
    text = re.sub(r'[\s，。！？、,.!?；;：:\-_/()（）\[\]【】]+', '', text)
    return text


def _expand_attachment_token_variants(token):
    variants = {token}
    if not token.startswith('附件'):
        return variants

    suffix = token[2:]
    if not suffix:
        return variants

    # 附件一 <=> 附件1
    if suffix in _ATTACHMENT_CN_TO_NUM:
        variants.add('附件' + _ATTACHMENT_CN_TO_NUM[suffix])
    if suffix in _ATTACHMENT_NUM_TO_CN:
        variants.add('附件' + _ATTACHMENT_NUM_TO_CN[suffix])

    return variants


def build_file_trace_keywords(file_keywords=None, lookup_subject=''):
    raw_tokens = list(file_keywords or [])
    if lookup_subject:
        raw_tokens.append(lookup_subject)

    normalized = []
    for raw in raw_tokens:
        value = normalize_file_trace_keyword(raw)
        if not value or value in FILE_TRACE_NOISE_WORDS:
            continue

        # lookup_subject 可能是一句话，这里再切一遍
        chunks = re.findall(r'[\u4e00-\u9fffA-Za-z0-9._\-]{2,80}', value)
        if not chunks:
            chunks = [value]

        for chunk in chunks:
            item = normalize_file_trace_keyword(chunk)
            if not item or item in FILE_TRACE_NOISE_WORDS:
                continue
            normalized.append(item)

    deduped = []
    seen = set()
    for token in normalized:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def inquiry_file_matches_keywords(inquiry_file, keywords=None):
    keywords = keywords or []
    if not keywords:
        return True

    display_upload_user = get_upload_user_display(inquiry_file.upload_user) or ''
    haystacks = [
        normalize_file_trace_keyword(inquiry_file.file_name),
        normalize_file_trace_keyword(getattr(inquiry_file, 'stored_file_name', None)),
        normalize_file_trace_keyword(inquiry_file.upload_user),
        normalize_file_trace_keyword(display_upload_user),
        normalize_file_trace_keyword(inquiry_file.engineer_name),
        normalize_file_trace_keyword(inquiry_file.department),
    ]

    for keyword in keywords:
        if not keyword:
            continue
        variants = _expand_attachment_token_variants(keyword)
        for variant in variants:
            if any(field and variant in field for field in haystacks):
                return True

    return False


def ensure_engineer_binding(user, source_name, bind_type='auto', confidence=1.0):
    """确保工程师名与用户存在绑定关系。"""
    if not user:
        return
    norm_name = normalize_engineer_key(source_name)
    raw_name = normalize_engineer_name(source_name)
    if not norm_name or not raw_name:
        return

    binding = EngineerBinding.query.filter_by(engineer_name_norm=norm_name).first()
    if binding:
        binding.user_id = user.id
        binding.bind_type = bind_type
        binding.confidence = confidence
        binding.engineer_name_raw = raw_name
        return

    db.session.add(EngineerBinding(
        engineer_name_raw=raw_name,
        engineer_name_norm=norm_name,
        user_id=user.id,
        bind_type=bind_type,
        confidence=confidence
    ))


def auto_bind_engineer_for_user(user):
    """注册后自动做工程师名关联。"""
    if not user:
        return 0

    bound_count = 0
    if user.real_name:
        ensure_engineer_binding(user, user.real_name, bind_type='auto', confidence=1.0)
        bound_count += 1

    # 历史数据中同名工程师自动绑定
    normalized_name = normalize_engineer_key(user.real_name)
    if normalized_name:
        distinct_engineers = db.session.query(PriceRecord.engineer_name).distinct().all()
        for row in distinct_engineers:
            candidate = row[0]
            if normalize_engineer_key(candidate) == normalized_name:
                ensure_engineer_binding(user, candidate, bind_type='auto', confidence=1.0)
                bound_count += 1

    return bound_count


def get_or_create_csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token


def get_request_csrf_token():
    token = request.headers.get(CSRF_HEADER_NAME, '')
    if token:
        return token

    token = request.form.get('csrf_token', '')
    if token:
        return token

    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload.get('csrf_token', '')
    return ''


def get_client_ip():
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def _rate_limit_rule(path):
    return RATE_LIMIT_RULES.get(path, RATE_LIMIT_DEFAULT)


def _check_and_record_rate_limit(bucket_key, limit, window_seconds):
    now_ts = time.time()
    with _rate_limit_lock:
        bucket = _rate_limit_buckets[bucket_key]
        cutoff = now_ts - window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            retry_after = max(1, int(window_seconds - (now_ts - bucket[0])) + 1)
            return False, retry_after

        bucket.append(now_ts)
        return True, 0


def sanitize_json_value(value):
    """统一对 JSON 字符串做 HTML 转义，降低存储型/反射型 XSS 风险。"""
    if isinstance(value, str):
        return html.escape(value, quote=True)
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {k: sanitize_json_value(v) for k, v in value.items()}
    return value


def api_internal_error(log_prefix, exc):
    """统一内部错误响应，避免向前端暴露内部异常细节。"""
    try:
        db.session.rollback()
    except Exception:
        pass
    print(f"[{log_prefix}] {exc}", flush=True)
    return jsonify({'success': False, 'message': '服务器内部错误，请稍后重试'}), 500


def resolve_uploaded_file_path(inquiry_file):
    """按 file_id 关联的存储文件名精确定位上传文件。"""
    upload_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
    stored_name = (getattr(inquiry_file, 'stored_file_name', None) or '').strip()

    if stored_name:
        candidate = os.path.abspath(os.path.join(upload_dir, stored_name))
        upload_root = os.path.abspath(upload_dir)
        if candidate.startswith(upload_root + os.sep) and os.path.exists(candidate):
            return candidate

    # 兼容历史数据：stored_file_name 为空时回退旧逻辑
    safe_original_name = secure_filename(inquiry_file.file_name or '')
    if os.path.isdir(upload_dir):
        for filename in os.listdir(upload_dir):
            if filename.endswith('_' + inquiry_file.file_name) or (safe_original_name and filename.endswith('_' + safe_original_name)):
                return os.path.join(upload_dir, filename)

    direct = os.path.join(upload_dir, inquiry_file.file_name or '')
    if os.path.exists(direct):
        return direct

    return None


# 初始化自然语言解析器
nlp_parser = None

def init_nlp_parser():
    """初始化NLP解析器并加载材料词库"""
    global nlp_parser
    import traceback
    import sys
    try:
        print("[NLP] 开始初始化自然语言解析器...", flush=True)
        nlp_parser = NLPParser(db.session)
        print("[NLP] NLPParser 创建成功", flush=True)
        nlp_parser.load_material_names()
        print("[NLP] 自然语言解析器初始化完成", flush=True)
    except Exception as e:
        print(f"[NLP] 解析器初始化失败: {e}", flush=True)
        traceback.print_exc()
        nlp_parser = NLPParser()  # 降级使用无数据库版本
        print("[NLP] 使用降级模式（无数据库）", flush=True)

# 上传目录
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)


# 应用启动后初始化NLP解析器
@app.before_request
def ensure_nlp_parser():
    """确保NLP解析器已初始化"""
    global _schema_checked
    if not _schema_checked:
        ensure_schema_compatibility()
        _schema_checked = True

    global nlp_parser
    if nlp_parser is None:
        print("[NLP] before_request 触发，开始初始化...", flush=True)
        init_nlp_parser()


@app.before_request
def enforce_api_rate_limit():
    """接口限流：按 IP+接口 维度控制请求频率。"""
    if not request.path.startswith('/api/'):
        return None
    if request.method == 'OPTIONS':
        return None

    limit, window_seconds = _rate_limit_rule(request.path)
    identity = str(current_user.get_id()) if current_user.is_authenticated else get_client_ip()
    bucket_key = f"{identity}:{request.path}:{request.method}"
    allowed, retry_after = _check_and_record_rate_limit(bucket_key, limit, window_seconds)
    if allowed:
        return None

    response = jsonify({
        'success': False,
        'message': '请求过于频繁，请稍后重试'
    })
    response.status_code = 429
    response.headers['Retry-After'] = str(retry_after)
    return response


@app.before_request
def enforce_csrf():
    """写接口 CSRF 校验。"""
    if not request.path.startswith('/api/'):
        return None
    if request.method not in CSRF_MUTATION_METHODS:
        return None
    if request.method == 'OPTIONS':
        return None
    if request.path in CSRF_EXEMPT_PATHS:
        return None
    if not current_user.is_authenticated:
        return None

    session_token = session.get('csrf_token', '')
    request_token = get_request_csrf_token()
    if not session_token or not request_token:
        return jsonify({'success': False, 'message': 'CSRF 校验失败，请刷新页面后重试'}), 403
    if not hmac.compare_digest(str(session_token), str(request_token)):
        return jsonify({'success': False, 'message': 'CSRF 校验失败，请刷新页面后重试'}), 403
    return None


@app.after_request
def apply_security_response(response):
    """统一安全响应头与 JSON 输出转义。"""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'

    if request.path.startswith('/api/') and current_user.is_authenticated:
        response.headers[CSRF_HEADER_NAME] = get_or_create_csrf_token()

    if response.is_json:
        payload = response.get_json(silent=True)
        if payload is not None:
            safe_payload = sanitize_json_value(payload)
            response.set_data(json.dumps(safe_payload, ensure_ascii=False))
            response.headers['Content-Type'] = 'application/json; charset=utf-8'

    return response


# ==================== 认证路由 ====================

@app.route('/login')
def login_page():
    """登录页面"""
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    return send_from_directory('../frontend', 'login.html')


@app.route('/register')
def register_page():
    """注册页面"""
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    return send_from_directory('../frontend', 'register.html')


@app.route('/api/register', methods=['POST'])
def api_register():
    """用户注册接口"""
    try:
        data = request.get_json() or {}
        username = str(data.get('username', '')).strip()
        real_name = str(data.get('real_name', '')).strip()
        phone = str(data.get('phone', '')).strip()
        department = str(data.get('department', '')).strip()
        password = str(data.get('password', ''))
        confirm_password = str(data.get('confirm_password', ''))

        if not username or not re.fullmatch(r'[A-Za-z0-9_]{3,32}', username):
            return jsonify({'success': False, 'message': '用户名需为3-32位字母数字下划线'}), 400
        if not real_name:
            return jsonify({'success': False, 'message': '真实姓名不能为空'}), 400
        if not phone or len(phone) != 11 or not phone.startswith('1'):
            return jsonify({'success': False, 'message': '手机号格式不正确'}), 400
        if len(password) < 8 or not re.search(r'[A-Za-z]', password) or not re.search(r'\d', password):
            return jsonify({'success': False, 'message': '密码至少8位且包含字母和数字'}), 400
        if password != confirm_password:
            return jsonify({'success': False, 'message': '两次密码输入不一致'}), 400

        if User.query.filter_by(username=username).first():
            return jsonify({'success': False, 'message': '用户名已存在'}), 400
        if User.query.filter_by(phone=phone).first():
            return jsonify({'success': False, 'message': '手机号已被使用'}), 400

        user = User(
            username=username,
            phone=phone,
            real_name=real_name,
            department=department,
            role='user',
            is_active=True
        )
        user.set_password(password)
        db.session.add(user)
        db.session.flush()

        bound_count = auto_bind_engineer_for_user(user)
        db.session.commit()

        return jsonify({
            'success': True,
            'message': '注册成功',
            'bound_count': bound_count,
            'user': user.to_dict()
        })
    except Exception as e:
        db.session.rollback()
        return api_internal_error('api_register', e)


@app.route('/api/login', methods=['POST'])
def api_login():
    """登录接口"""
    try:
        data = request.get_json() or {}
        account = str(data.get('phone') or data.get('username') or data.get('account') or '').strip()
        password = data.get('password', '')

        if not account:
            return jsonify({'success': False, 'message': '账号或密码错误'}), 400

        # 查询用户
        if len(account) == 11 and account.startswith('1'):
            user = User.query.filter_by(phone=account).first()
        else:
            user = User.query.filter_by(username=account).first()
        if not user:
            return jsonify({'success': False, 'message': '账号或密码错误'}), 400

        # 验证密码
        if not user.check_password(password):
            return jsonify({'success': False, 'message': '账号或密码错误'}), 400

        # 检查账号状态
        if not user.is_active:
            return jsonify({'success': False, 'message': '账号已被禁用'}), 403

        # 登录成功
        login_user(user, remember=True)
        session.permanent = True
        csrf_token = get_or_create_csrf_token()
        user.last_login = datetime.now()
        db.session.commit()

        return jsonify({
            'success': True,
            'message': '登录成功',
            'user': user.to_dict(),
            'csrf_token': csrf_token
        })

    except Exception as e:
        print(f"[登录错误] {e}")
        return jsonify({'success': False, 'message': '登录失败，请稍后重试'}), 500


@app.route('/api/logout', methods=['POST'])
@api_login_required
def api_logout():
    """退出登录"""
    session.pop('csrf_token', None)
    logout_user()
    return jsonify({'success': True, 'message': '已退出登录'})


@app.route('/api/user/info', methods=['GET'])
@api_login_required
def get_user_info():
    """获取当前用户信息"""
    return jsonify({
        'success': True,
        'user': current_user.to_dict(),
        'csrf_token': get_or_create_csrf_token()
    })


@app.route('/api/csrf-token', methods=['GET'])
@api_login_required
def get_csrf_token():
    """获取 CSRF token（前端可按需拉取）。"""
    return jsonify({
        'success': True,
        'csrf_token': get_or_create_csrf_token()
    })


@app.route('/api/user/password', methods=['POST'])
@api_login_required
def change_password():
    """修改密码"""
    try:
        data = request.get_json() or {}
        old_password = data.get('old_password', '')
        new_password = data.get('new_password', '')

        # 验证旧密码
        if not current_user.check_password(old_password):
            return jsonify({'success': False, 'message': '原密码错误'}), 400

        # 验证新密码长度
        if len(new_password) < 6:
            return jsonify({'success': False, 'message': '新密码长度至少6位'}), 400

        # 更新密码
        current_user.set_password(new_password)
        db.session.commit()

        return jsonify({'success': True, 'message': '密码修改成功'})

    except Exception as e:
        print(f"[修改密码错误] {e}")
        db.session.rollback()
        return jsonify({'success': False, 'message': '密码修改失败'}), 500


# ==================== 主页路由 ====================

@app.route('/')
@login_required
def index():
    """主页"""
    return send_from_directory('../frontend', 'index.html')


@app.route('/healthz', methods=['GET'])
def healthz():
    """应用健康检查"""
    return jsonify({'success': True, 'status': 'ok', 'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})


@app.route('/api/upload', methods=['POST'])
@api_login_required
def upload_file():
    """
    上传Excel文件并解析入库
    """
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'message': '没有上传文件'}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'message': '文件名为空'}), 400

        if not file.filename.endswith(('.xlsx', '.xls', '.csv')):
            return jsonify({'success': False, 'message': '只支持Excel文件(.xlsx, .xls)和CSV文件(.csv)'}), 400

        # 获取表单数据
        form_upload_user = normalize_engineer_name(request.form.get('upload_user', ''))
        if current_user.is_authenticated:
            # 上传人固定取当前登录账号（优先真实姓名），避免与Excel工程师混淆
            upload_user = normalize_engineer_name(current_user.real_name) or normalize_engineer_name(current_user.username) or normalize_engineer_name(current_user.phone) or form_upload_user or '未知'
            # 填报部门固定取上传人账号部门，不依赖前端输入
            department = normalize_engineer_name(current_user.department) or '未知'
        else:
            upload_user = form_upload_user or '未知'
            department = normalize_engineer_name(request.form.get('department', '')) or '未知'

        legacy_engineer_name = normalize_engineer_name(request.form.get('engineer_name', ''))
        batch_no = request.form.get('batch_no', '')
        try:
            validity_months = int(request.form.get('validity_months', 12))
        except (TypeError, ValueError):
            validity_months = 12
        validity_months = max(1, min(validity_months, 60))

        # 检查是否已经查询过相关材料（业务闭环：先查询后上传）
        query_materials = []
        for key, value in request.form.items():
            if key.startswith('query_material_'):
                query_materials.append(value)

        # 保存文件（文件名安全化，避免路径穿越）
        original_filename = file.filename
        safe_filename = secure_filename(original_filename)
        if not safe_filename:
            return jsonify({'success': False, 'message': '文件名无效'}), 400

        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_filename}"
        filepath = os.path.abspath(os.path.join(UPLOAD_FOLDER, filename))
        upload_root = os.path.abspath(UPLOAD_FOLDER)
        if not filepath.startswith(upload_root + os.sep):
            return jsonify({'success': False, 'message': '文件路径非法'}), 400

        file.save(filepath)

        # 创建文件记录
        inquiry_file = InquiryFile(
            file_name=original_filename,
            stored_file_name=filename,
            upload_user=upload_user,
            department=department,
            engineer_name='待提取',
            batch_no=batch_no,
            parse_status='processing',
            validity_months=validity_months
        )
        db.session.add(inquiry_file)
        db.session.commit()

        # 解析文件 - 支持 Excel 和 CSV
        try:
            df = None
            parse_errors = []

            # 根据文件扩展名选择解析方式
            if original_filename.endswith('.csv'):
                try:
                    # 尝试不同编码读取 CSV
                    for encoding in ['utf-8', 'gbk', 'gb2312', 'utf-8-sig']:
                        try:
                            df = pd.read_csv(filepath, encoding=encoding)
                            print(f"[智能识别] CSV使用编码: {encoding}")
                            break
                        except UnicodeDecodeError:
                            continue
                        except Exception as e:
                            parse_errors.append(f"CSV编码{encoding}: {str(e)}")
                except Exception as e:
                    parse_errors.append(f"CSV解析: {str(e)}")
            else:
                # Excel 文件解析
                try:
                    df = pd.read_excel(filepath, engine='openpyxl')
                except Exception as e1:
                    parse_errors.append(f"openpyxl引擎: {str(e1)}")
                    try:
                        df = pd.read_excel(filepath, engine='xlrd')
                    except Exception as e2:
                        parse_errors.append(f"xlrd引擎: {str(e2)}")
                        try:
                            df = pd.read_excel(filepath)
                        except Exception as e3:
                            parse_errors.append(f"默认引擎: {str(e3)}")

            if df is None or df.empty:
                if parse_errors:
                    print(f"[upload_file_parse] parser attempts failed: {'; '.join(parse_errors)}", flush=True)
                inquiry_file.parse_status = f'failed: 无法解析文件'
                db.session.commit()
                return jsonify({
                    'success': False,
                    'message': '文件解析失败，请检查文件格式或编码后重试'
                }), 400

            # ============ 智能检测数据起始行 ============
            # 检查第一行是否为合并单元格标题（大量Unnamed列）
            unnamed_count = sum(1 for col in df.columns if 'Unnamed' in str(col))
            total_cols = len(df.columns)

            print(f"[智能识别] 初始读取: 总列数={total_cols}, Unnamed列数={unnamed_count}")

            # 强制尝试跳过行查找有效列名
            found_valid = False
            for skip_rows in range(0, min(15, len(df) + 10)):
                try:
                    if skip_rows == 0:
                        df_new = df
                    else:
                        df_new = pd.read_excel(filepath, skiprows=skip_rows, engine='openpyxl')
                    new_unnamed = sum(1 for col in df_new.columns if 'Unnamed' in str(col))
                    new_total = len(df_new.columns)
                    col_str = ' '.join(str(c) for c in df_new.columns)

                    # 找到有效列名行的条件：Unnamed少于30%，且有常见列名
                    if new_unnamed < new_total * 0.3:
                        if any(kw in col_str for kw in ['材料', '单价', '序号', '项目']):
                            print(f"[智能识别] 找到有效列名行，跳过{skip_rows}行")
                            df = df_new
                            found_valid = True
                            break
                except:
                    continue

            if not found_valid:
                print(f"[智能识别] 未找到有效列名行，使用原始数据")

            record_count = 0
            skipped_rows = 0
            duplicate_skipped = 0  # 因重复跳过的记录数
            quality_issues = []

            # ============ 使用 template_config 智能列名识别 ============
            print(f"[智能识别] 原始列名: {list(df.columns)}")

            # 构建列名映射
            column_map, unmatched_cols, confidence = build_column_mapping(df.columns)

            print(f"[智能识别] 映射结果: {column_map}")
            print(f"[智能识别] 未匹配列: {unmatched_cols}")
            print(f"[智能识别] 置信度: {confidence:.2%}")

            # 检测是否为多供应商模板（Template B）
            is_multi_supplier, supplier_groups = detect_multi_supplier(df.columns)
            if is_multi_supplier:
                print(f"[智能识别] 检测到多供应商模板，将拆分供应商列")

            # ============ 智能识别填报工程师列 ============
            from template_config import detect_engineer_column
            engineer_col, engineer_source = detect_engineer_column(df, column_map)
            if engineer_col and '填报工程师' not in column_map:
                column_map['填报工程师'] = engineer_col
                print(f"[智能识别] 填报工程师列: {engineer_col} (来源: {engineer_source})")

            # 检查必须字段
            if '材料名称' not in column_map:
                # 尝试查找包含"材料"或"品名"的列
                for col in df.columns:
                    col_str = str(col).lower()
                    if any(kw in col_str for kw in ['材料', '品名', '物资', '货品', 'material']):
                        column_map['材料名称'] = col
                        print(f"[智能识别] 降级映射: 材料名称 -> {col}")
                        break

            if '材料名称' not in column_map:
                inquiry_file.parse_status = f'failed: 未找到材料名称列，请确保Excel中有材料名称相关的列'
                db.session.commit()
                return jsonify({
                    'success': False,
                    'message': f'未找到"材料名称"列，无法解析数据。当前检测到的列名: {", ".join(df.columns)}'
                }), 400

            # ============ 2. 数据获取辅助函数 ============
            def get_raw_value(row, standard_name, default=None):
                """获取指定标准列的原始值"""
                if standard_name not in column_map:
                    return default
                col = column_map[standard_name]
                try:
                    val = row[col]
                    if pd.isna(val):
                        return default
                    return val
                except (KeyError, IndexError):
                    return default

            def get_cleaned_value(row, standard_name, default=None):
                """获取指定标准列的清洗后值"""
                raw_val = get_raw_value(row, standard_name, default)
                return clean_value(raw_val, standard_name)

            def extract_engineer_candidates():
                """从 Excel 中提取工程师候选值（非空）。"""
                if '填报工程师' not in column_map:
                    return []

                engineer_column = column_map['填报工程师']
                if engineer_column not in df.columns:
                    return []

                candidates = []
                for raw_value in df[engineer_column].tolist():
                    cleaned = normalize_engineer_name(clean_value(raw_value, '填报工程师'))
                    if cleaned:
                        candidates.append(cleaned)
                return candidates

            # 检测有效的数据行
            def is_valid_data_row(row):
                """判断是否为有效数据行"""
                material = get_cleaned_value(row, '材料名称', '')
                if not material:
                    return False, '材料名称为空'
                # 跳过标题行
                title_keywords = ['材料名称', '材料', '名称', '品名', '序号', '编号', 'no', 'No', 'NO']
                if str(material) in title_keywords:
                    return False, '可能是标题行'
                # 跳过纯数字的无效行
                material_str = str(material).strip()
                if material_str.isdigit() and len(material_str) <= 3:
                    return False, '可能是序号'
                return True, ''

            # 逐行解析
            records_to_add = []
            engineer_candidates = extract_engineer_candidates()
            extracted_engineer_name = engineer_candidates[0] if engineer_candidates else ''
            engineer_user_cache = {}

            def resolve_engineer_user_id(name):
                key = normalize_engineer_key(name)
                if not key:
                    return None
                if key in engineer_user_cache:
                    return engineer_user_cache[key]
                user = get_user_by_engineer_name(name)
                engineer_user_cache[key] = user.id if user else None
                return engineer_user_cache[key]

            if not extracted_engineer_name:
                fail_message = '未从Excel提取到“填报工程师”，已拒绝入库'
                if legacy_engineer_name:
                    fail_message += '（已忽略表单工程师，需以Excel提取值为准）'

                inquiry_file.parse_status = f'failed: {fail_message}'
                inquiry_file.record_count = 0
                inquiry_file.engineer_name = '未提取'
                db.session.add(UploadAudit(
                    file_id=inquiry_file.file_id,
                    upload_user=upload_user,
                    department=department,
                    engineer_name='未提取',
                    status='failed',
                    note=fail_message
                ))
                db.session.commit()
                return jsonify({'success': False, 'message': fail_message}), 400

            if len(set(engineer_candidates)) > 1:
                quality_issues.append('检测到多个填报工程师，已按行优先写入，空值行回退为文件级工程师。')
            if legacy_engineer_name and legacy_engineer_name != extracted_engineer_name:
                quality_issues.append('已忽略表单中的工程师，统一以Excel提取结果入库。')

            # 处理多供应商模板（Template B）
            if is_multi_supplier:
                print(f"[智能识别] 使用多供应商模板解析模式")
                # 多供应商模板：需要拆分供应商列
                for idx, row in df.iterrows():
                    try:
                        # 检查基础数据是否有效
                        material_name = get_cleaned_value(row, '材料名称')
                        if not material_name:
                            skipped_rows += 1
                            continue
                        record_engineer_name = normalize_engineer_name(get_cleaned_value(row, '填报工程师')) or extracted_engineer_name
                        if not record_engineer_name:
                            skipped_rows += 1
                            quality_issues.append(f"第{idx+1}行: 填报工程师为空，已跳过")
                            continue

                        # 为每个供应商组创建一条记录
                        for i in range(1, 4):  # 支持最多3个供应商
                            price_col_name = f'单价{i}'
                            supplier_col_name = f'供应商/来源{i}' if i > 1 else '供应商/来源'
                            tax_col_name = f'是否含税{i}' if i > 1 else '是否含税'

                            # 获取该供应商的价格 - 直接从 DataFrame 列中获取
                            price = None
                            if price_col_name in column_map:
                                try:
                                    raw_val = row[column_map[price_col_name]]
                                    if not pd.isna(raw_val):
                                        price = clean_value(raw_val, '单价')
                                except:
                                    pass
                            if price is None and price_col_name in df.columns:
                                try:
                                    raw_val = row[price_col_name]
                                    if not pd.isna(raw_val):
                                        price = clean_value(raw_val, '单价')
                                except:
                                    pass

                            if price is None:
                                continue  # 该供应商无价格，跳过

                            # 获取供应商名称
                            supplier = None
                            if supplier_col_name in column_map:
                                try:
                                    raw_val = row[column_map[supplier_col_name]]
                                    if not pd.isna(raw_val):
                                        supplier = clean_value(raw_val, '供应商/来源')
                                except:
                                    pass
                            if supplier is None and supplier_col_name in df.columns:
                                try:
                                    raw_val = row[supplier_col_name]
                                    if not pd.isna(raw_val):
                                        supplier = clean_value(raw_val, '供应商/来源')
                                except:
                                    pass

                            # 获取是否含税
                            is_tax = None
                            if tax_col_name in column_map:
                                try:
                                    raw_val = row[column_map[tax_col_name]]
                                    if not pd.isna(raw_val):
                                        is_tax = clean_value(raw_val, '是否含税')
                                except:
                                    pass
                            if is_tax is None and tax_col_name in df.columns:
                                try:
                                    raw_val = row[tax_col_name]
                                    if not pd.isna(raw_val):
                                        is_tax = clean_value(raw_val, '是否含税')
                                except:
                                    pass
                            if is_tax is None:
                                is_tax = get_cleaned_value(row, '是否含税')  # 使用默认值

                            # 处理报价时间
                            quote_date_str = get_cleaned_value(row, '报价时间')
                            quote_date = None
                            if quote_date_str:
                                try:
                                    quote_date = pd.to_datetime(quote_date_str).date()
                                except:
                                    pass

                            # ============ 入库前去重检查 ============
                            specification = get_cleaned_value(row, '规格型号')

                            if is_record_exists(material_name, specification, supplier, quote_date):
                                duplicate_skipped += 1
                                print(f"[去重] 跳过重复记录: {material_name} - {specification} - {supplier}")
                                continue

                            # 计算有效期
                            valid_until = None
                            if quote_date:
                                valid_until = quote_date + relativedelta(months=validity_months)

                            # 创建记录
                            price_record = PriceRecord(
                                file_id=inquiry_file.file_id,
                                project_name=get_cleaned_value(row, '项目名称'),
                                material_name=material_name,
                                specification=specification,
                                unit=get_cleaned_value(row, '单位'),
                                price=price,
                                is_tax_included=is_tax,
                                supplier=supplier,
                                region=get_cleaned_value(row, '地区'),
                                quote_date=quote_date,
                                valid_until=valid_until,
                                remark=get_cleaned_value(row, '备注'),
                                department=get_cleaned_value(row, '填报部门') or department,
                                engineer_name=record_engineer_name,
                                engineer_user_id=resolve_engineer_user_id(record_engineer_name),
                                inquiry_type=get_cleaned_value(row, '询价类别')
                            )
                            records_to_add.append(price_record)
                            record_count += 1

                    except Exception as e:
                        print(f"[智能识别] 第{idx+1}行解析失败: {e}")
                        quality_issues.append(f"第{idx+1}行: 解析失败，已跳过")
                        continue
                print(f"[智能识别] 多供应商解析完成，记录数: {record_count}")
            else:
                # 标准模板解析
                for idx, row in df.iterrows():
                    try:
                        is_valid, reason = is_valid_data_row(row)
                        if not is_valid:
                            skipped_rows += 1
                            continue

                        # 使用智能清洗获取各字段值
                        material_name = get_cleaned_value(row, '材料名称')
                        if not material_name:
                            skipped_rows += 1
                            continue
                        record_engineer_name = normalize_engineer_name(get_cleaned_value(row, '填报工程师')) or extracted_engineer_name
                        if not record_engineer_name:
                            skipped_rows += 1
                            quality_issues.append(f"第{idx+1}行: 填报工程师为空，已跳过")
                            continue

                        # 处理单价（清洗后已是float或None）
                        price = get_cleaned_value(row, '单价')

                        # 处理报价时间（清洗后已是字符串格式或None）
                        quote_date_str = get_cleaned_value(row, '报价时间')
                        quote_date = None
                        if quote_date_str:
                            try:
                                quote_date = pd.to_datetime(quote_date_str).date()
                            except:
                                pass

                        # ============ 入库前去重检查 ============
                        specification = get_cleaned_value(row, '规格型号')
                        supplier = get_cleaned_value(row, '供应商/来源')

                        if is_record_exists(material_name, specification, supplier, quote_date):
                            duplicate_skipped += 1
                            print(f"[去重] 跳过重复记录: {material_name} - {specification} - {supplier}")
                            continue

                        # 计算有效期
                        valid_until = None
                        if quote_date:
                            valid_until = quote_date + relativedelta(months=validity_months)

                        # 创建记录 - 所有字段都经过智能清洗
                        price_record = PriceRecord(
                            file_id=inquiry_file.file_id,
                            project_name=get_cleaned_value(row, '项目名称'),
                            material_name=material_name,
                            specification=specification,
                            unit=get_cleaned_value(row, '单位'),
                            price=price,
                            is_tax_included=get_cleaned_value(row, '是否含税'),
                            supplier=supplier,
                            region=get_cleaned_value(row, '地区'),
                            quote_date=quote_date,
                            valid_until=valid_until,
                            remark=get_cleaned_value(row, '备注'),
                            department=get_cleaned_value(row, '填报部门') or department,
                            engineer_name=record_engineer_name,
                            engineer_user_id=resolve_engineer_user_id(record_engineer_name),
                            inquiry_type=get_cleaned_value(row, '询价类别')
                        )
                        records_to_add.append(price_record)
                        record_count += 1

                    except Exception as e:
                        print(f"[智能识别] 第{idx+1}行解析失败: {e}")
                        quality_issues.append(f"第{idx+1}行: 解析失败，已跳过")
                        continue

            # 批量添加记录
            for record in records_to_add:
                db.session.add(record)

            # 更新文件记录
            inquiry_file.parse_status = 'success'
            inquiry_file.record_count = record_count
            inquiry_file.engineer_name = extracted_engineer_name

            if query_materials:
                for material in query_materials:
                    db.session.add(QueryLog(
                        material_name=material,
                        query_time=datetime.now(),
                        engineer_name=extracted_engineer_name,
                        department=department,
                        status='completed'
                    ))

            # 创建审计记录
            audit = UploadAudit(
                file_id=inquiry_file.file_id,
                upload_user=upload_user,
                department=department,
                engineer_name=extracted_engineer_name,
                status='completed'
            )
            db.session.add(audit)
            db.session.commit()

            # 检查重复询价
            duplicate_check = check_duplicate_inquiry(inquiry_file.file_id)

            # 检查是否已经查询过相关材料（业务闭环：先查询后上传）
            recent_queries = QueryLog.query.filter(
                QueryLog.engineer_name == extracted_engineer_name,
                QueryLog.department == department,
                QueryLog.query_time >= datetime.now() - timedelta(hours=24)
            ).order_by(QueryLog.query_time.desc()).limit(10).all()

            query_history = []
            for query in recent_queries:
                query_history.append({
                    'material_name': query.material_name,
                    'query_time': query.query_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'status': query.status
                })

            # 构建返回消息
            message = f'上传成功，共解析{record_count}条记录'
            if duplicate_skipped > 0:
                message += f'，跳过{duplicate_skipped}条重复记录'

            return jsonify({
                'success': True,
                'message': message,
                'file_id': inquiry_file.file_id,
                'record_count': record_count,
                'skipped_rows': skipped_rows,
                'duplicate_skipped': duplicate_skipped,
                'column_mapping': {k: v for k, v in column_map.items()},
                'quality_issues': quality_issues[:10],  # 只返回前10个问题
                'duplicate_check': duplicate_check,
                'query_history': query_history
            })

        except Exception as e:
            fail_message = '文件解析异常'
            print(f"[upload_file_parse] {e}", flush=True)
            inquiry_file.parse_status = f'failed: {fail_message}'
            inquiry_file.record_count = 0
            db.session.add(UploadAudit(
                file_id=inquiry_file.file_id,
                upload_user=upload_user,
                department=department,
                engineer_name=inquiry_file.engineer_name or '未提取',
                status='failed',
                note=fail_message
            ))
            db.session.commit()
            return jsonify({'success': False, 'message': '文件解析失败，请检查模板和字段后重试'}), 500

    except Exception as e:
        return api_internal_error('upload_file', e)


def is_record_exists(material_name, specification, supplier, quote_date):
    """
    检查记录是否已存在（入库前去重）

    去重条件：材料名称 + 规格型号 + 供应商 + 报价时间 都相同
    """
    try:
        query = PriceRecord.query.filter(
            PriceRecord.material_name == material_name
        )

        # 规格型号匹配（处理NULL）
        if specification:
            query = query.filter(PriceRecord.specification == specification)
        else:
            query = query.filter(
                db.or_(PriceRecord.specification == None, PriceRecord.specification == '')
            )

        # 供应商匹配（处理NULL）
        if supplier:
            query = query.filter(PriceRecord.supplier == supplier)
        else:
            query = query.filter(
                db.or_(PriceRecord.supplier == None, PriceRecord.supplier == '')
            )

        # 报价时间匹配（处理NULL）
        if quote_date:
            query = query.filter(PriceRecord.quote_date == quote_date)
        else:
            query = query.filter(
                db.or_(PriceRecord.quote_date == None, PriceRecord.quote_date == '')
            )

        return query.first() is not None

    except Exception as e:
        print(f"检查记录重复失败: {e}")
        return False  # 检查失败时不阻止入库


def check_duplicate_inquiry(file_id):
    """检查重复询价 - 显示历史询价工程师"""
    try:
        # 获取刚上传的记录
        new_records = PriceRecord.query.filter_by(file_id=file_id).all()

        if not new_records:
            return {'has_duplicate': False, 'duplicates': []}

        duplicates = []
        one_year_ago = datetime.now().date() - timedelta(days=365)

        for record in new_records:
            # 查询近1年内相似记录
            similar_records = PriceRecord.query.filter(
                PriceRecord.file_id != file_id,
                PriceRecord.material_name == record.material_name,
                PriceRecord.specification == record.specification,
                PriceRecord.quote_date >= one_year_ago
            ).all()

            if similar_records:
                for similar in similar_records:
                    source_file = get_source_file_info(similar.file_id)
                    duplicates.append({
                        'new_material': record.material_name,
                        'new_specification': record.specification,
                        'new_price': record.price,
                        'existing_material': similar.material_name,
                        'existing_specification': similar.specification,
                        'existing_price': similar.price,
                        'existing_quote_date': similar.quote_date.strftime('%Y-%m-%d') if similar.quote_date else None,
                        'existing_file': source_file.file_name if source_file else None,
                        'existing_department': similar.department,
                        'existing_engineer': similar.engineer_name
                    })

        return {
            'has_duplicate': len(duplicates) > 0,
            'duplicates': duplicates[:10],  # 只返回前10条
            'total_count': len(duplicates)
        }

    except Exception as e:
        print(f"检查重复询价失败: {e}")
        return {'has_duplicate': False, 'duplicates': []}


def get_source_file_info(file_id):
    """跨数据库获取文件信息"""
    if file_id is None:
        return None
    try:
        return InquiryFile.query.get(file_id)
    except:
        return None


def increment_reference_count_for_records(record_ids, step=1):
    """为被查询/查看的记录累加引用次数。"""
    if not record_ids:
        return 0

    ids = []
    seen = set()
    for value in record_ids:
        try:
            rid = int(value)
        except (TypeError, ValueError):
            continue
        if rid <= 0 or rid in seen:
            continue
        seen.add(rid)
        ids.append(rid)

    if not ids:
        return 0

    try:
        inc = int(step)
    except (TypeError, ValueError):
        inc = 1
    if inc <= 0:
        inc = 1

    try:
        db.session.query(PriceRecord).filter(
            PriceRecord.record_id.in_(ids)
        ).update(
            {PriceRecord.reference_count: db.func.coalesce(PriceRecord.reference_count, 0) + inc},
            synchronize_session=False
        )
        db.session.commit()
        return len(ids)
    except Exception as e:
        db.session.rollback()
        print(f"[reference_count] 引用计数更新失败: {e}", flush=True)
        return 0


def parse_natural_language_query(query_text):
    """解析自然语言查询 - 使用专业NLP解析器"""
    global nlp_parser

    # 确保解析器已初始化
    if nlp_parser is None:
        init_nlp_parser()

    # 使用新解析器解析
    parsed = nlp_parser.parse(query_text)

    # 调试输出
    print(f"[NLP DEBUG] extract_time_range returned: start={parsed.get('start_date')}, end={parsed.get('end_date')}, display={parsed.get('time_display')}", flush=True)

    # 转换为旧格式兼容
    result = {
        'material_name': parsed.get('material_name'),
        'specification': parsed.get('specification'),
        'region': parsed.get('region'),
        'price': parsed.get('price'),
        'unit': parsed.get('unit'),
        'date': None,
        'material_synonyms': parsed.get('material_candidates', []),
        'parsed_intent': parsed.get('parsed_intent', 'price_inquiry'),
        'expanded_search_terms': parsed.get('material_candidates', []),
        'raw_query': query_text,
        'start_date': parsed.get('start_date'),
        'end_date': parsed.get('end_date'),
        'time_display': parsed.get('time_display'),
        'is_valid': parsed.get('is_valid', True),
        'errors': parsed.get('errors', [])
    }

    return result


@app.route('/api/natural_query', methods=['POST'])
@api_login_required
def natural_language_query():
    """自然语言查询接口"""
    try:
        # 尝试多种方式获取JSON数据
        if request.is_json:
            data = request.get_json()
        else:
            data = request.get_json(force=True, silent=True)

        if not data:
            # 尝试从原始数据解析
            try:
                import json
                data = json.loads(request.data.decode('utf-8'))
            except:
                return jsonify({'success': False, 'message': '无法解析请求数据'}), 400

        query_text = data.get('query', '') if data else ''
        page = max(1, int((data or {}).get('page', 1)))
        page_size_arg = (data or {}).get('page_size', (data or {}).get('pageSize'))
        per_page = int(page_size_arg) if page_size_arg else int((data or {}).get('per_page', 20))
        per_page = min(max(per_page, 1), 5000)
        max_scan = int((data or {}).get('max_scan', 500))
        max_scan = min(max(max_scan, 50), 5000)

        if not query_text:
            return jsonify({'success': False, 'message': '查询文本不能为空'}), 400

        # 解析自然语言查询（规则优化版）
        parsed_params = parse_natural_language_query(query_text)
        parsed_params = enrich_parsed_params(query_text, parsed_params)

        # 调试输出
        print(f"[自然语言查询] 输入: {query_text}", flush=True)
        print(f"[自然语言查询] 解析结果 material_name: {parsed_params.get('material_name')}", flush=True)
        print(f"[自然语言查询] 解析结果 specification: {parsed_params.get('specification')}", flush=True)
        print(f"[自然语言查询] 解析结果 region: {parsed_params.get('region')}", flush=True)
        print(f"[自然语言查询] 解析结果 time_display: {parsed_params.get('time_display')}", flush=True)
        print(f"[自然语言查询] 解析结果 start_date: {parsed_params.get('start_date')}", flush=True)
        print(f"[自然语言查询] 解析结果 parsed_intent: {parsed_params.get('parsed_intent')}", flush=True)

        # 确保 parsed_params 有正确的 intent
        if not parsed_params.get('parsed_intent'):
            parsed_params['parsed_intent'] = 'price_inquiry'

        lookup_subject = extract_lookup_subject(query_text)
        parsed_params['lookup_subject'] = lookup_subject

        # 上一轮自然语言查询上下文（用于“这份/这个/上面”的追问）
        last_context = session.get('last_natural_query_context') or {}
        context_file_ids = []
        if isinstance(last_context.get('file_ids'), list):
            for value in last_context.get('file_ids'):
                try:
                    fid = int(value)
                    if fid > 0:
                        context_file_ids.append(fid)
                except (TypeError, ValueError):
                    continue

        entities = parsed_params.get('entities') or {}
        uploader_filters = [
            normalize_submission_actor_key(item)
            for item in (entities.get('uploader_candidates') or [])
            if normalize_submission_actor_key(item)
        ]
        department_filters = [
            normalize_submission_department_key(item)
            for item in (entities.get('department_candidates') or [])
            if normalize_submission_department_key(item)
        ]
        stats_metric = entities.get('stats_metric') or 'file_count'
        file_keywords = build_file_trace_keywords(entities.get('file_keywords') or [])

        parsed_params['uploader_filters'] = uploader_filters
        parsed_params['department_filters'] = department_filters
        parsed_params['stats_metric'] = stats_metric
        parsed_params['file_keywords'] = file_keywords
        parsed_params['normalized_query_text'] = entities.get('normalized_query_text') or ''
        parsed_params['corrections'] = entities.get('corrections') or []

        submission_followup = (
            is_followup_reference_query(query_text, lookup_subject)
            or any(token in query_text for token in SUBMISSION_FOLLOW_UP_TOKENS)
        )
        if parsed_params.get('parsed_intent') in {'uploader_stats', 'department_stats'} and submission_followup and last_context:
            if not uploader_filters:
                uploader_filters = [
                    normalize_submission_actor_key(item)
                    for item in (last_context.get('uploader_filters') or [])
                    if normalize_submission_actor_key(item)
                ]
            if not department_filters:
                department_filters = [
                    normalize_submission_department_key(item)
                    for item in (last_context.get('department_filters') or [])
                    if normalize_submission_department_key(item)
                ]
            parsed_params['uploader_filters'] = uploader_filters
            parsed_params['department_filters'] = department_filters
            if uploader_filters or department_filters:
                parsed_params['context_inherited'] = True

        if parsed_params.get('parsed_intent') in {'uploader_stats', 'department_stats'}:
            start_date = parsed_params.get('start_date')
            end_date = parsed_params.get('end_date')
            effective_start_date = start_date
            if not effective_start_date and not end_date:
                effective_start_date = datetime.now().date() - timedelta(days=365)

            success_files = filter_success_inquiry_files_for_submission(
                start_date=effective_start_date,
                end_date=end_date,
                uploader_filters=uploader_filters,
                department_filters=department_filters
            )

            context_ids = []
            for source in success_files:
                if source.file_id is None:
                    continue
                context_ids.append(source.file_id)
                if len(context_ids) >= 200:
                    break

            session['last_natural_query_context'] = {
                'query_text': query_text,
                'parsed_intent': parsed_params.get('parsed_intent') or '',
                'material_name': parsed_params.get('material_name') or '',
                'specification': parsed_params.get('specification') or '',
                'region': parsed_params.get('region') or '',
                'lookup_subject': parsed_params.get('lookup_subject') or '',
                'uploader_filters': uploader_filters,
                'department_filters': department_filters,
                'stats_metric': stats_metric,
                'file_keywords': parsed_params.get('file_keywords') or [],
                'file_ids': context_ids,
                'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            session.modified = True

            scope_text = format_submission_time_scope(parsed_params)
            if stats_metric == 'record_count':
                sort_key = 'record_count'
            elif stats_metric == 'uploader_count' and parsed_params.get('parsed_intent') == 'department_stats':
                sort_key = 'uploader_count'
            else:
                sort_key = 'file_count'

            if parsed_params.get('parsed_intent') == 'uploader_stats':
                uploader_map = {}
                for source in success_files:
                    display_name = get_upload_user_display(source.upload_user) or normalize_engineer_name(source.upload_user) or '未知上传人'
                    key = normalize_submission_actor_key(display_name) or normalize_submission_actor_key(source.upload_user) or f"file_{source.file_id}"
                    if key not in uploader_map:
                        uploader_map[key] = {
                            'upload_user': display_name,
                            'department_set': set(),
                            'file_count': 0,
                            'record_count': 0,
                            'latest_upload_time': None,
                        }
                    uploader_map[key]['file_count'] += 1
                    uploader_map[key]['record_count'] += int(source.record_count or 0)
                    if source.department:
                        uploader_map[key]['department_set'].add(source.department)
                    if source.upload_time and (
                        not uploader_map[key]['latest_upload_time']
                        or source.upload_time > uploader_map[key]['latest_upload_time']
                    ):
                        uploader_map[key]['latest_upload_time'] = source.upload_time

                uploader_data = []
                for item in uploader_map.values():
                    uploader_data.append({
                        'upload_user': item['upload_user'],
                        'department': '、'.join(sorted(item['department_set'])) if item['department_set'] else '-',
                        'file_count': item['file_count'],
                        'record_count': item['record_count'],
                        'latest_upload_time': item['latest_upload_time'].strftime('%Y-%m-%d %H:%M:%S') if item['latest_upload_time'] else '-',
                    })

                uploader_data.sort(key=lambda x: (x.get(sort_key) or 0, x.get('latest_upload_time') or ''), reverse=True)
                total = len(uploader_data)
                pages = (total + per_page - 1) // per_page if total > 0 else 1
                start = (page - 1) * per_page
                end = start + per_page
                page_data = uploader_data[start:end] if start < total else []

                total_files = sum(item.get('file_count', 0) for item in uploader_data)
                total_records = sum(item.get('record_count', 0) for item in uploader_data)
                summary = f"{scope_text}共统计到{total}位上传人，累计提交{total_files}份报价文件（{total_records}条记录）。"
                if uploader_filters and total == 1:
                    row = uploader_data[0]
                    metric_value = row.get(sort_key) or 0
                    if sort_key == 'record_count':
                        metric_label = '条报价记录'
                    elif sort_key == 'uploader_count':
                        metric_label = '位上传人'
                    else:
                        metric_label = '份报价文件'
                    summary = f"{row.get('upload_user') or '该上传人'}在{scope_text}提交了{metric_value}{metric_label}（文件{row.get('file_count', 0)}份，记录{row.get('record_count', 0)}条）。"

                return jsonify({
                    'success': True,
                    'uploader_stats_mode': True,
                    'metric': sort_key,
                    'summary': summary,
                    'data': page_data,
                    'total': total,
                    'page': page,
                    'per_page': per_page,
                    'pages': pages,
                    'parsed_params': parsed_params
                })

            department_map = {}
            for source in success_files:
                dept_name = source.department or '未知部门'
                dept_key = normalize_submission_department_key(dept_name) or dept_name
                if dept_key not in department_map:
                    department_map[dept_key] = {
                        'department': dept_name,
                        'file_count': 0,
                        'record_count': 0,
                        'uploader_set': set(),
                        'latest_upload_time': None,
                    }
                department_map[dept_key]['file_count'] += 1
                department_map[dept_key]['record_count'] += int(source.record_count or 0)
                upload_name = get_upload_user_display(source.upload_user) or normalize_engineer_name(source.upload_user) or ''
                if upload_name:
                    department_map[dept_key]['uploader_set'].add(upload_name)
                if source.upload_time and (
                    not department_map[dept_key]['latest_upload_time']
                    or source.upload_time > department_map[dept_key]['latest_upload_time']
                ):
                    department_map[dept_key]['latest_upload_time'] = source.upload_time

            department_data = []
            for item in department_map.values():
                department_data.append({
                    'department': item['department'],
                    'file_count': item['file_count'],
                    'record_count': item['record_count'],
                    'uploader_count': len(item['uploader_set']),
                    'latest_upload_time': item['latest_upload_time'].strftime('%Y-%m-%d %H:%M:%S') if item['latest_upload_time'] else '-',
                })

            department_data.sort(key=lambda x: (x.get(sort_key) or 0, x.get('latest_upload_time') or ''), reverse=True)
            total = len(department_data)
            pages = (total + per_page - 1) // per_page if total > 0 else 1
            start = (page - 1) * per_page
            end = start + per_page
            page_data = department_data[start:end] if start < total else []

            total_files = sum(item.get('file_count', 0) for item in department_data)
            total_records = sum(item.get('record_count', 0) for item in department_data)
            summary = f"{scope_text}共统计到{total}个部门，累计提交{total_files}份报价文件（{total_records}条记录）。"
            if department_filters and total == 1:
                row = department_data[0]
                metric_value = row.get(sort_key) or 0
                if sort_key == 'record_count':
                    metric_label = '条报价记录'
                elif sort_key == 'uploader_count':
                    metric_label = '位上传人'
                else:
                    metric_label = '份报价文件'
                summary = f"{row.get('department') or '该部门'}在{scope_text}提交了{metric_value}{metric_label}（上传人{row.get('uploader_count', 0)}位）。"

            return jsonify({
                'success': True,
                'department_stats_mode': True,
                'metric': sort_key,
                'summary': summary,
                'data': page_data,
                'total': total,
                'page': page,
                'per_page': per_page,
                'pages': pages,
                'parsed_params': parsed_params
            })

        if parsed_params.get('parsed_intent') == 'file_trace':
            compact_query_text = _compact_text(query_text)
            compact_material_name = _compact_text(parsed_params.get('material_name') or '')
            trace_followup = (
                is_followup_reference_query(query_text, lookup_subject)
                or any(token in query_text for token in FOLLOW_UP_REFERENCE_TOKENS)
                or any(token in query_text for token in SUBMISSION_FOLLOW_UP_TOKENS)
            )
            has_identity_filter = bool(uploader_filters or department_filters)
            has_param_filter = bool(parsed_params.get('material_name') or parsed_params.get('specification') or parsed_params.get('region'))

            # 避免将整句“这条记录来自哪个文件”误当作材料名
            if compact_material_name and compact_material_name == compact_query_text and (file_keywords or trace_followup):
                parsed_params['material_name'] = ''
                has_param_filter = bool(parsed_params.get('specification') or parsed_params.get('region'))

            trace_lookup_subject = '' if (trace_followup and not file_keywords) else lookup_subject
            trace_keywords = build_file_trace_keywords(file_keywords, trace_lookup_subject)
            parsed_params['file_keywords'] = trace_keywords

            if trace_followup and last_context:
                if not trace_keywords:
                    trace_keywords = build_file_trace_keywords(
                        last_context.get('file_keywords') or []
                    )
                    parsed_params['file_keywords'] = trace_keywords

                if not uploader_filters:
                    uploader_filters = [
                        normalize_submission_actor_key(item)
                        for item in (last_context.get('uploader_filters') or [])
                        if normalize_submission_actor_key(item)
                    ]
                    parsed_params['uploader_filters'] = uploader_filters

                if not department_filters:
                    department_filters = [
                        normalize_submission_department_key(item)
                        for item in (last_context.get('department_filters') or [])
                        if normalize_submission_department_key(item)
                    ]
                    parsed_params['department_filters'] = department_filters

                if context_file_ids and not parsed_params.get('context_file_ids'):
                    parsed_params['context_file_ids'] = context_file_ids

                if trace_keywords or parsed_params.get('context_file_ids') or uploader_filters or department_filters:
                    parsed_params['context_inherited'] = True

            candidate_file_ids = set()
            for value in (parsed_params.get('context_file_ids') or []):
                try:
                    fid = int(value)
                    if fid > 0:
                        candidate_file_ids.add(fid)
                except (TypeError, ValueError):
                    continue

            if has_param_filter:
                file_id_query = PriceRecord.query

                if parsed_params.get('material_name'):
                    search_terms = [parsed_params['material_name']]
                    if parsed_params.get('material_synonyms'):
                        search_terms.extend(parsed_params['material_synonyms'])
                    or_conditions = [PriceRecord.material_name.like(f'%{term}%') for term in search_terms if term]
                    if or_conditions:
                        file_id_query = file_id_query.filter(db.or_(*or_conditions))

                if parsed_params.get('specification'):
                    file_id_query = file_id_query.filter(PriceRecord.specification.like(f"%{parsed_params['specification']}%"))

                if parsed_params.get('region'):
                    region_terms = [parsed_params['region']]
                    if parsed_params.get('expanded_search_terms'):
                        region_terms.extend([item for item in parsed_params['expanded_search_terms'] if item])
                    region_or_conditions = [PriceRecord.region.like(f'%{term}%') for term in region_terms if term]
                    if region_or_conditions:
                        file_id_query = file_id_query.filter(db.or_(*region_or_conditions))

                start_date = parsed_params.get('start_date')
                end_date = parsed_params.get('end_date')
                if start_date:
                    file_id_query = file_id_query.filter(PriceRecord.quote_date >= start_date)
                if end_date:
                    file_id_query = file_id_query.filter(PriceRecord.quote_date <= end_date)
                if not start_date and not end_date:
                    one_year_ago = datetime.now().date() - timedelta(days=365)
                    file_id_query = file_id_query.filter(PriceRecord.quote_date >= one_year_ago)

                matched_records = rank_records(file_id_query.limit(max_scan).all(), parsed_params)
                for item in matched_records:
                    if item.file_id is not None:
                        candidate_file_ids.add(item.file_id)

            if not (has_param_filter or trace_keywords or candidate_file_ids or has_identity_filter):
                return jsonify({'success': False, 'message': '请补充材料名、规格或附件关键词后再追溯来源文件'}), 400

            start_date = parsed_params.get('start_date')
            end_date = parsed_params.get('end_date')
            effective_start_date = start_date
            if not effective_start_date and not end_date:
                effective_start_date = datetime.now().date() - timedelta(days=365)

            success_files = filter_success_inquiry_files_for_submission(
                start_date=effective_start_date,
                end_date=end_date,
                uploader_filters=uploader_filters,
                department_filters=department_filters
            )

            if candidate_file_ids:
                success_files = [item for item in success_files if item.file_id in candidate_file_ids]

            if trace_keywords:
                success_files = [item for item in success_files if inquiry_file_matches_keywords(item, trace_keywords)]

            trace_rows = []
            context_ids = []
            for source_file in success_files:
                uploader_user = get_user_by_upload_user(source_file.upload_user)
                upload_display = get_upload_user_display(source_file.upload_user) or normalize_engineer_name(source_file.upload_user) or '未知上传人'
                record_count = int(source_file.record_count or 0)
                if record_count <= 0 and source_file.file_id is not None:
                    record_count = PriceRecord.query.filter_by(file_id=source_file.file_id).count()

                trace_rows.append({
                    'file_id': source_file.file_id,
                    'file_name': source_file.file_name or '-',
                    'upload_user': upload_display,
                    'department': source_file.department or '未知',
                    'engineer_name': source_file.engineer_name or '未知',
                    'upload_time': source_file.upload_time.strftime('%Y-%m-%d %H:%M:%S') if source_file.upload_time else '-',
                    'record_count': record_count,
                    'is_bound': bool(uploader_user),
                    'phone_masked': mask_phone(uploader_user.phone) if uploader_user else '',
                    'uploader_user_id': uploader_user.id if uploader_user else None,
                })

                if source_file.file_id is not None:
                    context_ids.append(source_file.file_id)

            trace_rows.sort(key=lambda x: ((x.get('upload_time') or ''), int(x.get('record_count') or 0)), reverse=True)
            total = len(trace_rows)
            pages = (total + per_page - 1) // per_page if total > 0 else 1
            start = (page - 1) * per_page
            end = start + per_page
            page_data = trace_rows[start:end] if start < total else []

            scope_text = format_submission_time_scope(parsed_params)
            total_records = sum(int(item.get('record_count') or 0) for item in trace_rows)
            if total == 0:
                summary = f"{scope_text}未命中来源文件，请补充附件关键词或更具体的筛选条件。"
            elif total == 1:
                row = trace_rows[0]
                summary = f"{scope_text}定位到1份来源文件：{row.get('file_name') or '-'}，由{row.get('upload_user') or '未知上传人'}上传（{row.get('record_count', 0)}条记录）。"
            else:
                summary = f"{scope_text}定位到{total}份来源文件，共{total_records}条报价记录。"

            session['last_natural_query_context'] = {
                'query_text': query_text,
                'parsed_intent': parsed_params.get('parsed_intent') or '',
                'material_name': parsed_params.get('material_name') or '',
                'specification': parsed_params.get('specification') or '',
                'region': parsed_params.get('region') or '',
                'lookup_subject': parsed_params.get('lookup_subject') or '',
                'uploader_filters': uploader_filters,
                'department_filters': department_filters,
                'stats_metric': stats_metric,
                'file_keywords': trace_keywords,
                'file_ids': context_ids[:200],
                'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            session.modified = True

            return jsonify({
                'success': True,
                'file_trace_mode': True,
                'summary': summary,
                'data': page_data,
                'total': total,
                'page': page,
                'per_page': per_page,
                'pages': pages,
                'parsed_params': parsed_params
            })

        if not parsed_params.get('material_name'):
            # 对价格查询允许降级；工程师/上传人查询不直接回退整句，避免把“谁上传了...”当材料名
            if parsed_params.get('parsed_intent') not in {'engineer_lookup', 'uploader_lookup', 'uploader_stats', 'department_stats', 'file_trace'}:
                parsed_params['material_name'] = query_text
            else:
                parsed_params['material_name'] = ''

        # Avoid treating short follow-up questions as material names.
        compact_query_text = _compact_text(query_text)
        compact_material_name = _compact_text(parsed_params.get('material_name') or '')
        if (
            parsed_params.get('parsed_intent') == 'engineer_lookup'
            and compact_material_name
            and compact_material_name == compact_query_text
            and is_engineer_followup_query(query_text)
        ):
            parsed_params['material_name'] = ''

        # 避免将“成唐提交的报价”整句误判为材料名，优先按上传人/部门语义处理
        if (
            parsed_params.get('parsed_intent') == 'uploader_lookup'
            and compact_material_name
            and compact_material_name == compact_query_text
            and (uploader_filters or department_filters or entities.get('has_submission_action'))
        ):
            parsed_params['material_name'] = ''

        # 上传人追问承接：如“这份报价谁上传的”自动继承上一轮查询条件/文件范围
        if parsed_params.get('parsed_intent') == 'uploader_lookup':
            is_followup = is_followup_reference_query(query_text, lookup_subject)
            missing_filters = not (parsed_params.get('material_name') or parsed_params.get('specification') or parsed_params.get('region') or len(lookup_subject) >= 2)
            if is_followup and missing_filters and last_context:
                parsed_params['material_name'] = parsed_params.get('material_name') or (last_context.get('material_name') or '')
                parsed_params['specification'] = parsed_params.get('specification') or (last_context.get('specification') or '')
                parsed_params['region'] = parsed_params.get('region') or (last_context.get('region') or '')
                if not lookup_subject:
                    lookup_subject = last_context.get('lookup_subject') or ''
                    parsed_params['lookup_subject'] = lookup_subject
                if context_file_ids:
                    parsed_params['context_file_ids'] = context_file_ids

                if not uploader_filters:
                    uploader_filters = [
                        normalize_submission_actor_key(item)
                        for item in (last_context.get('uploader_filters') or [])
                        if normalize_submission_actor_key(item)
                    ]
                if not department_filters:
                    department_filters = [
                        normalize_submission_department_key(item)
                        for item in (last_context.get('department_filters') or [])
                        if normalize_submission_department_key(item)
                    ]
                parsed_params['uploader_filters'] = uploader_filters
                parsed_params['department_filters'] = department_filters

                parsed_params['context_inherited'] = True
                print(f"[自然语言查询] uploader_lookup 使用上下文承接: files={len(context_file_ids)}", flush=True)

        # 工程师追问承接：如“这份资料是谁负责的”自动继承上一轮查询条件/文件范围
        # 仅在缺少筛选条件时承接，避免覆盖用户新输入
        if parsed_params.get('parsed_intent') == 'engineer_lookup':
            is_followup = is_followup_reference_query(query_text, lookup_subject) or is_engineer_followup_query(query_text)
            missing_filters = not (parsed_params.get('material_name') or parsed_params.get('specification') or parsed_params.get('region'))
            if is_followup and missing_filters and last_context:
                parsed_params['material_name'] = parsed_params.get('material_name') or (last_context.get('material_name') or '')
                parsed_params['specification'] = parsed_params.get('specification') or (last_context.get('specification') or '')
                parsed_params['region'] = parsed_params.get('region') or (last_context.get('region') or '')
                if not lookup_subject:
                    lookup_subject = last_context.get('lookup_subject') or ''
                    parsed_params['lookup_subject'] = lookup_subject
                if context_file_ids:
                    parsed_params['context_file_ids'] = context_file_ids
                parsed_params['context_inherited'] = True
                print(f"[natural_query] engineer_lookup context inherited: files={len(context_file_ids)}", flush=True)

            has_context = bool(parsed_params.get('context_file_ids'))
            if not (parsed_params.get('material_name') or parsed_params.get('specification') or parsed_params.get('region') or has_context):
                return jsonify({'success': False, 'message': '请补充材料名称或规格后再查询负责人'}), 400

        if parsed_params.get('parsed_intent') == 'uploader_lookup':
            has_context = bool(parsed_params.get('context_file_ids'))
            has_identity_filter = bool(uploader_filters or department_filters)
            if not (parsed_params.get('material_name') or parsed_params.get('specification') or parsed_params.get('region') or len(lookup_subject) >= 2 or has_context or has_identity_filter):
                return jsonify({'success': False, 'message': '请补充材料名、规格或文件关键词后再查询上传人'}), 400

            # 仅按“上传人/部门”查询时，直接基于文件维度返回结果
            has_param_filter = bool(parsed_params.get('material_name') or parsed_params.get('specification') or parsed_params.get('region'))
            if has_identity_filter and not has_param_filter:
                start_date = parsed_params.get('start_date')
                end_date = parsed_params.get('end_date')
                effective_start_date = start_date
                if not effective_start_date and not end_date:
                    effective_start_date = datetime.now().date() - timedelta(days=365)

                success_files = filter_success_inquiry_files_for_submission(
                    start_date=effective_start_date,
                    end_date=end_date,
                    uploader_filters=uploader_filters,
                    department_filters=department_filters
                )

                uploader_data = []
                context_ids = []
                for source_file in success_files:
                    uploader_user = get_user_by_upload_user(source_file.upload_user)
                    uploader_data.append({
                        'file_id': source_file.file_id,
                        'file_name': source_file.file_name or '-',
                        'upload_user': get_upload_user_display(source_file.upload_user) or '未知',
                        'upload_time': source_file.upload_time.strftime('%Y-%m-%d %H:%M:%S') if source_file.upload_time else None,
                        'department': source_file.department or '未知',
                        'engineer_name': source_file.engineer_name or '未知',
                        'uploader_user_id': uploader_user.id if uploader_user else None,
                        'phone_masked': mask_phone(uploader_user.phone) if uploader_user else '',
                        'is_bound': bool(uploader_user),
                        'record_count': int(source_file.record_count or 0),
                        'latest_quote': None,
                    })
                    if source_file.file_id is not None:
                        context_ids.append(source_file.file_id)

                total = len(uploader_data)
                pages = (total + per_page - 1) // per_page if total > 0 else 1
                start = (page - 1) * per_page
                end = start + per_page
                page_data = uploader_data[start:end] if start < total else []

                session['last_natural_query_context'] = {
                    'query_text': query_text,
                    'parsed_intent': parsed_params.get('parsed_intent') or '',
                    'material_name': parsed_params.get('material_name') or '',
                    'specification': parsed_params.get('specification') or '',
                    'region': parsed_params.get('region') or '',
                    'lookup_subject': parsed_params.get('lookup_subject') or '',
                    'uploader_filters': uploader_filters,
                    'department_filters': department_filters,
                    'stats_metric': stats_metric,
                    'file_keywords': parsed_params.get('file_keywords') or [],
                    'file_ids': context_ids[:200],
                    'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                session.modified = True

                summary = None
                if has_identity_filter:
                    scope_text = format_submission_time_scope(parsed_params)
                    total_records = sum(int(item.get('record_count') or 0) for item in uploader_data)
                    summary = f"{scope_text}命中{total}份报价文件（{total_records}条记录）。"

                return jsonify({
                    'success': True,
                    'uploader_mode': True,
                    'summary': summary,
                    'data': page_data,
                    'total': total,
                    'page': page,
                    'per_page': per_page,
                    'pages': pages,
                    'parsed_params': parsed_params
                })

        # 构建数据库查询
        query = PriceRecord.query

        # 判断是否有解析到参数
        has_params = parsed_params['material_name'] or parsed_params['specification'] or parsed_params['region']
        active_context_file_ids = parsed_params.get('context_file_ids') or context_file_ids

        if not has_params:
            if parsed_params.get('parsed_intent') in {'uploader_lookup', 'engineer_lookup'} and active_context_file_ids:
                query = query.filter(PriceRecord.file_id.in_(active_context_file_ids))
                print(f"[自然语言查询] uploader_lookup 使用上下文 file_id 过滤: {len(active_context_file_ids)}", flush=True)
            else:
                # 如果没有解析到参数，使用主体词（去噪）做模糊搜索
                search_text = (lookup_subject or query_text).strip()
                or_conditions = [
                    PriceRecord.material_name.like(f'%{search_text}%'),
                    PriceRecord.specification.like(f'%{search_text}%'),
                    PriceRecord.supplier.like(f'%{search_text}%'),
                    PriceRecord.remark.like(f'%{search_text}%')
                ]

                if parsed_params.get('parsed_intent') == 'uploader_lookup' and search_text:
                    file_ids = [
                        row.file_id for row in InquiryFile.query.filter(
                            InquiryFile.file_name.like(f'%{search_text}%')
                        ).limit(200).all() if row.file_id is not None
                    ]
                    if file_ids:
                        or_conditions.append(PriceRecord.file_id.in_(file_ids))

                query = query.filter(db.or_(*or_conditions))
                print(f"[自然语言查询] 未解析到参数，使用主体词模糊搜索: {search_text}", flush=True)
        else:
            # 应用解析的参数
            if parsed_params['material_name']:
                # 使用近义词进行搜索
                search_terms = [parsed_params['material_name']]
                if parsed_params['material_synonyms']:
                    search_terms.extend(parsed_params['material_synonyms'])

                # 构建OR查询
                or_conditions = []
                for term in search_terms:
                    or_conditions.append(PriceRecord.material_name.like(f'%{term}%'))

                if or_conditions:
                    query = query.filter(db.or_(*or_conditions))

            if parsed_params['specification']:
                query = query.filter(PriceRecord.specification.like(f'%{parsed_params["specification"]}%'))

            if parsed_params['region']:
                # 使用地区近义词进行搜索
                search_terms = [parsed_params['region']]
                if parsed_params.get('expanded_search_terms'):
                    # 提取地区相关的近义词
                    region_synonyms = [term for term in parsed_params['expanded_search_terms'] if '市' in term or '省' in term or '区' in term]
                    if region_synonyms:
                        search_terms.extend(region_synonyms)

                # 构建OR查询
                or_conditions = []
                for term in search_terms:
                    or_conditions.append(PriceRecord.region.like(f'%{term}%'))

                if or_conditions:
                    query = query.filter(db.or_(*or_conditions))

            if parsed_params.get('parsed_intent') in {'uploader_lookup', 'engineer_lookup'} and active_context_file_ids:
                query = query.filter(PriceRecord.file_id.in_(active_context_file_ids))

        # 使用解析出的时间范围（如果有）
        start_date = parsed_params.get('start_date')
        end_date = parsed_params.get('end_date')

        if start_date:
            query = query.filter(PriceRecord.quote_date >= start_date)
        if end_date:
            query = query.filter(PriceRecord.quote_date <= end_date)

        # 如果没有时间范围，默认查询近1年
        if not start_date and not end_date:
            one_year_ago = datetime.now().date() - timedelta(days=365)
            query = query.filter(PriceRecord.quote_date >= one_year_ago)

        # 限制单次扫描上限，避免自然语言查询拉全表
        all_records = query.limit(max_scan).all()

        # 语义排序（同义词 + 词项命中 + 严格词组）
        if has_params:
            sorted_records = rank_records(all_records, parsed_params)
        else:
            sorted_records = sorted(all_records, key=lambda x: x.quote_date or datetime.min.date(), reverse=True)

        # 构建结果
        results = []
        for record in sorted_records:
            result = record.to_dict()
            # 添加来源追溯信息 - 跨数据库查询
            source_file = get_source_file_info(record.file_id)
            if source_file:
                result['source_file_name'] = source_file.file_name
                result['source_upload_time'] = source_file.upload_time.strftime('%Y-%m-%d %H:%M:%S') if source_file.upload_time else None
                result['source_upload_user'] = get_upload_user_display(source_file.upload_user) or ''
                result['source_department'] = source_file.department
                result['source_engineer'] = source_file.engineer_name

            # 工程师联系方式默认仅返回脱敏信息
            bound_user = None
            if record.engineer_user_id:
                bound_user = User.query.get(record.engineer_user_id)
            if not bound_user:
                bound_user = get_user_by_engineer_name(record.engineer_name)

            if bound_user:
                result['engineer_user_id'] = bound_user.id
                result['engineer_phone_masked'] = mask_phone(bound_user.phone)
                result['engineer_contact_available'] = True
            else:
                result['engineer_user_id'] = None
                result['engineer_phone_masked'] = ''
                result['engineer_contact_available'] = False
            results.append(result)

        # 保存自然语言查询上下文，支持“这份/这个/上面”的追问
        context_ids = []
        seen_ids = set()
        for item in results:
            file_id = item.get('file_id')
            if isinstance(file_id, int) and file_id > 0 and file_id not in seen_ids:
                context_ids.append(file_id)
                seen_ids.add(file_id)
            if len(context_ids) >= 200:
                break

        session['last_natural_query_context'] = {
            'query_text': query_text,
            'parsed_intent': parsed_params.get('parsed_intent') or '',
            'material_name': parsed_params.get('material_name') or '',
            'specification': parsed_params.get('specification') or '',
            'region': parsed_params.get('region') or '',
            'lookup_subject': parsed_params.get('lookup_subject') or '',
            'uploader_filters': uploader_filters,
            'department_filters': department_filters,
            'stats_metric': stats_metric,
            'file_keywords': parsed_params.get('file_keywords') or [],
            'file_ids': context_ids,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        session.modified = True

        # 工程师查询模式（模板匹配，不接入AI）
        if parsed_params.get('parsed_intent') == 'engineer_lookup':
            engineer_map = {}
            for item in results:
                key = item.get('engineer_user_id') or item.get('engineer_name') or 'unknown'
                if key not in engineer_map:
                    engineer_map[key] = {
                        'engineer_name': item.get('engineer_name') or '未知',
                        'department': item.get('department') or item.get('source_department') or '未知',
                        'engineer_user_id': item.get('engineer_user_id'),
                        'phone_masked': item.get('engineer_phone_masked') or '',
                        'is_bound': bool(item.get('engineer_user_id')),
                        'material_count': 0,
                        'latest_quote': item.get('quote_date')
                    }
                engineer_map[key]['material_count'] += 1
                if item.get('quote_date') and (not engineer_map[key]['latest_quote'] or item.get('quote_date') > engineer_map[key]['latest_quote']):
                    engineer_map[key]['latest_quote'] = item.get('quote_date')

            engineer_data = list(engineer_map.values())
            engineer_data.sort(key=lambda x: (x.get('material_count') or 0), reverse=True)
            total = len(engineer_data)
            pages = (total + per_page - 1) // per_page if total > 0 else 1
            start = (page - 1) * per_page
            end = start + per_page
            page_data = engineer_data[start:end] if start < total else []
            return jsonify({
                'success': True,
                'engineer_mode': True,
                'data': page_data,
                'total': total,
                'page': page,
                'per_page': per_page,
                'pages': pages,
                'parsed_params': parsed_params
            })

        # 上传人查询模式（按来源文件聚合）
        if parsed_params.get('parsed_intent') == 'uploader_lookup':
            uploader_map = {}
            for item in results:
                file_id = item.get('file_id')
                source_file = get_source_file_info(file_id)
                if not source_file:
                    continue

                key = source_file.file_id
                if key not in uploader_map:
                    uploader_user = get_user_by_upload_user(source_file.upload_user)
                    uploader_map[key] = {
                        'file_id': source_file.file_id,
                        'file_name': source_file.file_name or '-',
                        'upload_user': get_upload_user_display(source_file.upload_user) or '未知',
                        'upload_time': source_file.upload_time.strftime('%Y-%m-%d %H:%M:%S') if source_file.upload_time else None,
                        'department': source_file.department or '未知',
                        'engineer_name': source_file.engineer_name or '未知',
                        'uploader_user_id': uploader_user.id if uploader_user else None,
                        'phone_masked': mask_phone(uploader_user.phone) if uploader_user else '',
                        'is_bound': bool(uploader_user),
                        'record_count': 0,
                        'latest_quote': item.get('quote_date')
                    }

                uploader_map[key]['record_count'] += 1
                if item.get('quote_date') and (not uploader_map[key]['latest_quote'] or item.get('quote_date') > uploader_map[key]['latest_quote']):
                    uploader_map[key]['latest_quote'] = item.get('quote_date')

            uploader_data = list(uploader_map.values())
            uploader_data.sort(key=lambda x: ((x.get('record_count') or 0), (x.get('upload_time') or '')), reverse=True)
            total = len(uploader_data)
            pages = (total + per_page - 1) // per_page if total > 0 else 1
            start = (page - 1) * per_page
            end = start + per_page
            page_data = uploader_data[start:end] if start < total else []
            return jsonify({
                'success': True,
                'uploader_mode': True,
                'data': page_data,
                'total': total,
                'page': page,
                'per_page': per_page,
                'pages': pages,
                'parsed_params': parsed_params
            })

        # 比价分析（如果查询意图是比价）
        comparison_data = None
        if parsed_params['parsed_intent'] == 'comparison':
            comparison_data = analyze_price_comparison(parsed_params, results)

        # 趋势分析（如果查询意图是趋势）
        trend_data = None
        if parsed_params['parsed_intent'] == 'trend':
            trend_data = analyze_price_trend(parsed_params, results)

        # 分页
        total = len(results)
        pages = (total + per_page - 1) // per_page if total > 0 else 1
        start = (page - 1) * per_page
        end = start + per_page
        page_data = results[start:end] if start < total else []

        # 自然语言价格列表结果按当前页累计引用次数
        increment_reference_count_for_records([item.get('record_id') for item in page_data])

        return jsonify({
            'success': True,
            'data': page_data,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': pages,
            'parsed_params': parsed_params,
            'comparison_data': comparison_data,
            'trend_data': trend_data
        })

    except Exception as e:
        return api_internal_error('natural_language_query', e)


def analyze_price_comparison(parsed_params, records):
    """分析价格比较"""
    if not records or len(records) < 2:
        return None

    # 按供应商分组
    suppliers = {}
    for record in records:
        supplier = record.get('supplier', '未知供应商')
        if supplier not in suppliers:
            suppliers[supplier] = []
        suppliers[supplier].append(record)

    # 计算各供应商的平均价格
    comparison_result = {
        'suppliers': [],
        'avg_prices': {},
        'price_ranges': {},
        'recommendation': None
    }

    for supplier, supplier_records in suppliers.items():
        prices = [r['price'] for r in supplier_records if r['price']]
        if prices:
            avg_price = sum(prices) / len(prices)
            min_price = min(prices)
            max_price = max(prices)
            comparison_result['suppliers'].append(supplier)
            comparison_result['avg_prices'][supplier] = avg_price
            comparison_result['price_ranges'][supplier] = (min_price, max_price)

    # 找出最便宜的供应商
    if comparison_result['avg_prices']:
        cheapest_supplier = min(comparison_result['avg_prices'], key=comparison_result['avg_prices'].get)
        comparison_result['recommendation'] = f"推荐选择 {cheapest_supplier}，平均价格最低"

    return comparison_result


def analyze_price_trend(parsed_params, records):
    """分析价格趋势"""
    if not records or len(records) < 3:
        return None

    # 按时间排序
    records_sorted = sorted(records, key=lambda x: x['quote_date'] or '')

    # 计算价格变化趋势
    prices = [r['price'] for r in records_sorted if r['price']]
    dates = [r['quote_date'] for r in records_sorted if r['quote_date']]

    if len(prices) < 3:
        return None

    # 计算价格变化率
    price_changes = []
    for i in range(1, len(prices)):
        change = ((prices[i] - prices[i-1]) / prices[i-1]) * 100
        price_changes.append(change)

    # 计算平均变化率
    avg_change = sum(price_changes) / len(price_changes)

    # 判断趋势
    trend = "稳定"
    if avg_change > 5:
        trend = "上涨"
    elif avg_change < -5:
        trend = "下跌"

    trend_result = {
        'trend': trend,
        'avg_change_percent': avg_change,
        'latest_price': prices[-1],
        'earliest_price': prices[0],
        'price_change': prices[-1] - prices[0],
        'analysis': f"价格呈现{trend}趋势，平均变化率为{avg_change:.2f}%"
    }

    return trend_result


@app.route('/api/query', methods=['GET'])
@api_login_required
def query_records():
    """
    历史询价查询
    支持按材料名称、规格型号、地区、时间范围查询
    """
    try:
        # 获取查询参数
        material_name = request.args.get('material_name', '')
        specification = request.args.get('specification', '')
        region = request.args.get('region', '')
        time_range = request.args.get('time_range', '')
        start_date = request.args.get('start_date', '')
        end_date = request.args.get('end_date', '')
        page = max(1, int(request.args.get('page', 1)))
        page_size_arg = request.args.get('page_size', request.args.get('pageSize', None))
        per_page = int(page_size_arg) if page_size_arg else int(request.args.get('per_page', 20))
        per_page = min(max(per_page, 1), 100)

        # 构建查询
        query = PriceRecord.query

        if material_name:
            query = query.filter(PriceRecord.material_name.like(f'%{material_name}%'))

        if specification:
            query = apply_specification_partial_filter(query, specification)

        if region:
            query = query.filter(PriceRecord.region.like(f'%{region}%'))

        # 处理时间范围
        if time_range and time_range not in ['custom', 'all']:
            # 根据预设时间范围计算开始日期
            today = datetime.now().date()
            if time_range == 'year':
                start_date = (today - timedelta(days=365)).strftime('%Y-%m-%d')
            elif time_range == '6months':
                start_date = (today - timedelta(days=180)).strftime('%Y-%m-%d')
            elif time_range == '3months':
                start_date = (today - timedelta(days=90)).strftime('%Y-%m-%d')
            end_date = today.strftime('%Y-%m-%d')

        # 应用日期过滤
        if time_range != 'all':
            if not start_date and not end_date:
                # 默认查询近1年
                one_year_ago = datetime.now().date() - timedelta(days=365)
                query = query.filter(PriceRecord.quote_date >= one_year_ago)
            else:
                if start_date:
                    query = query.filter(PriceRecord.quote_date >= datetime.strptime(start_date, '%Y-%m-%d').date())
                if end_date:
                    query = query.filter(PriceRecord.quote_date <= datetime.strptime(end_date, '%Y-%m-%d').date())

        # 排序和分页
        query = query.order_by(PriceRecord.quote_date.desc())
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # 构建结果
        results = []
        for record in pagination.items:
            result = record.to_dict()
            # 添加来源追溯信息 - 跨数据库查询
            source_file = get_source_file_info(record.file_id)
            if source_file:
                result['source_file_name'] = source_file.file_name
                result['source_upload_time'] = source_file.upload_time.strftime('%Y-%m-%d %H:%M:%S') if source_file.upload_time else None
                result['source_upload_user'] = get_upload_user_display(source_file.upload_user) or ''
                result['source_department'] = source_file.department
                result['source_engineer'] = source_file.engineer_name
            results.append(result)

        # 当前页结果视为一次引用
        increment_reference_count_for_records([item.record_id for item in pagination.items])

        return jsonify({
            'success': True,
            'data': results,
            'total': pagination.total,
            'page': page,
            'per_page': per_page,
            'pages': pagination.pages
        })

    except Exception as e:
        return api_internal_error('query_records', e)


@app.route('/api/statistics', methods=['GET'])
@api_login_required
def get_statistics():
    """
    上传统计
    按部门、工程师统计上传次数与最近上传时间
    注意：工程师统计已去重（相同材料名+规格+供应商+报价日期只算一条）
    """
    try:
        # 按部门统计（从文件表）
        dept_stats = db.session.query(
            InquiryFile.department,
            db.func.count(InquiryFile.file_id).label('upload_count'),
            db.func.max(InquiryFile.upload_time).label('last_upload_time')
        ).filter(
            InquiryFile.parse_status == 'success'
        ).group_by(InquiryFile.department).all()

        # SQL聚合替代 Python .all() 分组，避免大数据量性能问题
        engineer_unique_sub = db.session.query(
            PriceRecord.engineer_name.label('engineer_name'),
            PriceRecord.department.label('department'),
            PriceRecord.material_name.label('material_name'),
            PriceRecord.specification.label('specification'),
            PriceRecord.supplier.label('supplier'),
            PriceRecord.quote_date.label('quote_date')
        ).filter(
            PriceRecord.engineer_name != None,
            PriceRecord.engineer_name != ''
        ).distinct().subquery()

        engineer_stats_rows = db.session.query(
            engineer_unique_sub.c.engineer_name,
            engineer_unique_sub.c.department,
            db.func.count().label('record_count'),
            db.func.max(engineer_unique_sub.c.quote_date).label('latest_quote')
        ).group_by(
            engineer_unique_sub.c.engineer_name,
            engineer_unique_sub.c.department
        ).order_by(
            db.func.count().desc()
        ).all()

        engineer_stats = [
            {
                'engineer_name': row.engineer_name,
                'department': row.department,
                'record_count': row.record_count,
                'latest_quote': row.latest_quote
            }
            for row in engineer_stats_rows
        ]

        material_unique_sub = db.session.query(
            PriceRecord.material_name.label('material_name'),
            PriceRecord.specification.label('specification'),
            PriceRecord.supplier.label('supplier'),
            PriceRecord.quote_date.label('quote_date'),
            PriceRecord.reference_count.label('reference_count')
        ).distinct().subquery()

        material_stats_rows = db.session.query(
            material_unique_sub.c.material_name,
            db.func.count().label('record_count'),
            db.func.coalesce(db.func.sum(material_unique_sub.c.reference_count), 0).label('total_references'),
            db.func.max(material_unique_sub.c.quote_date).label('latest_quote')
        ).group_by(
            material_unique_sub.c.material_name
        ).order_by(
            db.func.coalesce(db.func.sum(material_unique_sub.c.reference_count), 0).desc()
        ).limit(10).all()

        material_stats = [
            {
                'material_name': row.material_name,
                'record_count': row.record_count,
                'total_references': row.total_references,
                'latest_quote': row.latest_quote,
                'reference_per_record': (row.total_references / row.record_count) if row.record_count else 0
            }
            for row in material_stats_rows
        ]

        # 总体统计（去重后）
        total_unique_records = db.session.query(
            PriceRecord.material_name,
            PriceRecord.specification,
            PriceRecord.supplier,
            PriceRecord.quote_date
        ).distinct().count()

        total_files = InquiryFile.query.filter_by(parse_status='success').count()
        total_references = db.session.query(db.func.sum(PriceRecord.reference_count)).scalar() or 0

        return jsonify({
            'success': True,
            'data': {
                'total_files': total_files,
                'total_records': total_unique_records,
                'total_references': total_references,
                'by_department': [
                    {
                        'department': stat.department or '未知',
                        'upload_count': stat.upload_count,
                        'last_upload_time': stat.last_upload_time.strftime('%Y-%m-%d %H:%M:%S') if stat.last_upload_time else None
                    }
                    for stat in dept_stats
                ],
                'by_engineer': [
                    {
                        'engineer_name': item['engineer_name'] or '未知',
                        'department': item['department'] or '未知',
                        'upload_count': item['record_count'],
                        'last_upload_time': item['latest_quote'].strftime('%Y-%m-%d') if item['latest_quote'] else None
                    }
                    for item in engineer_stats
                ],
                'high_value_materials': [
                    {
                        'material_name': item['material_name'],
                        'record_count': item['record_count'],
                        'total_references': item['total_references'],
                        'latest_quote': item['latest_quote'].strftime('%Y-%m-%d') if item['latest_quote'] else None,
                        'reference_per_record': item['reference_per_record']
                    }
                    for item in material_stats
                ]
            }
        })

    except Exception as e:
        return api_internal_error('get_statistics', e)


@app.route('/api/files', methods=['GET'])
@api_login_required
def list_files():
    """
    文件列表
    """
    try:
        page = max(1, int(request.args.get('page', 1)))
        page_size_arg = request.args.get('page_size', request.args.get('pageSize', None))
        per_page = int(page_size_arg) if page_size_arg else int(request.args.get('per_page', 20))
        per_page = min(max(per_page, 1), 100)

        pagination = InquiryFile.query.filter(
            InquiryFile.parse_status == 'success'
        ).order_by(InquiryFile.upload_time.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        file_rows = []
        for item in pagination.items:
            row = item.to_dict()
            row['upload_user'] = get_upload_user_display(item.upload_user) or row.get('upload_user') or '未知'
            file_rows.append(row)

        return jsonify({
            'success': True,
            'data': file_rows,
            'total': pagination.total,
            'page': page,
            'per_page': per_page,
            'pages': pagination.pages
        })

    except Exception as e:
        return api_internal_error('list_files', e)


@app.route('/api/records/<int:record_id>', methods=['GET'])
@api_login_required
def get_record_detail(record_id):
    """
    获取单条记录详情
    """
    try:
        record = PriceRecord.query.get(record_id)
        if not record:
            return jsonify({'success': False, 'message': '记录不存在'}), 404

        record.reference_count = int(record.reference_count or 0) + 1
        db.session.commit()
        db.session.refresh(record)

        result = record.to_dict()

        # 添加完整的来源追溯信息 - 跨数据库查询
        source_file = get_source_file_info(record.file_id)
        if source_file:
            result['source_file_name'] = source_file.file_name
            result['source_upload_time'] = source_file.upload_time.strftime('%Y-%m-%d %H:%M:%S') if source_file.upload_time else None
            result['source_upload_user'] = get_upload_user_display(source_file.upload_user) or ''
            result['source_department'] = source_file.department
            result['source_engineer'] = source_file.engineer_name

        bound_user = None
        if record.engineer_user_id:
            bound_user = User.query.get(record.engineer_user_id)
        if not bound_user:
            bound_user = get_user_by_engineer_name(record.engineer_name)
        result['engineer_contact_available'] = bool(bound_user)
        result['engineer_phone_masked'] = mask_phone(bound_user.phone) if bound_user else ''

        return jsonify({
            'success': True,
            'data': result
        })

    except Exception as e:
        return api_internal_error('get_record_detail', e)


@app.route('/api/download_template')
@api_login_required
def download_template():
    """下载Excel模板"""
    template_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'sample_data', '询价模板_示例数据.xlsx')
    if os.path.exists(template_path):
        return send_from_directory(os.path.dirname(template_path), '询价模板_示例数据.xlsx', as_attachment=True)
    else:
        return jsonify({'success': False, 'message': '模板文件不存在'}), 404


@app.route('/api/download/<int:file_id>')
@api_login_required
def download_file(file_id):
    """下载原始上传文件"""
    try:
        inquiry_file = InquiryFile.query.get(file_id)
        if not inquiry_file:
            return jsonify({'success': False, 'message': '文件不存在'}), 404

        filepath = resolve_uploaded_file_path(inquiry_file)
        if filepath:
            upload_dir = os.path.dirname(filepath)
            stored_name = os.path.basename(filepath)
            return send_from_directory(upload_dir, stored_name, as_attachment=True, download_name=inquiry_file.file_name)

        return jsonify({'success': False, 'message': '文件未找到'}), 404

    except Exception as e:
        return api_internal_error('download_file', e)


@app.route('/api/preview/<int:file_id>')
@api_login_required
def preview_file(file_id):
    """预览Excel/CSV文件内容"""
    try:
        inquiry_file = InquiryFile.query.get(file_id)
        if not inquiry_file:
            return jsonify({'success': False, 'message': '文件不存在'}), 404

        filepath = resolve_uploaded_file_path(inquiry_file)

        if not filepath:
            return jsonify({'success': False, 'message': '文件未找到'}), 404

        # 根据文件类型读取内容
        if filepath.endswith('.csv'):
            # 尝试不同编码读取 CSV
            df = None
            for encoding in ['utf-8', 'gbk', 'gb2312', 'utf-8-sig']:
                try:
                    df = pd.read_csv(filepath, encoding=encoding)
                    break
                except UnicodeDecodeError:
                    continue
            if df is None:
                return jsonify({'success': False, 'message': '无法解析CSV文件，请检查编码'}), 400
        else:
            df = pd.read_excel(filepath)

        # 清理列名
        df.columns = [str(col).strip().replace('\n', '').replace('\r', '') for col in df.columns]

        # 处理NaN值 - 使用fillna替换为空字符串
        df = df.fillna('')

        # 返回前50行数据，转换为可JSON序列化的格式
        preview_data = []
        for _, row in df.head(50).iterrows():
            row_dict = {}
            for col in df.columns:
                val = row[col]
                # 确保所有值都是可JSON序列化的
                if pd.isna(val):
                    row_dict[col] = ''
                elif isinstance(val, (int, float)):
                    row_dict[col] = val if not pd.isna(val) else ''
                else:
                    row_dict[col] = str(val)
            preview_data.append(row_dict)

        columns = list(df.columns)

        return jsonify({
            'success': True,
            'data': {
                'file_name': inquiry_file.file_name,
                'columns': columns,
                'rows': preview_data,
                'total_rows': len(df)
            }
        })

    except Exception as e:
        return api_internal_error('preview_file', e)


@app.route('/api/engineer/contact/<int:record_id>', methods=['GET'])
@api_login_required
def get_engineer_contact(record_id):
    """获取工程师联系方式（默认脱敏，reveal=1 时返回完整号码）"""
    try:
        record = PriceRecord.query.get(record_id)
        if not record:
            return jsonify({'success': False, 'message': '记录不存在'}), 404

        bound_user = None
        if record.engineer_user_id:
            bound_user = User.query.get(record.engineer_user_id)
        if not bound_user:
            bound_user = get_user_by_engineer_name(record.engineer_name)

        if not bound_user:
            return jsonify({
                'success': True,
                'data': {
                    'engineer_name': record.engineer_name or '未知',
                    'department': record.department or '未知',
                    'is_bound': False,
                    'phone_masked': '',
                    'phone': ''
                }
            })

        reveal = str(request.args.get('reveal', '')).lower() in {'1', 'true', 'yes'}
        return jsonify({
            'success': True,
            'data': {
                'engineer_name': bound_user.real_name or record.engineer_name or '未知',
                'department': bound_user.department or record.department or '未知',
                'is_bound': True,
                'user_id': bound_user.id,
                'phone_masked': mask_phone(bound_user.phone),
                'phone': bound_user.phone if reveal else ''
            }
        })
    except Exception as e:
        return api_internal_error('get_engineer_contact', e)


@app.route('/api/uploader/contact/<int:file_id>', methods=['GET'])
@api_login_required
def get_uploader_contact(file_id):
    """获取上传人联系方式（默认脱敏，reveal=1 时返回完整号码）"""
    try:
        inquiry_file = InquiryFile.query.get(file_id)
        if not inquiry_file:
            return jsonify({'success': False, 'message': '文件不存在'}), 404

        bound_user = get_user_by_upload_user(inquiry_file.upload_user)
        if not bound_user:
            return jsonify({
                'success': True,
                'data': {
                    'file_id': inquiry_file.file_id,
                    'file_name': inquiry_file.file_name or '-',
                    'uploader_name': get_upload_user_display(inquiry_file.upload_user) or '未知',
                    'department': inquiry_file.department or '未知',
                    'inquiry_engineer': inquiry_file.engineer_name or '未知',
                    'is_bound': False,
                    'phone_masked': '',
                    'phone': ''
                }
            })

        reveal = str(request.args.get('reveal', '')).lower() in {'1', 'true', 'yes'}
        return jsonify({
            'success': True,
            'data': {
                'file_id': inquiry_file.file_id,
                'file_name': inquiry_file.file_name or '-',
                'uploader_name': bound_user.real_name or inquiry_file.upload_user or '未知',
                'department': bound_user.department or inquiry_file.department or '未知',
                'inquiry_engineer': inquiry_file.engineer_name or '未知',
                'is_bound': True,
                'user_id': bound_user.id,
                'phone_masked': mask_phone(bound_user.phone),
                'phone': bound_user.phone if reveal else ''
            }
        })
    except Exception as e:
        return api_internal_error('get_uploader_contact', e)


@app.route('/api/engineer/query', methods=['POST'])
@api_login_required
def engineer_lookup_query():
    """工程师查询（模板匹配，不接入AI）"""
    try:
        data = request.get_json() or {}
        query_text = str(data.get('query', '')).strip()
        if not query_text:
            return jsonify({'success': False, 'message': '查询文本不能为空'}), 400

        page = max(1, int(data.get('page', 1)))
        page_size_arg = data.get('page_size', data.get('pageSize'))
        per_page = int(page_size_arg) if page_size_arg else int(data.get('per_page', 20))
        per_page = min(max(per_page, 1), 100)
        max_scan = int(data.get('max_scan', 500))
        max_scan = min(max(max_scan, 50), 2000)

        parsed_params = enrich_parsed_params(query_text, parse_natural_language_query(query_text))
        parsed_params['parsed_intent'] = 'engineer_lookup'
        if not (parsed_params.get('material_name') or parsed_params.get('specification') or parsed_params.get('region')):
            return jsonify({'success': False, 'message': '请补充材料名称或规格后再查询负责人'}), 400

        query = PriceRecord.query
        if parsed_params.get('material_name'):
            query = query.filter(PriceRecord.material_name.like(f"%{parsed_params['material_name']}%"))
        if parsed_params.get('specification'):
            query = query.filter(PriceRecord.specification.like(f"%{parsed_params['specification']}%"))
        if parsed_params.get('region'):
            query = query.filter(PriceRecord.region.like(f"%{parsed_params['region']}%"))

        start_date = parsed_params.get('start_date')
        end_date = parsed_params.get('end_date')
        if start_date:
            query = query.filter(PriceRecord.quote_date >= start_date)
        if end_date:
            query = query.filter(PriceRecord.quote_date <= end_date)
        if not start_date and not end_date:
            one_year_ago = datetime.now().date() - timedelta(days=365)
            query = query.filter(PriceRecord.quote_date >= one_year_ago)

        records = rank_records(query.limit(max_scan).all(), parsed_params)

        engineer_map = {}
        for record in records:
            bound_user = None
            if record.engineer_user_id:
                bound_user = User.query.get(record.engineer_user_id)
            if not bound_user:
                bound_user = get_user_by_engineer_name(record.engineer_name)

            key = (bound_user.id if bound_user else None) or (record.engineer_name or 'unknown')
            if key not in engineer_map:
                engineer_map[key] = {
                    'engineer_name': (bound_user.real_name if bound_user else record.engineer_name) or '未知',
                    'department': (bound_user.department if bound_user else record.department) or '未知',
                    'is_bound': bool(bound_user),
                    'engineer_user_id': bound_user.id if bound_user else None,
                    'phone_masked': mask_phone(bound_user.phone) if bound_user else '',
                    'material_count': 0,
                    'latest_quote': record.quote_date.strftime('%Y-%m-%d') if record.quote_date else None,
                }
            engineer_map[key]['material_count'] += 1

        result_list = sorted(engineer_map.values(), key=lambda x: x['material_count'], reverse=True)
        total = len(result_list)
        pages = (total + per_page - 1) // per_page if total > 0 else 1
        start = (page - 1) * per_page
        end = start + per_page
        page_data = result_list[start:end] if start < total else []

        return jsonify({
            'success': True,
            'data': page_data,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': pages,
            'parsed_params': parsed_params
        })
    except Exception as e:
        return api_internal_error('engineer_lookup_query', e)


# ==================== 管理员接口 ====================

@app.route('/api/admin/system-status', methods=['GET'])
@admin_required
def admin_system_status():
    """系统状态诊断（用于排查 502/数据库异常）"""
    try:
        status = {
            'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'database': {
                'inquiry_file_ok': False,
                'price_record_ok': False,
                'user_ok': False,
                'upload_audit_ok': False,
            }
        }
        try:
            InquiryFile.query.limit(1).all()
            status['database']['inquiry_file_ok'] = True
        except Exception:
            pass
        try:
            PriceRecord.query.limit(1).all()
            status['database']['price_record_ok'] = True
        except Exception:
            pass
        try:
            User.query.limit(1).all()
            status['database']['user_ok'] = True
        except Exception:
            pass
        try:
            UploadAudit.query.limit(1).all()
            status['database']['upload_audit_ok'] = True
        except Exception:
            pass
        status['database']['all_ok'] = all(status['database'].values())
        return jsonify({'success': True, 'data': status})
    except Exception as e:
        return api_internal_error('admin_system_status', e)


@app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_list_users():
    """获取用户列表"""
    try:
        users = User.query.order_by(User.created_at.desc()).all()
        return jsonify({
            'success': True,
            'data': [u.to_dict() for u in users],
            'total': len(users)
        })
    except Exception as e:
        return api_internal_error('admin_list_users', e)


@app.route('/api/admin/users', methods=['POST'])
@admin_required
def admin_add_user():
    """添加用户"""
    try:
        data = request.get_json() or {}
        username = data.get('username', '').strip()
        phone = data.get('phone', '').strip()
        real_name = data.get('real_name', '')
        department = data.get('department', '')
        role = data.get('role', 'user')
        if role not in ALLOWED_USER_ROLES:
            return jsonify({'success': False, 'message': '角色参数非法'}), 400

        if username and not re.fullmatch(r'[A-Za-z0-9_]{3,32}', username):
            return jsonify({'success': False, 'message': '用户名需为3-32位字母数字下划线'}), 400

        # 验证手机号格式
        if not phone or len(phone) != 11 or not phone.startswith('1'):
            return jsonify({'success': False, 'message': '手机号格式不正确'}), 400

        # 检查用户名是否已存在
        if username and User.query.filter_by(username=username).first():
            return jsonify({'success': False, 'message': '用户名已存在'}), 400

        # 检查手机号是否已存在
        existing = User.query.filter_by(phone=phone).first()
        if existing:
            return jsonify({'success': False, 'message': '该手机号已被使用'}), 400

        # 创建用户
        user = User(
            username=username or None,
            phone=phone,
            real_name=real_name,
            department=department,
            role=role,
            is_active=True
        )
        # 设置默认密码为手机号后六位
        user.set_password(user.get_default_password())

        db.session.add(user)
        db.session.flush()
        auto_bind_engineer_for_user(user)
        db.session.commit()

        return jsonify({
            'success': True,
            'message': '用户创建成功，初始密码为手机号后六位',
            'user': user.to_dict()
        })

    except Exception as e:
        db.session.rollback()
        return api_internal_error('admin_add_user', e)


@app.route('/api/admin/users/<int:user_id>', methods=['PUT'])
@admin_required
def admin_update_user(user_id):
    """编辑用户"""
    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({'success': False, 'message': '用户不存在'}), 404

        data = request.get_json() or {}

        # 不能禁用自己
        if user.id == current_user.id and data.get('is_active') == False:
            return jsonify({'success': False, 'message': '不能禁用自己的账号'}), 400

        # 更新字段
        if 'username' in data:
            username = (data.get('username') or '').strip()
            if username and not re.fullmatch(r'[A-Za-z0-9_]{3,32}', username):
                return jsonify({'success': False, 'message': '用户名需为3-32位字母数字下划线'}), 400
            if username:
                exists = User.query.filter(User.username == username, User.id != user.id).first()
                if exists:
                    return jsonify({'success': False, 'message': '用户名已存在'}), 400
            user.username = username or None
        if 'real_name' in data:
            user.real_name = data['real_name']
        if 'department' in data:
            user.department = data['department']
        if 'role' in data:
            if data['role'] not in ALLOWED_USER_ROLES:
                return jsonify({'success': False, 'message': '角色参数非法'}), 400
            user.role = data['role']
        if 'is_active' in data:
            user.is_active = data['is_active']

        auto_bind_engineer_for_user(user)
        db.session.commit()

        return jsonify({
            'success': True,
            'message': '用户信息已更新',
            'user': user.to_dict()
        })

    except Exception as e:
        db.session.rollback()
        return api_internal_error('admin_update_user', e)


@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@admin_required
def admin_delete_user(user_id):
    """删除用户"""
    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({'success': False, 'message': '用户不存在'}), 404

        # 不能删除自己
        if user.id == current_user.id:
            return jsonify({'success': False, 'message': '不能删除自己的账号'}), 400

        EngineerBinding.query.filter_by(user_id=user.id).delete()
        PriceRecord.query.filter_by(engineer_user_id=user.id).update({'engineer_user_id': None})
        db.session.delete(user)
        db.session.commit()

        return jsonify({'success': True, 'message': '用户已删除'})

    except Exception as e:
        db.session.rollback()
        return api_internal_error('admin_delete_user', e)


@app.route('/api/admin/users/<int:user_id>/reset-password', methods=['POST'])
@admin_required
def admin_reset_password(user_id):
    """重置用户密码"""
    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({'success': False, 'message': '用户不存在'}), 404

        data = request.get_json() or {}
        new_password = data.get('new_password', '')

        if new_password:
            # 使用指定的新密码
            if len(new_password) < 6:
                return jsonify({'success': False, 'message': '密码长度至少6位'}), 400
            user.set_password(new_password)
            message = '密码已重置为新密码'
        else:
            # 重置为手机号后六位
            user.set_password(user.get_default_password())
            message = '密码已重置为手机号后六位'

        db.session.commit()

        return jsonify({'success': True, 'message': message})

    except Exception as e:
        db.session.rollback()
        return api_internal_error('admin_reset_password', e)


@app.route('/api/admin/engineer-bindings', methods=['GET'])
@admin_required
def admin_list_engineer_bindings():
    """管理员查看工程师绑定列表"""
    try:
        bindings = EngineerBinding.query.order_by(EngineerBinding.updated_at.desc()).all()
        user_map = {u.id: u for u in User.query.all()}
        data = []
        for item in bindings:
            user = user_map.get(item.user_id)
            row = item.to_dict()
            row['user'] = user.to_dict() if user else None
            data.append(row)
        return jsonify({'success': True, 'data': data, 'total': len(data)})
    except Exception as e:
        return api_internal_error('admin_list_engineer_bindings', e)


@app.route('/api/admin/engineer-bindings/pending', methods=['GET'])
@admin_required
def admin_list_pending_engineers():
    """管理员查看待绑定工程师名"""
    try:
        rows = db.session.query(
            PriceRecord.engineer_name,
            PriceRecord.department,
            db.func.count(PriceRecord.record_id).label('record_count'),
            db.func.max(PriceRecord.quote_date).label('latest_quote')
        ).filter(
            PriceRecord.engineer_name != None,
            PriceRecord.engineer_name != ''
        ).group_by(
            PriceRecord.engineer_name,
            PriceRecord.department
        ).order_by(
            db.func.count(PriceRecord.record_id).desc()
        ).all()

        bound_norms = {item.engineer_name_norm for item in EngineerBinding.query.all()}
        pending = []
        for row in rows:
            norm_name = normalize_engineer_key(row.engineer_name)
            if not norm_name or norm_name in bound_norms:
                continue
            pending.append({
                'engineer_name': row.engineer_name,
                'department': row.department or '未知',
                'record_count': row.record_count,
                'latest_quote': row.latest_quote.strftime('%Y-%m-%d') if row.latest_quote else None
            })

        return jsonify({'success': True, 'data': pending, 'total': len(pending)})
    except Exception as e:
        return api_internal_error('admin_list_pending_engineers', e)


@app.route('/api/admin/engineer-bindings', methods=['POST'])
@admin_required
def admin_bind_engineer_to_user():
    """管理员手动绑定工程师名到用户"""
    try:
        data = request.get_json() or {}
        user_id = int(data.get('user_id', 0))
        engineer_name = normalize_engineer_name(data.get('engineer_name', ''))
        if not user_id or not engineer_name:
            return jsonify({'success': False, 'message': '参数不完整'}), 400

        user = User.query.get(user_id)
        if not user:
            return jsonify({'success': False, 'message': '用户不存在'}), 404

        ensure_engineer_binding(user, engineer_name, bind_type='manual', confidence=1.0)
        db.session.flush()

        norm_name = normalize_engineer_key(engineer_name)
        updated = 0
        records = PriceRecord.query.filter(
            PriceRecord.engineer_name != None,
            PriceRecord.engineer_name != ''
        ).all()
        for record in records:
            if normalize_engineer_key(record.engineer_name) == norm_name:
                record.engineer_user_id = user.id
                updated += 1

        db.session.commit()
        return jsonify({
            'success': True,
            'message': '绑定成功',
            'updated_records': updated
        })
    except Exception as e:
        db.session.rollback()
        return api_internal_error('admin_bind_engineer_to_user', e)


@app.route('/api/admin/engineer-bindings/<int:binding_id>', methods=['DELETE'])
@admin_required
def admin_unbind_engineer_binding(binding_id):
    """管理员取消工程师名称与用户账号的关联"""
    try:
        binding = EngineerBinding.query.get(binding_id)
        if not binding:
            return jsonify({'success': False, 'message': '关联记录不存在'}), 404

        norm_name = binding.engineer_name_norm
        bound_user_id = binding.user_id

        updated = 0
        records = PriceRecord.query.filter_by(engineer_user_id=bound_user_id).all()
        for record in records:
            if normalize_engineer_key(record.engineer_name) == norm_name:
                record.engineer_user_id = None
                updated += 1

        db.session.delete(binding)
        db.session.commit()

        return jsonify({
            'success': True,
            'message': '已取消关联',
            'updated_records': updated
        })
    except Exception as e:
        db.session.rollback()
        return api_internal_error('admin_unbind_engineer_binding', e)


def init_db():
    """初始化数据库 - 创建所有数据库表"""
    with app.app_context():
        # 创建所有数据库的表（包括 binds 中的）
        db.create_all()
        ensure_schema_compatibility()
        print("数据库初始化完成")
        print(f"  - 文件数据库: {INQUIRY_FILE_DB}")
        print(f"  - 明细数据库: {PRICE_RECORD_DB}")
        print(f"  - 审计数据库: {UPLOAD_AUDIT_DB}")
        print(f"  - 用户数据库: {USER_DB}")

        # 创建初始管理员（如果用户表为空）
        create_initial_admin()


def ensure_schema_compatibility():
    """
    轻量 schema 兼容修复（SQLite/MySQL 增量字段）。
    """
    try:
        for bind_key, table_name, column_name, sqlite_ddl, mysql_ddl in [
            (
                'inquiry_file',
                'inquiry_file',
                'stored_file_name',
                "ALTER TABLE inquiry_file ADD COLUMN stored_file_name VARCHAR(255)",
                "ALTER TABLE inquiry_file ADD COLUMN stored_file_name VARCHAR(255)"
            ),
            (
                'price_record',
                'price_record',
                'engineer_user_id',
                "ALTER TABLE price_record ADD COLUMN engineer_user_id INTEGER",
                "ALTER TABLE price_record ADD COLUMN engineer_user_id INT"
            ),
            (
                'user',
                'user',
                'username',
                "ALTER TABLE user ADD COLUMN username VARCHAR(64)",
                "ALTER TABLE user ADD COLUMN username VARCHAR(64)"
            ),
        ]:
            engine = db.engines.get(bind_key)
            if not engine:
                continue
            backend = engine.url.get_backend_name()
            with engine.begin() as conn:
                if backend == 'sqlite':
                    columns = [row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()]
                else:
                    columns = [row[0] for row in conn.exec_driver_sql(
                        f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                        f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{table_name}'"
                    ).fetchall()]
                if column_name not in columns:
                    conn.exec_driver_sql(sqlite_ddl if backend == 'sqlite' else mysql_ddl)

        # 创建工程师绑定表（用户库）
        user_engine = db.engines.get('user')
        if user_engine:
            EngineerBinding.__table__.create(bind=user_engine, checkfirst=True)

        # 核心索引优化（材料、地区、时间、组合索引）
        price_engine = db.engines.get('price_record')
        if price_engine:
            backend = price_engine.url.get_backend_name()
            statements = [
                "CREATE INDEX IF NOT EXISTS idx_price_material ON price_record(material_name)",
                "CREATE INDEX IF NOT EXISTS idx_price_region ON price_record(region)",
                "CREATE INDEX IF NOT EXISTS idx_price_quote_date ON price_record(quote_date)",
                "CREATE INDEX IF NOT EXISTS idx_price_material_region_date ON price_record(material_name, region, quote_date)",
            ]
            if backend == 'mysql':
                statements = [
                    "CREATE INDEX idx_price_material ON price_record(material_name)",
                    "CREATE INDEX idx_price_region ON price_record(region)",
                    "CREATE INDEX idx_price_quote_date ON price_record(quote_date)",
                    "CREATE INDEX idx_price_material_region_date ON price_record(material_name, region, quote_date)",
                ]
            with price_engine.begin() as conn:
                for stmt in statements:
                    try:
                        conn.exec_driver_sql(stmt)
                    except Exception:
                        # 索引已存在时忽略
                        pass
    except Exception as exc:
        print(f"[SCHEMA] ensure_schema_compatibility failed: {exc}", flush=True)


def create_initial_admin():
    """创建初始管理员账号"""
    try:
        # 检查用户表是否为空
        user_count = User.query.count()
        if user_count == 0:
            admin_phone = os.environ.get('INITIAL_ADMIN_PHONE', '13800138000').strip()
            admin_password = os.environ.get('INITIAL_ADMIN_PASSWORD', admin_phone[-6:] if len(admin_phone) >= 6 else '123456')

            if not admin_phone or len(admin_phone) != 11 or not admin_phone.startswith('1'):
                admin_phone = '13800138000'
            if not admin_password or len(admin_password) < 6:
                admin_password = admin_phone[-6:]

            # 创建默认管理员
            admin = User(
                username='admin',
                phone=admin_phone,
                real_name='系统管理员',
                department='系统管理部',
                role='admin',
                is_active=True
            )
            admin.set_password(admin_password)
            db.session.add(admin)
            db.session.commit()
            print("[初始化] 已创建默认管理员账号:")
            print(f"  - 手机号: {admin_phone}")
            print("  - 密码: 已按初始化配置生成")
            print("  - 请登录后及时修改密码")
    except Exception as e:
        print(f"[初始化] 创建管理员失败: {e}")
        db.session.rollback()


def startup_init():
    """应用启动初始化（开发模式+gunicorn 都执行）。"""
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'database')
    if not os.path.exists(db_path):
        os.makedirs(db_path)
    try:
        init_db()
        with app.app_context():
            init_nlp_parser()
    except Exception as exc:
        # 防止初始化异常导致进程退出（避免 Nginx 502）
        print(f"[STARTUP] init failed, keep process alive for retry: {exc}", flush=True)


startup_init()


if __name__ == '__main__':
    print("=" * 60)
    print("企业内部历史询价复用系统")
    port = int(os.environ.get('PORT', 5001))
    print(f"访问地址: http://localhost:{port}")
    print("=" * 60)
    app.run(debug=False, host='0.0.0.0', port=port)










































