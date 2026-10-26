from pathlib import Path

from core.service_pricing import estimate_api_cost
from core.village_geometry import (GATE as VILLAGE_GATE, SPAWN as VILLAGE_SPAWN,
                                   build as build_geometry)

"""Generate a village map as a package: background, collision, doors.

The village's art is metered. Every generation is quoted from the service's
published per-endpoint price table BEFORE it is called, checked against a
per-call cap and the remaining PixelLab balance, then charged to the actor's
ledger with the API's real usage.usd. Collision and door maps stay exact:
they are derived from the main map (mirror or copy) and the north arrival
road, never guessed from the generated pixels.
"""

VILLAGE_ART_WIDTH = 344
VILLAGE_ART_HEIGHT = 192
VILLAGE_ART_NATIVE = (688, 384)
VILLAGE_ART_DEFAULT_MAX_USD = 0.05
VILLAGE_ART_MIN_CREDITS_USD = 0.50
VILLAGE_ART_ENDPOINT = '/create-image-pixflux'


def _village_art_paths(village):
    base = os.path.join(ROOT, 'assets', f'think_tank_{village}.png')
    return {
        'image': base,
        'collision': os.path.join(ROOT, f'collision_grid_{village}.json'),
        'doors': os.path.join(ROOT, f'door_triggers_{village}.json'),
        'scenes': os.path.join(ROOT, 'scenes.json'),
    }


def _village_art_quote():
    body = {'image_size': {'width': VILLAGE_ART_WIDTH, 'height': VILLAGE_ART_HEIGHT},
            'no_background': False}
    estimate = estimate_api_cost('pixellab', VILLAGE_ART_ENDPOINT, body)
    remaining = _pixellab_credits_remaining()
    month = _spend_cap_month_view()
    shared_remaining = None
    if month.get('usage') is not None and float(month.get('cap') or 0) > 0:
        shared_remaining = max(0.0, float(month['cap']) - float(month['usage']))
    return {
        'estimate': estimate,
        'pixellabRemainingUsd': remaining,
        'sharedCapUsd': month.get('cap'),
        'sharedUsedUsd': month.get('usage'),
        'sharedCapRemainingUsd': shared_remaining,
    }


def _village_art_prompt(theme):
    theme = (theme or 'a quiet southern village with warm stone and olive trees').strip()
    return (
        'Top-down SNES-style pixel art village map. ' + theme + '. '
        'Keep the reference layout\'s building footprints, doors, paths, and open '
        'ground in the same places; restyle the terrain, roofs, foliage, and '
        'lighting. Cohesive 16-bit palette, readable paths, no text, no UI, no '
        'characters, no border.'
    )


def _village_art_reference_b64():
    import base64
    import io
    from PIL import Image
    src = Image.open(os.path.join(ROOT, 'assets', 'think_tank_background.png')).convert('RGB')
    ref = src.transpose(Image.FLIP_LEFT_RIGHT).resize(
        (VILLAGE_ART_WIDTH, VILLAGE_ART_HEIGHT), Image.NEAREST)
    buf = io.BytesIO()
    ref.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('ascii')


def _village_art_decode_and_save(image_b64, target):
    import base64
    import io
    from PIL import Image
    if image_b64.startswith('data:'):
        image_b64 = image_b64.split(',', 1)[1]
    raw = base64.b64decode(image_b64)
    img = Image.open(io.BytesIO(raw)).convert('RGB')
    img = img.resize(VILLAGE_ART_NATIVE, Image.NEAREST)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    img.save(target)


def _village_art_write_fallback(target):
    """Zero-cost fallback: the mirrored main background, same as the hand
    builder. Used by dryRun so geometry can ship without metered spend."""
    from PIL import Image
    src = Image.open(os.path.join(ROOT, 'assets', 'think_tank_background.png')).convert('RGB')
    out = src.transpose(Image.FLIP_LEFT_RIGHT).resize(VILLAGE_ART_NATIVE, Image.NEAREST)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    out.save(target)


def _village_art_write_geometry(village, mirror=True):
    grid, doors = build_geometry(Path(ROOT), mirror=bool(mirror))
    with open(os.path.join(ROOT, f'collision_grid_{village}.json'), 'w', encoding='utf-8') as fh:
        json.dump(grid, fh, separators=(',', ':'))
    with open(os.path.join(ROOT, f'door_triggers_{village}.json'), 'w', encoding='utf-8') as fh:
        json.dump(doors, fh, indent=2)
        fh.write('\n')


def _village_art_register_scene(village):
    path = os.path.join(ROOT, 'scenes.json')
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[village] = {
        'bgSprite': f'/assets/think_tank_{village}.png',
        'gridSprite': f'/collision_grid_{village}.json',
        'doorsSprite': f'/door_triggers_{village}.json',
        'spawn': dict(VILLAGE_SPAWN),
        'gate': dict(VILLAGE_GATE),
    }
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write('\n')


def generate_village_map(actor_id, village, theme='', mirror=True, dry_run=False,
                         force=False, max_usd=None, quote_only=False):
    """Quote, then generate one village's background and exact geometry.
    Returns a JSON-friendly dict; never raises for a refused call. The quote
    is always included, so a director can see the per-endpoint price before
    authorizing the spend."""
    state = get_state_from_db() or {}
    actor = str(actor_id or '').strip()
    if not actor or not _is_director_or_admin(state, actor):
        return {'ok': False, 'error': 'only a director or the chief of staff may generate village art'}
    village = re.sub(r'[^a-z0-9-]+', '-', str(village or '').strip().lower()).strip('-')
    if not village:
        return {'ok': False, 'error': 'village id is required'}
    if village in ('main', 'winter'):
        return {'ok': False, 'error': 'main and winter are built-in maps and cannot be regenerated'}
    paths = _village_art_paths(village)
    quote = _village_art_quote()
    cap = VILLAGE_ART_DEFAULT_MAX_USD if max_usd is None else float(max_usd)
    if quote['estimate'] is None:
        return {'ok': False, 'error': 'no published price for the generation endpoint', 'quote': quote}
    result = {'ok': True, 'village': village, 'quote': quote, 'maxUsd': cap}
    if quote_only:
        return {**result, 'quoteOnly': True}
    if not force and not dry_run and os.path.exists(paths['image']):
        return {**result, 'ok': False, 'alreadyExists': True,
                'error': 'art already exists; pass force=true to regenerate',
                'paths': paths}
    estimate = float(quote['estimate']['usd'])
    if not dry_run:
        if estimate > cap:
            return {**result, 'ok': False,
                    'error': (f'estimated ${estimate:.4f} exceeds the ${cap:.4f} per-call cap')}
        if quote['pixellabRemainingUsd'] is not None and quote['pixellabRemainingUsd'] < estimate:
            return {**result, 'ok': False,
                    'error': (f'PixelLab has ${quote["pixellabRemainingUsd"]:.2f}; '
                              f'this call needs ${estimate:.2f}')}
        if quote['sharedCapRemainingUsd'] is not None and quote['sharedCapRemainingUsd'] < estimate:
            return {**result, 'ok': False,
                    'error': (f'the shared monthly cap has ${quote["sharedCapRemainingUsd"]:.2f} left; '
                              f'this call needs ${estimate:.2f}')}
        if _think_tank_spend_cap_exceeded():
            return {**result, 'ok': False, 'error': 'the think tank spend cap is exhausted'}
        if _agent_spend_cap_exceeded(actor):
            return {**result, 'ok': False, 'error': 'your spend cap is exhausted'}
        if quote['pixellabRemainingUsd'] is not None and quote['pixellabRemainingUsd'] < VILLAGE_ART_MIN_CREDITS_USD:
            return {**result, 'ok': False,
                    'error': (f'PixelLab has ${quote["pixellabRemainingUsd"]:.2f}; '
                              f'the generation floor is ${VILLAGE_ART_MIN_CREDITS_USD:.2f}')}
        body = {
            'description': _village_art_prompt(theme),
            'image_size': {'width': VILLAGE_ART_WIDTH, 'height': VILLAGE_ART_HEIGHT},
            'text_guidance_scale': 7,
            'detail': 'highly detailed',
            'view': 'high top-down',
            'isometric': False,
            'no_background': False,
            'init_image': {'type': 'base64', 'base64': _village_art_reference_b64(), 'format': 'png'},
            'init_image_strength': 420,
            'seed': 51,
        }
        data, error = _pixellab_call('POST', VILLAGE_ART_ENDPOINT, body, timeout=180)
        if error:
            log_action(actor, 'generate_village_map', {
                'village': village, 'ok': False, 'error': error[:300],
                'estimateUsd': estimate, 'maxUsd': cap}, authorized=True)
            return {**result, 'ok': False, 'error': error}
        cost = float((data.get('usage') or {}).get('usd') or 0)
        if cost > 0:
            _accrue_spend('pixellab', cost, agent_id=actor)
            try:
                record_service_price_observation(
                    'pixellab', VILLAGE_ART_ENDPOINT, estimate, cost,
                    (quote.get('estimate') or {}).get('basis'), 'village_art', actor)
            except Exception:
                pass
        image_b64 = (data.get('image') or {}).get('base64') or ''
        if not image_b64:
            return {**result, 'ok': False, 'costUsd': cost,
                    'error': 'PixelLab returned no image'}
        try:
            _village_art_decode_and_save(image_b64, paths['image'])
        except Exception as e:
            return {**result, 'ok': False, 'costUsd': cost,
                    'error': f'could not decode the generated image: {e}'}
        result['costUsd'] = cost
        result['varianceUsd'] = round(cost - estimate, 6)
        result['pixellabRemainingUsd'] = _pixellab_credits_remaining()
    else:
        _village_art_write_fallback(paths['image'])
        result['dryRun'] = True
        result['costUsd'] = 0.0
    _village_art_write_geometry(village, mirror=mirror)
    _village_art_register_scene(village)
    log_action(actor, 'generate_village_map', {
        'village': village, 'ok': True, 'dryRun': bool(dry_run),
        'estimateUsd': estimate if not dry_run else 0.0,
        'costUsd': result.get('costUsd'),
        'maxUsd': cap, 'mirror': bool(mirror), 'theme': (theme or '')[:200],
        'paths': paths}, authorized=True)
    return result
