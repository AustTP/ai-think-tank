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
    net = _route_net_cache
    if not net:
        return None
    weights = net.get('weights') or []
    bias = net.get('bias') or []
    lanes = net.get('lanes') or []
    if not weights or not bias or len(bias) != len(lanes):
        return None
    best_k = 0
    best_s = None
    for k in range(len(lanes)):
        s = bias[k]
        for b, v in _route_net_features(text).items():
            row = weights[b] if b < len(weights) else None
            if row and k < len(row):
                s += v * row[k]
        if best_s is None or s > best_s:
            best_s = s
            best_k = k
    return lanes[best_k]


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
        live = live_lane or 'unclear'
        log_action('player', 'route_shadow_classified',
                   {'liveLane': live, 'netLane': net_lane,
                    'agree': net_lane == live,
                    'text': (text or '')[:200]},
                   authorized=True)
    except Exception:  # noqa: BLE001 -- shadow must never break routing
        pass
