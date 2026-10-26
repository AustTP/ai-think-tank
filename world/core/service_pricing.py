"""Published per-service, per-endpoint prices.

The Bank records what was spent. This module answers what a call SHOULD cost
before it is made, from the `pricing` block in world/api_services.json. A
tool that spends money quotes first, compares the quote with its own cap and
the remaining account balance, and only then calls.
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

SERVICES_PATH = Path(__file__).resolve().parent.parent / 'api_services.json'


def load_services(path: Path | None = None) -> dict:
    p = Path(path) if path else SERVICES_PATH
    try:
        with open(p, encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data.get('services') if isinstance(data, dict) else {}


def _number(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _bucket(prices, side):
    for limit, usd in prices or []:
        try:
            if side <= float(limit):
                return float(usd), f'{side:g}px <= {float(limit):g}px'
        except (TypeError, ValueError):
            continue
    if prices:
        try:
            limit, usd = prices[-1]
            return float(usd), f'{side:g}px > {float(limit):g}px (largest bucket)'
        except (TypeError, ValueError):
            pass
    return None, ''


def _endpoint_spec(pricing, path):
    endpoints = pricing.get('endpoints') or {}
    if not endpoints:
        return pricing
    if path in endpoints:
        return endpoints[path]
    for prefix in sorted(endpoints, key=len, reverse=True):
        if path.startswith(prefix):
            return endpoints[prefix]
    return None


def estimate_service_spec_cost(service: dict, path: str, body: dict | None = None) -> dict | None:
    """Quote one call from a service spec. Handles free, fixed, page-request,
    variable/per-token, size/tile buckets, and per-item prices. Returns None
    only when the service publishes no price at all."""
    if not isinstance(service, dict):
        return None
    pricing = service.get('pricing') or {}
    rel_path = path or ''
    base_path = urllib.parse.urlparse(service.get('base_url') or '').path
    if base_path and rel_path.startswith(base_path):
        rel_path = rel_path[len(base_path):] or '/'
    spec = _endpoint_spec(pricing, rel_path)
    if not isinstance(spec, dict):
        return None
    kind = spec.get('kind') or pricing.get('kind') or 'unknown'
    out = {'usd': None, 'kind': kind, 'basis': '',
           'note': spec.get('note') or pricing.get('note') or '',
           'enforcedBy': spec.get('enforced_by') or pricing.get('enforced_by'),
           'endpoint': path or None}
    body = body if isinstance(body, dict) else {}
    if kind in ('free', 'daily_calls'):
        out['usd'] = 0.0
        out['basis'] = 'free'
        return out
    if kind == 'fixed':
        usd = _number(spec.get('usd'))
        if usd is not None:
            out['usd'] = round(usd, 6)
            out['basis'] = f'${usd:.4f} per call'
        return out
    if kind == 'page_request':
        usd = _number(spec.get('usd_per_request'))
        out['usd'] = round(usd, 6) if usd is not None else None
        out['basis'] = ((f'${usd:.4f} reference per request; enforced by '
                         f'{out["enforcedBy"] or "the page-request budget"}')
                        if usd is not None else '')
        return out
    if kind in ('variable', 'per_token'):
        out['basis'] = 'variable; no single per-call price'
        return out
    if kind == 'size_bucket':
        size = body.get('image_size') if isinstance(body.get('image_size'), dict) else {}
        w, h = _number(size.get('width')), _number(size.get('height'))
        if w and h:
            usd, basis = _bucket(spec.get('prices'), max(w, h))
            if usd is not None:
                out['usd'], out['basis'] = round(usd, 6), basis
        return out
    if kind == 'tile_bucket':
        tile = body.get('tile_size') if isinstance(body.get('tile_size'), dict) else {}
        w, h = _number(tile.get('width')), _number(tile.get('height'))
        if w and h:
            usd, basis = _bucket(spec.get('prices'), max(w, h))
            if usd is not None:
                out['usd'], out['basis'] = round(usd, 6), basis
        return out
    if kind == 'per_item':
        items = body.get(spec.get('item_key') or 'items')
        count = len(items) if isinstance(items, list) else 1
        unit = _number(spec.get('unit_usd'))
        if unit is not None:
            out['usd'] = round(unit * count, 6)
            out['basis'] = f'{count} item(s) x ${unit:.4f}'
        return out
    return out


def estimate_api_cost(service_id: str, path: str, body: dict | None = None,
                      services_path: Path | None = None) -> dict | None:
    """Convenience wrapper: look up a service by id and quote it."""
    service = (load_services(services_path) or {}).get(service_id)
    quote = estimate_service_spec_cost(service, path, body)
    if quote is not None:
        quote['service'] = service_id
    return quote


def describe_service_pricing(service: dict) -> dict:
    """Compact, agent-readable price summary for a service spec."""
    pricing = (service or {}).get('pricing') or {}
    kind = pricing.get('kind')
    endpoints = pricing.get('endpoints') or {}
    if endpoints:
        kind = 'endpoint_table'
    return {'kind': kind or 'unknown',
            'note': pricing.get('note') or '',
            'enforcedBy': pricing.get('enforced_by'),
            'endpoints': sorted(endpoints) if endpoints else []}
