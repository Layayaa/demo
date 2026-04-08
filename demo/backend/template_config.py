"""
Excel智能解析配置
实现基于关键词的字段映射，支持任意格式Excel
"""

import re
from datetime import datetime, timedelta

# 字段关键词映射表 - 用于智能识别
FIELD_KEYWORDS = {
    '序号': ['序号', '编号', 'no', 'No', 'NO', 'number', 'Number', 'id', 'ID', '序號', 'No.'],
    '项目名称': ['项目名称', '项目', '项目名', '项目工程', '工程项目', '工程名称', '工程', 'project', 'Project', '所属项目'],
    '材料名称': ['材料名称', '材料', '材料名', '品名', '物资名称', '物料名称', '材料/设备', '材料设备', 'material', 'Material', '材料名稱', '货物名称', '货品名称', '询价材料', '材料设备名称'],
    '规格型号': ['规格型号', '规格', '型号', '规格型号参数', '参数', '技术参数', '规格尺寸', 'Spec', 'Model', '规格型號', '规格/型号', '参数规格'],
    '单位': ['单位', '计量单位', '单位名称', '数量单位', 'unit', 'Unit', '單位', '计量单位名称'],
    '单价': ['单价', '价格', '报价', '含税价', '单价1', '单价(元)', '价格(元)', '综合单价', '询价单价', 'price', 'Price', '單價', '单价元', '含税单价', '报价单价'],
    '是否含税': ['是否含税', '含税', '税率', '含税情况', 'tax', 'Tax', '是否含稅', '税费'],
    '供应商/来源': ['供应商', '供应商名称', '供货商', '厂家', '品牌', '来源', '供应商/来源', '品牌/供应商', 'supplier', 'Supplier', '供應商', '供货单位', '品牌供应商'],
    '地区': ['地区', '区域', '地点', '项目地区', '所在地区', '所在城市', '城市', 'region', 'Region', '地區', '询价地区'],
    '报价时间': ['报价时间', '询价时间', '时间', '报价日期', '询价日期', '日期', '报价时间2', 'date', 'Date', '報價時間', '询价日期时间'],
    '备注': ['备注', '说明', '备注说明', '其他说明', 'remark', 'Remark', '備註', '备注信息'],
    '填报部门': ['填报部门', '部门', '询价部门', '所属部门', '询价单位', 'department', 'Department', '填報部門', '填报单位'],
    '填报工程师': ['填报工程师', '工程师', '询价人', '询价人员', '填报人', '经办人', '联系人', 'engineer', 'Engineer', '填報工程師', '询价工程师', '交付人员', '负责人', '填报人员', '交付人'],
    '询价类别': ['询价类别', '类别', '类型', '材料类别', '物资类别', 'category', 'Category', '詢價類別', '询价类型'],
    '上传人': ['上传人', '上传者', '录入人', '录入者', '录入', 'uploader', '询价人员'],
}

# 必须字段（至少有一个才能解析）
REQUIRED_FIELDS = ['材料名称']

# 多供应商列组（用于检测模板B）
SUPPLIER_COLUMN_GROUPS = [
    ['单价1', '是否含税', '供应商/来源'],
    ['单价2', '是否含税2', '供应商/来源2'],
    ['单价3', '是否含税3', '供应商/来源3'],
]

# 数据清洗规则
DATA_CLEANING_RULES = {
    '报价时间': {
        'formats': [
            '%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d',
            '%Y年%m月%d日', '%Y年%m月', '%Y-%m',
            '%m/%d/%Y', '%d/%m/%Y'
        ],
        'default_format': '%Y-%m-%d'
    },
    '是否含税': {
        'yes_values': ['是', 'yes', 'Yes', 'YES', '含税', '√', '✓', '☑', '1', 'True', 'true'],
        'no_values': ['否', 'no', 'No', 'NO', '不含税', '×', '✗', '☐', '0', 'False', 'false']
    },
    '单价': {
        'remove_chars': ['￥', '¥', '元', ',', '，', ' '],
        'min_value': 0,
        'max_value': 100000000,
        # 非数值价格关键词（这些值应设为NULL）
        'non_numeric_keywords': ['按实结算', '双方协商', '面议', '电询', '询价', '待定', '无'],
        # 复合价格分隔符
        'composite_separators': ['/', '\\', '~', '-', '至']
    },
    '供应商': {
        # 需要从供应商字段中移除的关键词
        'remove_keywords': ['联系人', '联系方式', '电话', '手机', 'Tel', 'tel', '联系'],
        # 手机号正则
        'phone_pattern': r'1[3-9]\d{9}'
    }
}


def match_column_to_field(column_name, field_keywords=None):
    """
    将列名匹配到目标字段

    Args:
        column_name: Excel中的列名
        field_keywords: 字段关键词字典，默认使用FIELD_KEYWORDS

    Returns:
        matched_field: 匹配到的字段名，如果没有匹配则返回None
    """
    if field_keywords is None:
        field_keywords = FIELD_KEYWORDS

    column_lower = str(column_name).lower().strip()

    # 1. 精确匹配
    for field, keywords in field_keywords.items():
        if column_name in keywords or column_lower in [k.lower() for k in keywords]:
            return field

    # 2. 包含匹配
    for field, keywords in field_keywords.items():
        for keyword in keywords:
            if keyword.lower() in column_lower or column_lower in keyword.lower():
                return field

    # 3. 相似度匹配
    best_match = None
    best_score = 0

    for field, keywords in field_keywords.items():
        for keyword in keywords:
            # 计算字符重叠相似度
            set1 = set(column_lower)
            set2 = set(keyword.lower())
            intersection = len(set1 & set2)
            union = len(set1 | set2)
            score = intersection / union if union > 0 else 0

            if score > best_score and score > 0.5:  # 50%相似度阈值
                best_score = score
                best_match = field

    return best_match


def build_column_mapping(df_columns):
    """
    构建列名到字段的映射关系

    Args:
        df_columns: DataFrame的列名列表

    Returns:
        mapping: {字段名: 列名} 的字典
        unmatched: 未匹配的列名列表
        confidence: 匹配置信度 (0-1)
    """
    mapping = {}
    unmatched = []

    for col in df_columns:
        # 跳过 Unnamed 列
        if 'Unnamed' in str(col):
            continue

        col_str = str(col)

        # 特殊处理多供应商模板的列
        # 单价1、单价2、单价3 等
        if col_str.startswith('单价') and any(c.isdigit() for c in col_str):
            mapping[col_str] = col
            continue
        # 供应商/来源1、供应商/来源2、供应商/来源3 等
        if '供应商' in col_str and any(c.isdigit() for c in col_str):
            mapping[col_str] = col
            continue
        # 是否含税1、是否含税2、是否含税3 等
        if col_str.startswith('是否含税') and any(c.isdigit() for c in col_str):
            mapping[col_str] = col
            continue

        matched_field = match_column_to_field(col)
        if matched_field:
            # 如果该字段已被映射，保留更匹配的
            if matched_field not in mapping:
                mapping[matched_field] = col
        else:
            unmatched.append(col)

    # 计算置信度
    matched_required = sum(1 for f in REQUIRED_FIELDS if f in mapping)
    confidence = matched_required / len(REQUIRED_FIELDS) if REQUIRED_FIELDS else 1.0

    return mapping, unmatched, confidence


def detect_multi_supplier(df_columns):
    """
    检测是否为多供应商模板

    Returns:
        is_multi: 是否为多供应商模板
        supplier_groups: 供应商列组列表
    """
    columns_str = ' '.join(str(c) for c in df_columns)

    # 检测是否存在单价1、单价2、单价3等
    has_multi_price = '单价1' in columns_str or '单价2' in columns_str
    has_multi_supplier = '供应商/来源2' in columns_str or '供应商2' in columns_str

    if has_multi_price or has_multi_supplier:
        return True, SUPPLIER_COLUMN_GROUPS

    return False, None


def clean_price(value):
    """
    清洗价格数据 - 增强版

    处理规则:
    1. 复合价格 "A/B" -> 取平均值
    2. 非数值价格 -> 返回 None
    3. 正常价格 -> 返回数值
    """
    import pandas as pd

    if pd.isna(value):
        return None, None

    value_str = str(value).strip()
    rules = DATA_CLEANING_RULES['单价']

    # 检查非数值关键词
    for keyword in rules['non_numeric_keywords']:
        if keyword in value_str:
            return None, f"非数值价格: {value_str}"

    # 移除特殊字符
    for char in rules['remove_chars']:
        value_str = value_str.replace(char, '')

    # 检查复合价格（如 "12.3/17.6"）
    for sep in rules['composite_separators']:
        if sep in value_str and value_str.count(sep) == 1:
            parts = value_str.split(sep)
            try:
                prices = [float(p.strip()) for p in parts if p.strip()]
                if len(prices) >= 2:
                    avg_price = sum(prices) / len(prices)
                    return avg_price, f"复合价格平均值: {value_str} -> {avg_price}"
            except:
                pass

    # 尝试提取数值
    try:
        # 提取数字部分
        match = re.search(r'[\d.]+', value_str)
        if match:
            price = float(match.group())
            if rules['min_value'] <= price <= rules['max_value']:
                return price, None
    except:
        pass

    return None, f"无法解析价格: {value_str}"


def clean_supplier(value):
    """
    清洗供应商数据 - 分离公司名称和联系电话

    Returns:
        supplier_name: 清洗后的供应商名称
        contact_phone: 提取的联系电话（可选）
    """
    import pandas as pd

    if pd.isna(value):
        return None, None

    value_str = str(value).strip()
    rules = DATA_CLEANING_RULES['供应商']

    # 提取手机号
    phone_match = re.search(rules['phone_pattern'], value_str)
    contact_phone = phone_match.group() if phone_match else None

    # 移除手机号
    supplier_name = re.sub(rules['phone_pattern'], '', value_str)

    # 移除联系人类关键词
    for keyword in rules['remove_keywords']:
        supplier_name = re.sub(keyword, '', supplier_name, flags=re.IGNORECASE)

    # 清理多余空白
    supplier_name = re.sub(r'\s+', ' ', supplier_name).strip()
    supplier_name = supplier_name.strip('，,、:：')

    return supplier_name if supplier_name else None, contact_phone


def clean_date(value):
    """
    清洗日期数据 - 增强版

    处理规则:
    1. Excel序列号 -> YYYY-MM-DD
    2. 各种日期格式 -> YYYY-MM-DD
    3. 无效日期 -> 返回原始字符串
    """
    import pandas as pd

    if pd.isna(value):
        return None

    rules = DATA_CLEANING_RULES['报价时间']

    # 处理Excel日期序列号
    if isinstance(value, (int, float)):
        try:
            base_date = datetime(1899, 12, 30)
            result_date = base_date + timedelta(days=int(value))
            return result_date.strftime(rules['default_format'])
        except:
            pass

    # 尝试各种日期格式
    value_str = str(value).strip()
    for fmt in rules['formats']:
        try:
            dt = datetime.strptime(value_str, fmt)
            return dt.strftime(rules['default_format'])
        except:
            continue

    # 尝试pandas自动解析
    try:
        dt = pd.to_datetime(value)
        return dt.strftime(rules['default_format'])
    except:
        pass

    return value_str


def clean_value(value, field):
    """
    清洗字段值 - 统一入口

    Args:
        value: 原始值
        field: 字段名

    Returns:
        cleaned_value: 清洗后的值
    """
    import pandas as pd

    if pd.isna(value):
        return None

    # 文本类型字段
    if field in ['项目名称', '材料名称', '规格型号', '单位', '地区', '备注', '填报部门', '填报工程师', '询价类别']:
        return str(value).strip()

    # 供应商字段 - 特殊处理
    if field in ['供应商/来源', '供应商/来源1', '供应商/来源2', '供应商/来源3']:
        supplier_name, _ = clean_supplier(value)
        return supplier_name

    # 是否含税
    if field.startswith('是否含税'):
        rules = DATA_CLEANING_RULES['是否含税']
        value_str = str(value).strip()
        if value_str in rules['yes_values']:
            return '是'
        elif value_str in rules['no_values']:
            return '否'
        # 默认含税
        if '含税' in value_str or '税' in value_str:
            return '是'
        return '是'  # 默认值

    # 单价字段
    if field.startswith('单价'):
        price, _ = clean_price(value)
        return price

    # 报价时间字段
    if field.startswith('报价时间'):
        return clean_date(value)

    return str(value).strip() if value else None


def get_template_config(template_id):
    """获取模板配置（保持兼容）"""
    return {'id': template_id, 'name': '智能识别模板'}


# ============ 数据清洗质量报告生成 ============

def generate_cleaning_report(df, column_map):
    """
    生成数据清洗质量报告

    Args:
        df: DataFrame
        column_map: 列名映射

    Returns:
        report: 清洗报告字典
    """
    import pandas as pd

    report = {
        'total_rows': len(df),
        'fields': {},
        'warnings': [],
        'cleaned_count': 0
    }

    for field, col in column_map.items():
        if col not in df.columns:
            continue

        values = df[col].dropna()
        field_report = {
            'original_count': len(values),
            'cleaned_count': 0,
            'issues': []
        }

        # 价格字段检查
        if field.startswith('单价'):
            for val in values:
                price, warning = clean_price(val)
                if price is not None:
                    field_report['cleaned_count'] += 1
                if warning:
                    field_report['issues'].append(warning)

        # 供应商字段检查
        elif field.startswith('供应商'):
            for val in values:
                name, phone = clean_supplier(val)
                if name:
                    field_report['cleaned_count'] += 1

        # 日期字段检查
        elif field.startswith('报价时间'):
            for val in values:
                date = clean_date(val)
                if date:
                    field_report['cleaned_count'] += 1

        else:
            field_report['cleaned_count'] = field_report['original_count']

        report['fields'][field] = field_report

    return report


# ============ 智能识别填报工程师列 ============

def is_chinese_name(text):
    """
    判断是否为中文人名（2-4个中文字符）
    """
    if not text:
        return False
    text = str(text).strip()
    # 长度检查
    if len(text) < 2 or len(text) > 4:
        return False
    # 检查是否全是中文字符
    return all('\u4e00' <= c <= '\u9fff' for c in text)


def detect_engineer_column(df, column_map):
    """
    智能识别填报工程师列

    策略:
    1. 已有"填报工程师"列映射 → 直接返回
    2. 检查最后一个有数据的列是否为人名列
       - 列名含"人"、"人员"等关键词
       - 或数据内容都是2-4个中文字符

    Args:
        df: DataFrame
        column_map: 已构建的列名映射

    Returns:
        col_name: 识别到的列名，未识别返回None
        source: 识别来源描述
    """
    import pandas as pd

    # 策略1: 已有映射
    if '填报工程师' in column_map:
        return column_map['填报工程师'], '已映射列'

    # 策略2: 检查最后一个有数据的列
    # 找出有实际数据的列（非空值 > 5，排除Unnamed列）
    data_cols = []
    for col in df.columns:
        if 'Unnamed' in str(col):
            continue
        non_null_count = df[col].notna().sum()
        if non_null_count > 5:
            data_cols.append(col)

    if not data_cols:
        return None, None

    last_data_col = data_cols[-1]

    # 检查列名是否含人员相关关键词
    col_name = str(last_data_col).strip()
    person_keywords = ['人', '人员', '工程师', '负责人', '经办人', '交付', '填报']
    if any(kw in col_name for kw in person_keywords):
        return last_data_col, f'列名匹配("{col_name}")'

    # 检查数据内容是否都是人名
    samples = df[last_data_col].dropna().astype(str).str.strip().tolist()
    # 过滤掉可能是列名/标题的值（以"列"开头或包含特殊字符）
    samples = [s for s in samples if not s.startswith('列') and not s.startswith('Unnamed')]

    if samples:
        # 检查前20个样本是否都是人名
        check_samples = samples[:20]
        if all(is_chinese_name(s) for s in check_samples):
            return last_data_col, f'数据特征识别(最后数据列)'

    return None, None
