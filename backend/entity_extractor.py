"""Rule-based entity extraction helpers for query optimization."""

import re

GENERIC_MATERIAL_WORDS = {
    '材料', '价格', '报价', '询价', '信息', '数据', '记录', '资料',
    '负责', '工程师', '联系', '谁', '哪个', '最近', '近', '查询'
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


def extract_entities(query_text: str, parsed_params: dict):
    query_text = query_text or ''
    parsed_params = parsed_params or {}
    material_name = (parsed_params.get('material_name') or '').strip()

    strict_phrase = material_name if len(material_name) >= 4 else ''
    material_keywords = split_keywords(material_name or query_text)
    if not material_keywords:
        material_keywords = split_keywords(query_text)

    return {
        'strict_material_phrase': strict_phrase,
        'material_keywords': material_keywords,
        'specification': (parsed_params.get('specification') or '').strip(),
        'region': (parsed_params.get('region') or '').strip(),
    }
