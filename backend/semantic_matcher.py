"""Semantic relevance scoring for price records."""

from entity_extractor import normalize_text


def calculate_relevance_score(record_dict: dict, entities: dict) -> float:
    entities = entities or {}
    material = normalize_text(record_dict.get('material_name') or '')
    specification = normalize_text(record_dict.get('specification') or '')
    region = normalize_text(record_dict.get('region') or '')

    score = 0.0
    max_score = 0.0

    strict_phrase = normalize_text(entities.get('strict_material_phrase') or '')
    if strict_phrase:
        max_score += 60
        if strict_phrase == material:
            score += 60
        elif strict_phrase in material:
            score += 45

    keywords = entities.get('material_keywords') or []
    if keywords:
        max_score += 30
        hit_count = sum(1 for keyword in keywords if normalize_text(keyword) in material)
        score += (hit_count / max(len(keywords), 1)) * 30

    spec = normalize_text(entities.get('specification') or '')
    if spec:
        max_score += 5
        if spec in specification:
            score += 5

    reg = normalize_text(entities.get('region') or '')
    if reg:
        max_score += 5
        if reg in region:
            score += 5

    return 0.0 if max_score <= 0 else (score / max_score)


def relevance_threshold(entities: dict) -> float:
    strict_phrase = normalize_text((entities or {}).get('strict_material_phrase') or '')
    if len(strict_phrase) >= 6:
        return 0.45
    if len(strict_phrase) >= 4:
        return 0.35
    return 0.2
