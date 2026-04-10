"""Rule-based intent recognizer for natural language query."""

import re

ENGINEER_INTENT_KEYWORDS = (
    '谁负责', '哪个工程师', '工程师是谁', '资料在哪个工程师', '联系工程师',
    '负责人是谁', '谁在跟进', '谁负责这个材料', '谁在负责', '谁经手',
    '谁处理的', '谁跟的', '谁接手', '谁负责这份材料', '谁在管这个报价',
    '谁是经办人', '哪个人负责'
)

UPLOADER_INTENT_KEYWORDS = (
    '谁上传', '上传人是谁', '谁传的报价', '谁上传了这个报价',
    '这个报价谁上传的', '联系上传人', '谁提交的报价', '报价是谁上传的',
    '上传这个报价的人', '谁传的文件', '文件是谁上传的', '谁传上来的',
    '谁录入的', '谁导入的', '这份报价谁录的', '这份文件谁上传的'
)

FILE_OWNER_INTENT_KEYWORDS = (
    '谁有这份文件', '这份文件在谁手上', '这个文件在谁手里',
    '这个报价在谁手上', '谁手里有这个报价', '谁持有这份资料',
    '资料在谁手上', '文件在谁那', '这个资料谁有', '上面这个报价谁传的',
    '刚才这条报价是谁上传的', '上一条报价谁上传的'
)

COMPARISON_KEYWORDS = (
    '比较', '对比', '比价', '哪个便宜', '哪个贵', '性价比', '最低价', '最高价', '更划算'
)
TREND_KEYWORDS = ('趋势', '走势', '变化', '涨跌', '波动', '最近价格变化', '价格走向')
STATISTICS_KEYWORDS = ('平均', '最高', '最低', '统计', '汇总', '中位数', '波动率')

PERSON_QUERY_TERMS = ('谁', '哪位', '哪个人', '哪一个人')
UPLOAD_TERMS = ('上传', '提交', '传了', '传的', '导入', '录入')
FILE_TERMS = ('文件', '资料', '报价', '询价', '记录', '报价单')
ENGINEER_TERMS = ('工程师', '负责人', '经办人', '跟进人', '承办人')
FOLLOW_UP_TERMS = ('这份', '这个', '该', '此', '上面', '刚才', '上一条', '上述')


def _normalize_for_match(text: str) -> str:
    text = (text or '').strip().lower()
    if not text:
        return ''
    text = re.sub(r'\s+', '', text)
    text = re.sub(r'[，。！？、,.!?；;：:"“”‘’\-_/()（）\[\]【】]', '', text)
    return text


def _contains_any(text: str, keywords) -> bool:
    return any(keyword in text for keyword in keywords)


def detect_intent(query_text: str, fallback_intent: str = 'price_inquiry') -> str:
    raw_text = (query_text or '').strip()
    if not raw_text:
        return fallback_intent

    text = _normalize_for_match(raw_text)

    # 明确短语优先
    if _contains_any(text, FILE_OWNER_INTENT_KEYWORDS):
        return 'uploader_lookup'
    if _contains_any(text, UPLOADER_INTENT_KEYWORDS):
        return 'uploader_lookup'
    if _contains_any(text, ENGINEER_INTENT_KEYWORDS):
        return 'engineer_lookup'

    # 跟进式追问：这份/这个 + 谁 + 文件/报价 => 上传人
    if _contains_any(text, FOLLOW_UP_TERMS) and _contains_any(text, PERSON_QUERY_TERMS) and _contains_any(text, FILE_TERMS):
        return 'uploader_lookup'

    # 组合规则：谁 + 上传/文件词 => 上传人查询
    if _contains_any(text, PERSON_QUERY_TERMS) and (_contains_any(text, UPLOAD_TERMS) or _contains_any(text, FILE_TERMS)):
        if not _contains_any(text, ENGINEER_TERMS):
            return 'uploader_lookup'

    # 组合规则：谁 + 工程师/负责人 => 工程师查询
    if _contains_any(text, PERSON_QUERY_TERMS) and _contains_any(text, ENGINEER_TERMS):
        return 'engineer_lookup'

    # “联系谁”优先按上下文指向人
    if '联系' in text and _contains_any(text, UPLOAD_TERMS):
        return 'uploader_lookup'
    if '联系' in text and _contains_any(text, ENGINEER_TERMS):
        return 'engineer_lookup'

    if _contains_any(text, COMPARISON_KEYWORDS):
        return 'comparison'
    if _contains_any(text, TREND_KEYWORDS):
        return 'trend'
    if _contains_any(text, STATISTICS_KEYWORDS):
        return 'statistics'

    return fallback_intent
