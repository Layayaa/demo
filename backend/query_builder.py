"""Database query builder helpers."""

from datetime import datetime, timedelta
from models import db


def apply_price_filters(base_query, model, parsed_params):
    """Apply structured filters to SQLAlchemy query."""
    parsed_params = parsed_params or {}

    material_name = (parsed_params.get('material_name') or '').strip()
    if material_name:
        search_terms = [material_name]
        for term in parsed_params.get('material_synonyms', []) or []:
            if term and term not in search_terms:
                search_terms.append(term)
        or_conditions = [model.material_name.like(f'%{term}%') for term in search_terms if term]
        if or_conditions:
            base_query = base_query.filter(db.or_(*or_conditions))

    specification = (parsed_params.get('specification') or '').strip()
    if specification:
        base_query = base_query.filter(model.specification.like(f'%{specification}%'))

    region = (parsed_params.get('region') or '').strip()
    if region:
        base_query = base_query.filter(model.region.like(f'%{region}%'))

    start_date = parsed_params.get('start_date')
    end_date = parsed_params.get('end_date')
    if start_date:
        base_query = base_query.filter(model.quote_date >= start_date)
    if end_date:
        base_query = base_query.filter(model.quote_date <= end_date)
    if not start_date and not end_date:
        base_query = base_query.filter(model.quote_date >= (datetime.now().date() - timedelta(days=365)))

    return base_query
