"""Price awareness for every registered external service.

The Bank knows what was spent. This answers the question an agent should ask
BEFORE spending: what does this call cost, what rule enforces it, and how much
of that budget is left? Prices come from api_services.json so an operator can
correct them without code.
"""

from core.service_pricing import (describe_service_pricing,
                                  estimate_service_spec_cost, load_services)


def _catalog():
    # The JSON registry is the source of truth for prices, so a corrected
    # price is visible without waiting for the live service cache to reload.
    return load_services() or {}


def _service_spec(service_id):
    return _catalog().get(service_id)


def _service_budget(service_id):
    """The enforced budget for a service, in its own unit. Free services say
    free; paid ones show month-to-date usage and the cap/balance that stops
    them."""
    if service_id == 'tavily':
        return {'unit': 'page_requests', 'used': _page_budget_used(),
                'cap': PAGE_REQUEST_MONTHLY_BUDGET,
                'enforcedBy': 'page_request_budget'}
    if service_id == 'apify':
        return {'unit': 'usd', 'used': round(float(_apify_spend_this_month()), 6),
                'cap': float(APIFY_MONTHLY_BUDGET_USD),
                'enforcedBy': 'apify_monthly_budget'}
    if service_id == 'pixellab':
        return {'unit': 'usd', 'balanceUsd': _pixellab_credits_remaining(),
                'minCreditsUsd': VILLAGE_ART_MIN_CREDITS_USD,
                'enforcedBy': 'pixellab_balance'}
    try:
        ledger = _spend_ledger_read() or {}
        used = _service_month_to_date(ledger, service_id)
    except Exception:
        used = 0.0
    return {'unit': 'usd', 'used': round(float(used or 0), 6),
            'cap': _spend_cap_month_view().get('cap'),
            'enforcedBy': 'shared_monthly_cap'}


def service_price_report(service=None, path=None, body=None):
    """List every service's price kind and note, or quote one endpoint."""
    month = _spend_cap_month_view()
    shared = {'cap': month.get('cap'), 'used': month.get('usage')}
    if month.get('usage') is not None and float(month.get('cap') or 0) > 0:
        shared['left'] = max(0.0, float(month['cap']) - float(month['usage']))
    if service:
        spec = _service_spec(service)
        if not spec:
            return {'ok': False, 'error': f'unknown service: {service}'}
        pricing = describe_service_pricing(spec)
        quote = estimate_service_spec_cost(spec, path or '', body)
        return {'ok': True, 'service': service, 'name': spec.get('name'),
                'baseUrl': spec.get('base_url'), 'pricing': pricing,
                'quote': quote, 'budget': _service_budget(service),
                'sharedCap': shared}
    rows = []
    for sid, spec in sorted(_catalog().items()):
        if '.' in sid:
            continue
        pricing = describe_service_pricing(spec)
        rows.append({'id': sid, 'name': spec.get('name'),
                     'baseUrl': spec.get('base_url'),
                     'priceKind': pricing.get('kind'),
                     'note': pricing.get('note'),
                     'enforcedBy': pricing.get('enforcedBy'),
                     'endpoints': pricing.get('endpoints')})
    return {'ok': True, 'services': rows, 'sharedCap': shared}
