# ---------------------------------------------------------------------------
# Route shadow net (foundry pilot 1, 2026-11-18): a tiny trained net that
# SHADOWS the live triage cascade without ever deciding a route. The live
# classifier (_classify_request_lane_default) runs a Jev quorum LLM call per
# incoming message; this part re-computes the lane with a hashed
# bag-of-words softmax net (library/nets/route-lane-v1.json, trained on the
# route_classified history in library/experiments/route-lane-net/) and logs
# `route_shadow_classified` beside the live decision. Agreement is therefore
# measurable passively as real traffic flows, and the disagreement log is the
# growing training set for a future promoted net. Failure rules: a missing,
# corrupt, or half-written net file means the shadow is OFF (never a wrong
# log), and any error in the shadow path is swallowed -- routing must never
# depend on it. The net file is re-checked when missing (60s throttle) so a
# net can be dropped in without a restart; a loaded net stays loaded until
# restart. Spec of the net format lives in the artifact itself + train.py.
# ---------------------------------------------------------------------------

_ROUTE_NET_PATH = os.path.join(LIBRARY_DIR, 'nets', 'route-lane-v1.json')
_route_net_cache = None          # loaded net dict, or None while disabled
_route_net_loaded_at = 0.0       # last time we tried (or managed) to load


def _route_net_features(text):
    """Hashed feature indices for one message. Spec identical to
    train.py: the message is truncated to the same 200 chars the action log
    stores (the net only ever votes on evidence it was trained on -- tokens
    past the cap would be weightless but would inflate the L2 norm and
    shrink the trained features' influence), [a-z0-9]+ tokens kept at
    len>=2, unigrams + adjacent bigrams, md5 -> first 8 hex -> mod buckets."""
    toks = [t for t in re.findall('[a-z0-9]+', (text or '').lower()[:200])
            if len(t) >= 2]
    grams = toks + [a + ' ' + b for a, b in zip(toks, toks[1:])]
    buckets = int(_route_net_cache.get('buckets') or 256) if _route_net_cache else 256
    feats = {}
    for g in grams:
        idx = int(hashlib.md5(g.encode()).hexdigest()[:8], 16) % buckets
        feats[idx] = feats.get(idx, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in feats.values()))
    if norm > 0:
        feats = {b: v / norm for b, v in feats.items()}
    return feats


def _route_net_predict(text):
    """Lane id from the loaded net, or None when the shadow is off. Argmax
    over W[bucket][lane] sums + bias; ties resolve to the earliest lane in
    the artifact's lane order (deterministic)."""
    ranked = _route_net_scores(text)
    return ranked[0][0] if ranked else None


def _route_net_scores(text):
    """Ranked [(lane, prob), ...] over every lane in the artifact, or None
    when the shadow is off. The probabilities are a softmax over the net's
    raw scores -- a heuristic confidence for ranking and context, NOT a
    calibrated probability of live agreement."""
    net = _route_net_cache
    if not net:
        return None
    weights = net.get('weights') or []
    bias = net.get('bias') or []
    lanes = net.get('lanes') or []
    if not weights or not bias or len(bias) != len(lanes):
        return None
    feats = _route_net_features(text)
    scores = []
    for k in range(len(lanes)):
        s = bias[k]
        for b, v in feats.items():
            row = weights[b] if b < len(weights) else None
            if row and k < len(row):
                s += v * row[k]
        scores.append(s)
    top = max(scores)
    exp = [math.exp(s - top) for s in scores]
    z = sum(exp) or 1.0
    ranked = sorted(zip(lanes, [e / z for e in exp]),
                    key=lambda pair: (-pair[1], pair[0]))
    return ranked


def _route_net_read(text):
    """Compact top-2 second opinion for logging beside live decisions:
    [{'lane', 'p'}, ...] or None when the shadow is off. This is the
    context the operator (and the denial log) sees when Jev rejects or
    cannot classify a message -- never injected into a director's
    conversation, so human judgment stays clean."""
    ranked = _route_net_scores(text)
    if not ranked:
        return None
    return [{'lane': lane, 'p': round(p, 3)}
            for lane, p in ranked[:2] if p > 0]


def _route_net_refresh():
    """Load the net if it is not loaded yet (throttled to one filesystem
    attempt a minute so a not-yet-deployed net costs almost nothing). A
    corrupt or truncated file leaves the shadow OFF."""
    global _route_net_cache, _route_net_loaded_at
    if _route_net_cache is not None:
        return
    now = time.time()
    if now - _route_net_loaded_at < 60.0:
        return
    _route_net_loaded_at = now
    try:
        with open(_ROUTE_NET_PATH, encoding='utf-8') as fh:
            net = json.load(fh)
        if isinstance(net, dict) and net.get('lanes') and net.get('weights') \
                and net.get('bias') and len(net['bias']) == len(net['lanes']):
            _route_net_cache = net
            print(f'[route-shadow] net loaded: {len(net["lanes"])} lanes, '
                  f'{len(net["weights"])} buckets '
                  f'(trained on {((net.get("meta") or {}).get("examples"))} examples)',
                  flush=True)
    except (OSError, ValueError):
        pass


def _route_shadow_classified(text, live_lane):
    """Log the net's lane beside the live lane for one classified message.
    NEVER raises, NEVER decides: any failure here is invisible to routing."""
    try:
        _route_net_refresh()
        if _route_net_cache is None:
            return
        net_lane = _route_net_predict(text)
        if net_lane is None:
            return
        ranked = _route_net_scores(text) or []
        net_margin = (round(ranked[0][1] - ranked[1][1], 3)
                      if len(ranked) >= 2 else 1.0)
        live = live_lane or 'unclear'
        log_action('player', 'route_shadow_classified',
                   {'liveLane': live, 'netLane': net_lane,
                    'agree': net_lane == live,
                    'netTop': [{'lane': lane, 'p': round(p, 3)}
                               for lane, p in ranked[:2]],
                    'netMargin': net_margin,
                    'text': (text or '')[:200]},
                   authorized=True)
    except Exception:  # noqa: BLE001 -- shadow must never break routing
        pass


# --- Disagreement review surface ---------------------------------------------
# The improvement loop for the live triage: every routed message now leaves
# TWO rows (route_classified from the live Jev quorum, route_shadow_classified
# from the tiny net). This surface pairs them by text + time and reports the
# disagreements with the net's confidence, so the operator can adjudicate: a
# wrong LIVE label (Jev inconsistency, e.g. the 2026-11-18 spike/story flip)
# becomes corrected training data; a wrong NET row becomes new training data.
# Either way the next retrain is better. Read-only, operator/admin gated.

_ROUTE_SHADOW_PAIR_WINDOW_S = 10.0   # twin rows log within milliseconds
_ROUTE_SHADOW_LIST_CAP = 50


def _route_shadow_pair(rows, now_ts=None):
    """Pair recent route_classified / route_shadow_classified rows into
    totals + disagreement and unpaired lists. `rows` is one list holding
    both actions, newest-first [(ts, details_json), ...] per action key.
    Pure function so tests cover the pairing without a database."""
    now_ts = time.time() if now_ts is None else now_ts
    classified = {}
    shadow = []
    for ts, details in rows:
        try:
            d = json.loads(details) if isinstance(details, str) else details
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if d.get('text') is None:
            continue
        key = ' '.join(str(d['text']).lower().split())
        if d.get('lane') is not None and 'liveLane' not in d:
            classified.setdefault(key, []).append(ts)
        elif 'liveLane' in d:
            shadow.append((ts, d, key))
    paired = 0
    agree = 0
    disagreements = []
    unpaired = []
    for ts, d, key in shadow:
        nearest = None
        best_gap = None
        for c_ts in classified.get(key, ()):
            gap = abs(ts - c_ts)
            if gap <= _ROUTE_SHADOW_PAIR_WINDOW_S \
                    and (best_gap is None or gap < best_gap):
                nearest, best_gap = c_ts, gap
        entry = {
            'ts': ts, 'atMs': int(ts * 1000), 'text': d.get('text'),
            'liveLane': d.get('liveLane'), 'netLane': d.get('netLane'),
            'agree': d.get('agree'),
            'netTop': d.get('netTop') or [], 'netMargin': d.get('netMargin'),
        }
        if nearest is None:
            entry['liveLane'] = None
            unpaired.append(entry)
            continue
        paired += 1
        if d.get('agree'):
            agree += 1
        else:
            disagreements.append(entry)
    window = max((ts for ts, _d, _k in shadow), default=now_ts) \
        - min((ts for ts, _d, _k in shadow), default=now_ts)
    return {
        'totals': {
            'shadow': len(shadow),
            'paired': paired,
            'agree': agree,
            'disagree': len(disagreements),
            'agreeRate': round(agree / paired, 3) if paired else None,
            'unpaired': len(unpaired),
        },
        'disagreements': disagreements[:_ROUTE_SHADOW_LIST_CAP],
        'unpaired': unpaired[:_ROUTE_SHADOW_LIST_CAP],
        'windowSeconds': round(window, 1) if shadow else 0.0,
    }


def _route_shadow_review(days):
    """Pull the recent pair rows from action_log and pair them."""
    days = max(1, min(int(days or 7), 14))
    since = time.time() - days * 24 * 3600
    with _db() as conn:
        rows = conn.execute(
            'SELECT ts, details FROM action_log '
            'WHERE action IN (?, ?) AND ts >= ? ORDER BY ts DESC',
            ('route_classified', 'route_shadow_classified', since),
        ).fetchall()
    out = _route_shadow_pair(rows)
    out['windowDays'] = days
    out['shadowOff'] = out['totals']['shadow'] == 0
    return out


@app.get('/api/route-shadow')
async def api_route_shadow(request: Request, days: int = 7):
    """Disagreement review for the route shadow net (foundry pilot 1):
    pairs each live triage decision with the tiny net's parallel answer and
    reports the disagreements with the net's confidence. The operator
    adjudicates; both outcomes improve the next retrain. Operator/admin
    only."""
    if not _require_player_or_admin(request):
        return JSONResponse({'error': 'Unauthorized -- operator only'},
                            status_code=401)
    return JSONResponse(await asyncio.to_thread(_route_shadow_review, days))
