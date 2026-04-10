"""Smart query service: intent + entities + semantic ranking."""

from intent_recognizer import detect_intent
from entity_extractor import extract_entities
from semantic_matcher import calculate_relevance_score, relevance_threshold


def enrich_parsed_params(query_text: str, parsed_params: dict):
    parsed_params = dict(parsed_params or {})
    parsed_params['parsed_intent'] = detect_intent(
        query_text,
        fallback_intent=parsed_params.get('parsed_intent') or 'price_inquiry'
    )
    parsed_params['entities'] = extract_entities(query_text, parsed_params)
    return parsed_params


def rank_records(records, parsed_params):
    records = records or []
    entities = (parsed_params or {}).get('entities') or {}
    threshold = relevance_threshold(entities)

    with_score = []
    for record in records:
        score = calculate_relevance_score(record.to_dict() if hasattr(record, 'to_dict') else record, entities)
        with_score.append((record, score))

    if entities.get('material_keywords') or entities.get('strict_material_phrase'):
        with_score = [item for item in with_score if item[1] >= threshold]

    with_score.sort(key=lambda x: x[1], reverse=True)
    return [item[0] for item in with_score]
