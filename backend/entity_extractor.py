"""Rule-based entity extraction helpers for query optimization."""

import re

GENERIC_MATERIAL_WORDS = {
    '材料', '价格', '报价', '询价', '信息', '数据', '记录', '资料',
    '负责', '工程师', '联系', '谁', '哪个', '最近', '近', '查询'
}

GENERIC_FILE_WORDS = {
    '文件', '附件', '报价', '报价单', '询价', '询价单', '询价表', '报价表',
    '记录', '资料', '来自哪个文件', '来自哪个附件', '来源文件', '来源附件',
    '这份文件', '这个文件', '这份报价', '这个报价', '这条记录'
}

ALL_SUBMISSION_ACTION_WORDS = ('提交', '上传', '录入', '导入')
UPLOADER_ACTION_WORDS = ('上传', '录入', '导入')
ENGINEER_ACTION_WORDS = ('提交',)
COUNT_HINT_WORDS = ('多少', '几', '数量', '总数', '统计', '汇总')
RANK_HINT_WORDS = ('最多', '最少', '排名', '排行', 'top')
RECORD_COUNT_HINT_WORDS = ('多少条', '几条', '条记录', '记录数')
FILE_COUNT_HINT_WORDS = ('多少份', '几份', '多少个', '几次', '份报价', '份文件', '分报价')
UPLOADER_COUNT_HINT_WORDS = ('多少人', '几人', '几位', '多少位', '上传人数', '上传人数量', '提交人数', '人员数量')

CORRECTION_RULES = (
    ('几分报价', '几份报价'),
    ('多少分报价', '多少份报价'),
    ('分报价', '份报价'),
    ('报介', '报价'),
    ('记彔', '记录'),
)

LEADING_NOISE_WORDS = (
    '最近', '近一周', '近一个月', '近三个月', '近半年', '近一年',
    '本周', '上周', '本月', '上月', '上个月', '这个月', '这个星期',
    '今天', '昨日', '昨天', '今年', '去年', '请问', '帮我', '麻烦',
    '查一下', '查下', '看下', '看看', '统计下', '统计一下'
)

INVALID_ACTOR_TOKENS = {
    '哪个', '哪位', '谁', '所有人', '全部人', '上传人', '提交人', '负责人'
}

INVALID_DEPARTMENT_TOKENS = {
    '哪个部', '哪个部门', '各部', '各部门', '每个部', '每个部门',
    '所有部门', '全部部门', '本部门', '该部门'
}


def normalize_text(value: str) -> str:
    text = (value or '').strip().lower()
    if not text:
        return ''
    text = re.sub(r'\s+', '', text)
    return text


def normalize_engineer_name(value: str) -> str:
    text = normalize_text(value)
    if not text:
        return ''
    text = text.replace('工程师', '').replace('工程', '').replace('老师', '')
    if text.endswith('工') and len(text) > 1:
        text = text[:-1]
    return text


def normalize_department_name(value: str) -> str:
    text = normalize_text(value)
    if not text:
        return ''
    text = text.replace('部门', '部')
    if text.endswith('部分') and len(text) > 2:
        text = text[:-1]
    return text


def split_keywords(value: str):
    text = normalize_text(value)
    if not text:
        return []
    chunks = re.findall(r'[\u4e00-\u9fff]{2,}|[a-z]+\d+|[a-z]+|\d+', text)
    out = []
    for item in chunks:
        if item in GENERIC_MATERIAL_WORDS:
            continue
        if len(item) < 2:
            continue
        out.append(item)
    return out


def _dedupe(values):
    out = []
    seen = set()
    for value in values:
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _strip_leading_noise(value: str) -> str:
    text = value or ''
    changed = True
    while changed and text:
        changed = False
        for prefix in LEADING_NOISE_WORDS:
            if text.startswith(prefix):
                text = text[len(prefix):]
                changed = True
    return text


def _normalize_query_with_corrections(query_text: str):
    compact = normalize_text(query_text)
    if not compact:
        return '', []

    corrected = compact
    corrections = []
    for src, dst in CORRECTION_RULES:
        if src in corrected:
            corrected = corrected.replace(src, dst)
            corrections.append({'from': src, 'to': dst})

    return corrected, corrections


def _is_valid_actor(actor: str) -> bool:
    if not actor or len(actor) < 2:
        return False
    if actor in INVALID_ACTOR_TOKENS:
        return False
    if '谁' in actor or '附件' in actor:
        return False
    # 部门词不当作人员名
    if '部门' in actor or actor.endswith('部') or actor.endswith('部门') or actor.endswith('部分'):
        return False
    if actor.startswith('哪个') or actor.startswith('哪位'):
        return False
    return True


def _is_valid_department(dept: str) -> bool:
    if not dept or len(dept) < 2:
        return False
    if dept in INVALID_DEPARTMENT_TOKENS:
        return False
    if dept.startswith('哪个') or dept.startswith('各') or dept.startswith('每个'):
        return False
    if dept.startswith('这个') or dept.startswith('该') or dept.startswith('此') or dept.startswith('上面'):
        return False
    if '部' not in dept:
        return False
    return True


def _extract_actor_candidates(query_text: str, compact_text: str, action_words, possessive_pattern: str):
    compact = compact_text or normalize_text(query_text)
    if not compact:
        return []

    candidates = []
    for action in action_words:
        pattern = rf'([\u4e00-\u9fffA-Za-z0-9]{{2,20}}){action}'
        for match in re.finditer(pattern, compact):
            actor = _strip_leading_noise(match.group(1))
            actor = re.sub(r'(是谁|是)$', '', actor)
            actor = re.sub(r'(报价|文件|资料|记录)$', '', actor)
            actor = normalize_engineer_name(actor)
            if _is_valid_actor(actor):
                candidates.append(actor)

    for match in re.finditer(possessive_pattern, compact):
        actor = _strip_leading_noise(match.group(1))
        actor = re.sub(r'(是谁|是)$', '', actor)
        actor = normalize_engineer_name(actor)
        if _is_valid_actor(actor):
            candidates.append(actor)

    return _dedupe(candidates)


def _extract_uploader_candidates(query_text: str, compact_text: str = ''):
    return _extract_actor_candidates(
        query_text,
        compact_text,
        action_words=UPLOADER_ACTION_WORDS,
        possessive_pattern=r'([\u4e00-\u9fffA-Za-z0-9]{2,20})的(?:上传|录入|导入)'
    )


def _extract_engineer_candidates(query_text: str, compact_text: str = ''):
    return _extract_actor_candidates(
        query_text,
        compact_text,
        action_words=ENGINEER_ACTION_WORDS,
        possessive_pattern=r'([\u4e00-\u9fffA-Za-z0-9]{2,20})的提交'
    )


def _extract_department_candidates(query_text: str, compact_text: str = ''):
    compact = compact_text or normalize_text(query_text)
    if not compact:
        return []

    candidates = []
    for match in re.finditer(r'([\u4e00-\u9fffA-Za-z0-9]{1,20}部(?:门|分)?)', compact):
        dept = normalize_department_name(_strip_leading_noise(match.group(1)))
        if _is_valid_department(dept):
            candidates.append(dept)

    for match in re.finditer(r'([\u4e00-\u9fffA-Za-z0-9]{2,20})(?:部门)?提交', compact):
        raw = match.group(1)
        if raw.endswith('部') or raw.endswith('部门') or raw.endswith('部分'):
            dept = normalize_department_name(_strip_leading_noise(raw))
            if _is_valid_department(dept):
                candidates.append(dept)

    return _dedupe(candidates)


def _extract_stats_metric(query_text: str, compact_text: str = ''):
    compact = compact_text or normalize_text(query_text)
    if not compact:
        return 'file_count'

    if any(token in compact for token in RECORD_COUNT_HINT_WORDS):
        return 'record_count'
    if any(token in compact for token in UPLOADER_COUNT_HINT_WORDS):
        return 'uploader_count'
    if any(token in compact for token in FILE_COUNT_HINT_WORDS):
        return 'file_count'
    return 'file_count'


def _extract_file_keywords(query_text: str, compact_text: str = ''):
    raw_text = (query_text or '').strip()
    compact = compact_text or normalize_text(query_text)
    if not raw_text and not compact:
        return []

    candidates = []
    for match in re.finditer(r'附件[一二三四五六七八九十0-9]+', compact):
        candidates.append(match.group(0))

    for match in re.finditer(r'([\u4e00-\u9fffA-Za-z0-9._\-]{2,80}\.(?:xlsx|xls|csv|pdf|doc|docx|txt))', raw_text, flags=re.IGNORECASE):
        candidates.append(normalize_text(match.group(1)))

    for match in re.finditer(r'([\u4e00-\u9fffA-Za-z0-9]{2,40}(?:报价单|询价单|询价表|报价表|清单|附件))', compact):
        candidates.append(match.group(1))

    out = []
    for item in _dedupe(candidates):
        token = normalize_text(item)
        if not token:
            continue
        if token in GENERIC_FILE_WORDS:
            continue
        if len(token) < 2:
            continue
        out.append(token)

    return _dedupe(out)


def extract_entities(query_text: str, parsed_params: dict):
    query_text = query_text or ''
    parsed_params = parsed_params or {}
    material_name = (parsed_params.get('material_name') or '').strip()

    compact, corrections = _normalize_query_with_corrections(query_text)
    normalized_query_text = compact or normalize_text(query_text)

    strict_phrase = material_name if len(material_name) >= 4 else ''
    material_keywords = split_keywords(material_name) if material_name else []
    if not material_keywords and not material_name and not (parsed_params.get('region') or parsed_params.get('specification')):
        material_keywords = split_keywords(query_text)

    uploader_candidates = _extract_uploader_candidates(query_text, compact_text=normalized_query_text)
    engineer_candidates = _extract_engineer_candidates(query_text, compact_text=normalized_query_text)
    department_candidates = _extract_department_candidates(query_text, compact_text=normalized_query_text)
    file_keywords = _extract_file_keywords(query_text, compact_text=normalized_query_text)

    wants_count = any(token in normalized_query_text for token in COUNT_HINT_WORDS)
    wants_ranking = any(token in normalized_query_text for token in RANK_HINT_WORDS)
    has_submission_action = any(token in normalized_query_text for token in ALL_SUBMISSION_ACTION_WORDS)
    stats_metric = _extract_stats_metric(query_text, compact_text=normalized_query_text)

    return {
        'strict_material_phrase': strict_phrase,
        'material_keywords': material_keywords,
        'specification': (parsed_params.get('specification') or '').strip(),
        'region': (parsed_params.get('region') or '').strip(),
        'uploader_candidates': uploader_candidates,
        'engineer_candidates': engineer_candidates,
        'department_candidates': department_candidates,
        'file_keywords': file_keywords,
        'stats_metric': stats_metric,
        'wants_count': wants_count,
        'wants_ranking': wants_ranking,
        'has_submission_action': has_submission_action,
        'normalized_query_text': normalized_query_text,
        'corrections': corrections,
    }
