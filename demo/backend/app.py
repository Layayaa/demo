"""
企业内部历史询价复用系统 - Flask主应用
"""
import os
import sys
import json
import html
import hmac
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

from models import db, InquiryFile, PriceRecord, UploadAudit, QueryLog, User
from template_config import (
    FIELD_KEYWORDS, REQUIRED_FIELDS, DATA_CLEANING_RULES,
    match_column_to_field, build_column_mapping, detect_multi_supplier,
    clean_value, clean_price, clean_supplier, clean_date
)
from nlp_parser import NLPParser, parse_query

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
app.config['SESSION_COOKIE_SECURE'] = False  # 开发环境设为False，生产环境设为True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

CSRF_HEADER_NAME = 'X-CSRF-Token'
CSRF_MUTATION_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
CSRF_EXEMPT_PATHS = {'/api/login'}

RATE_LIMIT_DEFAULT = (120, 60)
RATE_LIMIT_RULES = {
    '/api/login': (5, 300),
    '/api/upload': (20, 300),
    '/api/natural_query': (60, 60),
    '/api/query': (90, 60)
}
_rate_limit_buckets = defaultdict(deque)
_rate_limit_lock = threading.Lock()

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


@app.route('/api/login', methods=['POST'])
def api_login():
    """登录接口"""
    try:
        data = request.get_json() or {}
        phone = data.get('phone', '').strip()
        password = data.get('password', '')

        # 验证手机号格式
        if not phone or len(phone) != 11 or not phone.startswith('1'):
            return jsonify({'success': False, 'message': '手机号或密码错误'}), 400

        # 查询用户
        user = User.query.filter_by(phone=phone).first()
        if not user:
            return jsonify({'success': False, 'message': '手机号或密码错误'}), 400

        # 验证密码
        if not user.check_password(password):
            return jsonify({'success': False, 'message': '手机号或密码错误'}), 400

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
        upload_user = request.form.get('upload_user', '未知')
        department = request.form.get('department', '')
        legacy_engineer_name = normalize_engineer_name(request.form.get('engineer_name', ''))
        batch_no = request.form.get('batch_no', '')
        validity_months = int(request.form.get('validity_months', 12))  # 默认12个月

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
                inquiry_file.parse_status = f'failed: 无法解析文件'
                db.session.commit()
                return jsonify({
                    'success': False,
                    'message': f'文件解析失败，请检查文件格式。错误信息: {"; ".join(parse_errors)}'
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
                                inquiry_type=get_cleaned_value(row, '询价类别')
                            )
                            records_to_add.append(price_record)
                            record_count += 1

                    except Exception as e:
                        print(f"[智能识别] 第{idx+1}行解析失败: {e}")
                        quality_issues.append(f"第{idx+1}行: {str(e)}")
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
                            inquiry_type=get_cleaned_value(row, '询价类别')
                        )
                        records_to_add.append(price_record)
                        record_count += 1

                    except Exception as e:
                        print(f"[智能识别] 第{idx+1}行解析失败: {e}")
                        quality_issues.append(f"第{idx+1}行: {str(e)}")
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
            fail_message = str(e)
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
            return jsonify({'success': False, 'message': f'文件解析失败: {fail_message}'}), 500

    except Exception as e:
        return jsonify({'success': False, 'message': f'上传失败: {str(e)}'}), 500


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

        if not query_text:
            return jsonify({'success': False, 'message': '查询文本不能为空'}), 400

        # 解析自然语言查询
        parsed_params = parse_natural_language_query(query_text)

        # 调试输出
        print(f"[自然语言查询] 输入: {query_text}", flush=True)
        print(f"[自然语言查询] 解析结果 material_name: {parsed_params.get('material_name')}", flush=True)
        print(f"[自然语言查询] 解析结果 specification: {parsed_params.get('specification')}", flush=True)
        print(f"[自然语言查询] 解析结果 region: {parsed_params.get('region')}", flush=True)
        print(f"[自然语言查询] 解析结果 time_display: {parsed_params.get('time_display')}", flush=True)
        print(f"[自然语言查询] 解析结果 start_date: {parsed_params.get('start_date')}", flush=True)
        print(f"[自然语言查询] 解析结果 parsed_intent: {parsed_params.get('parsed_intent')}", flush=True)

        # 确保parsed_params有正确的intent
        if not parsed_params.get('parsed_intent'):
            parsed_params['parsed_intent'] = 'price_inquiry'
        if not parsed_params.get('material_name'):
            # 使用原始查询作为材料名称（降级搜索）
            parsed_params['material_name'] = query_text

        # 构建数据库查询
        query = PriceRecord.query

        # 判断是否有解析到参数
        has_params = parsed_params['material_name'] or parsed_params['specification'] or parsed_params['region']

        if not has_params:
            # 如果没有解析到任何参数，使用原始文本进行模糊搜索
            # 对材料名称、规格型号、供应商进行搜索
            search_text = query_text.strip()
            or_conditions = [
                PriceRecord.material_name.like(f'%{search_text}%'),
                PriceRecord.specification.like(f'%{search_text}%'),
                PriceRecord.supplier.like(f'%{search_text}%'),
                PriceRecord.remark.like(f'%{search_text}%')
            ]
            query = query.filter(db.or_(*or_conditions))
            print(f"[自然语言查询] 未解析到参数，使用原始文本模糊搜索: {search_text}")
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

        # 先获取所有结果
        all_records = query.all()

        # 智能排序
        if has_params:
            # 计算匹配度得分
            def calculate_match_score(record):
                score = 0
                max_score = 0

                # 材料名称匹配
                if parsed_params['material_name']:
                    if record.material_name and parsed_params['material_name'] in record.material_name:
                        score += 30
                    max_score += 30

                # 规格型号匹配
                if parsed_params['specification']:
                    if record.specification and parsed_params['specification'] in record.specification:
                        score += 25
                    max_score += 25

                # 地区匹配
                if parsed_params['region']:
                    if record.region and parsed_params['region'] in record.region:
                        score += 20
                    max_score += 20

                # 时间匹配（最近的价格优先）
                if record.quote_date:
                    days_diff = (datetime.now().date() - record.quote_date).days
                    if days_diff <= 30:  # 最近30天
                        score += max(0, 15 - days_diff/2)  # 越近得分越高
                    max_score += 15

                # 价格匹配（如果查询中包含价格）
                if parsed_params['price']:
                    if record.price:
                        price_diff = abs(record.price - parsed_params['price'])
                        if price_diff <= parsed_params['price'] * 0.2:  # 价格差异在20%以内
                            score += max(0, 10 - price_diff/5)
                        max_score += 10

                # 归一化得分
                return score / max_score if max_score > 0 else 0

            # 应用智能排序
            results_with_scores = []
            for record in all_records:
                score = calculate_match_score(record)
                results_with_scores.append((record, score))

            # 按得分排序（降序）
            results_with_scores.sort(key=lambda x: x[1], reverse=True)
            sorted_records = [item[0] for item in results_with_scores]
        else:
            # 没有匹配条件时，按报价时间倒序
            sorted_records = sorted(all_records, key=lambda x: x.quote_date or datetime.min.date(), reverse=True)

        # 构建结果
        results = []
        updated_reference_count = False
        for record in sorted_records:
            # 增加引用计数
            record.reference_count = (record.reference_count or 0) + 1
            updated_reference_count = True

            result = record.to_dict()
            # 添加来源追溯信息 - 跨数据库查询
            source_file = get_source_file_info(record.file_id)
            if source_file:
                result['source_file_name'] = source_file.file_name
                result['source_upload_time'] = source_file.upload_time.strftime('%Y-%m-%d %H:%M:%S') if source_file.upload_time else None
                result['source_department'] = source_file.department
                result['source_engineer'] = source_file.engineer_name
            results.append(result)

        if updated_reference_count:
            db.session.commit()

        # 比价分析（如果查询意图是比价）
        comparison_data = None
        if parsed_params['parsed_intent'] == 'comparison':
            comparison_data = analyze_price_comparison(parsed_params, results)

        # 趋势分析（如果查询意图是趋势）
        trend_data = None
        if parsed_params['parsed_intent'] == 'trend':
            trend_data = analyze_price_trend(parsed_params, results)

        # 计算分页信息
        total = len(results)
        per_page = 20
        pages = (total + per_page - 1) // per_page if total > 0 else 1

        return jsonify({
            'success': True,
            'data': results,
            'total': total,
            'page': 1,
            'per_page': per_page,
            'pages': pages,
            'parsed_params': parsed_params,
            'comparison_data': comparison_data,
            'trend_data': trend_data
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'查询失败: {str(e)}'}), 500


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
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))

        # 构建查询
        query = PriceRecord.query

        if material_name:
            query = query.filter(PriceRecord.material_name.like(f'%{material_name}%'))

        if specification:
            query = query.filter(PriceRecord.specification.like(f'%{specification}%'))

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
        updated_reference_count = False
        for record in pagination.items:
            # 增加引用计数
            record.reference_count = (record.reference_count or 0) + 1
            updated_reference_count = True

            result = record.to_dict()
            # 添加来源追溯信息 - 跨数据库查询
            source_file = get_source_file_info(record.file_id)
            if source_file:
                result['source_file_name'] = source_file.file_name
                result['source_upload_time'] = source_file.upload_time.strftime('%Y-%m-%d %H:%M:%S') if source_file.upload_time else None
                result['source_department'] = source_file.department
                result['source_engineer'] = source_file.engineer_name
            results.append(result)

        if updated_reference_count:
            db.session.commit()

        return jsonify({
            'success': True,
            'data': results,
            'total': pagination.total,
            'page': page,
            'per_page': per_page,
            'pages': pagination.pages
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'查询失败: {str(e)}'}), 500


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
        ).group_by(InquiryFile.department).all()

        # 按工程师统计（去重后统计）
        # 去重条件：材料名 + 规格 + 供应商 + 报价日期
        # 使用子查询先去重，再按工程师分组
        from sqlalchemy import func, distinct

        # 方法：对每个工程师，统计去重后的记录数
        # 使用 DISTINCT 组合字段
        engineer_stats_raw = db.session.query(
            PriceRecord.engineer_name,
            PriceRecord.department,
            PriceRecord.material_name,
            PriceRecord.specification,
            PriceRecord.supplier,
            PriceRecord.quote_date
        ).filter(
            PriceRecord.engineer_name != None,
            PriceRecord.engineer_name != ''
        ).distinct().all()

        # 手动分组统计去重后的记录
        engineer_counts = {}
        engineer_latest = {}
        for row in engineer_stats_raw:
            key = (row.engineer_name, row.department)
            engineer_counts[key] = engineer_counts.get(key, 0) + 1
            if row.quote_date:
                if key not in engineer_latest or row.quote_date > engineer_latest[key]:
                    engineer_latest[key] = row.quote_date

        engineer_stats = [
            {
                'engineer_name': key[0],
                'department': key[1],
                'record_count': count,
                'latest_quote': engineer_latest.get(key)
            }
            for key, count in engineer_counts.items()
        ]
        # 按记录数排序
        engineer_stats.sort(key=lambda x: x['record_count'], reverse=True)

        # 按材料类型统计（去重后统计）
        material_stats_raw = db.session.query(
            PriceRecord.material_name,
            PriceRecord.specification,
            PriceRecord.supplier,
            PriceRecord.quote_date,
            PriceRecord.reference_count
        ).distinct().all()

        material_counts = {}
        material_refs = {}
        material_latest = {}
        for row in material_stats_raw:
            mat = row.material_name
            material_counts[mat] = material_counts.get(mat, 0) + 1
            material_refs[mat] = material_refs.get(mat, 0) + (row.reference_count or 0)
            if row.quote_date:
                if mat not in material_latest or row.quote_date > material_latest[mat]:
                    material_latest[mat] = row.quote_date

        # 排序取前10
        sorted_materials = sorted(material_counts.items(), key=lambda x: material_refs.get(x[0], 0), reverse=True)[:10]
        material_stats = [
            {
                'material_name': mat,
                'record_count': count,
                'total_references': material_refs.get(mat, 0),
                'latest_quote': material_latest.get(mat),
                'reference_per_record': material_refs.get(mat, 0) / count if count > 0 else 0
            }
            for mat, count in sorted_materials
        ]

        # 总体统计（去重后）
        total_unique_records = db.session.query(
            PriceRecord.material_name,
            PriceRecord.specification,
            PriceRecord.supplier,
            PriceRecord.quote_date
        ).distinct().count()

        total_files = InquiryFile.query.count()
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
        return jsonify({'success': False, 'message': f'统计失败: {str(e)}'}), 500


@app.route('/api/files', methods=['GET'])
@api_login_required
def list_files():
    """
    文件列表
    """
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))

        pagination = InquiryFile.query.order_by(InquiryFile.upload_time.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        return jsonify({
            'success': True,
            'data': [f.to_dict() for f in pagination.items],
            'total': pagination.total,
            'page': page,
            'per_page': per_page,
            'pages': pagination.pages
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'查询失败: {str(e)}'}), 500


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

        result = record.to_dict()

        # 添加完整的来源追溯信息 - 跨数据库查询
        source_file = get_source_file_info(record.file_id)
        if source_file:
            result['source_file_name'] = source_file.file_name
            result['source_upload_time'] = source_file.upload_time.strftime('%Y-%m-%d %H:%M:%S') if source_file.upload_time else None
            result['source_department'] = source_file.department
            result['source_engineer'] = source_file.engineer_name

        return jsonify({
            'success': True,
            'data': result
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'查询失败: {str(e)}'}), 500


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

        # 查找上传的文件
        upload_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
        # 文件名格式：时间戳_原始文件名
        safe_original_name = secure_filename(inquiry_file.file_name or '')
        for filename in os.listdir(upload_dir):
            if filename.endswith('_' + inquiry_file.file_name) or (safe_original_name and filename.endswith('_' + safe_original_name)):
                filepath = os.path.join(upload_dir, filename)
                return send_from_directory(upload_dir, filename, as_attachment=True, download_name=inquiry_file.file_name)

        # 如果没找到带时间戳的文件，尝试直接匹配
        if os.path.exists(os.path.join(upload_dir, inquiry_file.file_name)):
            return send_from_directory(upload_dir, inquiry_file.file_name, as_attachment=True)

        return jsonify({'success': False, 'message': '文件未找到'}), 404

    except Exception as e:
        return jsonify({'success': False, 'message': f'下载失败: {str(e)}'}), 500


@app.route('/api/preview/<int:file_id>')
@api_login_required
def preview_file(file_id):
    """预览Excel/CSV文件内容"""
    try:
        inquiry_file = InquiryFile.query.get(file_id)
        if not inquiry_file:
            return jsonify({'success': False, 'message': '文件不存在'}), 404

        # 查找上传的文件
        upload_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
        filepath = None
        safe_original_name = secure_filename(inquiry_file.file_name or '')
        for filename in os.listdir(upload_dir):
            if filename.endswith('_' + inquiry_file.file_name) or (safe_original_name and filename.endswith('_' + safe_original_name)):
                filepath = os.path.join(upload_dir, filename)
                break

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
        return jsonify({'success': False, 'message': f'预览失败: {str(e)}'}), 500


# ==================== 管理员接口 ====================

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
        return jsonify({'success': False, 'message': f'查询失败: {str(e)}'}), 500


@app.route('/api/admin/users', methods=['POST'])
@admin_required
def admin_add_user():
    """添加用户"""
    try:
        data = request.get_json() or {}
        phone = data.get('phone', '').strip()
        real_name = data.get('real_name', '')
        department = data.get('department', '')
        role = data.get('role', 'user')

        # 验证手机号格式
        if not phone or len(phone) != 11 or not phone.startswith('1'):
            return jsonify({'success': False, 'message': '手机号格式不正确'}), 400

        # 检查手机号是否已存在
        existing = User.query.filter_by(phone=phone).first()
        if existing:
            return jsonify({'success': False, 'message': '该手机号已被使用'}), 400

        # 创建用户
        user = User(
            phone=phone,
            real_name=real_name,
            department=department,
            role=role,
            is_active=True
        )
        # 设置默认密码为手机号后六位
        user.set_password(user.get_default_password())

        db.session.add(user)
        db.session.commit()

        return jsonify({
            'success': True,
            'message': '用户创建成功，初始密码为手机号后六位',
            'user': user.to_dict()
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'创建失败: {str(e)}'}), 500


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
        if 'real_name' in data:
            user.real_name = data['real_name']
        if 'department' in data:
            user.department = data['department']
        if 'role' in data:
            user.role = data['role']
        if 'is_active' in data:
            user.is_active = data['is_active']

        db.session.commit()

        return jsonify({
            'success': True,
            'message': '用户信息已更新',
            'user': user.to_dict()
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'更新失败: {str(e)}'}), 500


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

        db.session.delete(user)
        db.session.commit()

        return jsonify({'success': True, 'message': '用户已删除'})

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'删除失败: {str(e)}'}), 500


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
        return jsonify({'success': False, 'message': f'重置失败: {str(e)}'}), 500


def init_db():
    """初始化数据库 - 创建所有数据库表"""
    with app.app_context():
        # 创建所有数据库的表（包括 binds 中的）
        db.create_all()
        print("数据库初始化完成")
        print(f"  - 文件数据库: {INQUIRY_FILE_DB}")
        print(f"  - 明细数据库: {PRICE_RECORD_DB}")
        print(f"  - 审计数据库: {UPLOAD_AUDIT_DB}")
        print(f"  - 用户数据库: {USER_DB}")

        # 创建初始管理员（如果用户表为空）
        create_initial_admin()


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


if __name__ == '__main__':
    # 确保数据库目录存在
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'database')
    if not os.path.exists(db_path):
        os.makedirs(db_path)

    # 初始化数据库
    init_db()

    # 初始化NLP解析器（在应用上下文中）
    with app.app_context():
        init_nlp_parser()

    # 启动应用
    print("=" * 60)
    print("企业内部历史询价复用系统")
    print("访问地址: http://localhost:5000")
    print("=" * 60)
    app.run(debug=False, host='0.0.0.0', port=5000)
