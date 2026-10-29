
# ---------------------------------------------------------------------------
# Cascade triage for the grooming ceremony: a zero-dependency Naive Bayes
# classifier over the think tank's own groom decisions, fitted from the
# decision tape, standing IN FRONT of the JEV decider at the one call site
# where every card is minted.
#
# Why this shape: grooming is a high-volume, low-stakes, three-way verdict
# (accept / accept_generous / reject) whose labeled corpus already exists --
# every past groom decision sits in the tape with its prompt and choice.
# Asking a frontier model to re-derive a verdict the corpus can predict is
# pure spend. The cascade keeps the expensive judge for the cases that need
# it (low posterior, no training data, or an operator who has raised the
# live confidence bar because they distrust the machinery), and every
# auto-decision it makes is TAPED -- kind 'groom', model 'cascade-nb',
# structured meta -- so the audit trail shows who decided, and the
# calibration ledger can rate the cascade's outcomes exactly like JEV's.
#
# The classifier is deliberately primitive: multinomial Naive Bayes over
# stable-hashed word unigrams (md5, not Python's per-process hash()), with
# Laplace smoothing. No numpy, no sklearn -- the tank's dependency
# discipline. Self-reinforcement is blocked: cascade-produced rows are
# excluded from the training set.
#
# Dormant by default: it decides nothing until the operator writes the
# settings row `triage_cascade_enabled = on`. That is not timidity -- a
# decision-path that changes behavior based on data accumulated elsewhere
# in a shared store must not flip itself on uninvited, and an operator who
# has (rightly) said they do not yet trust tonight's changes should get
# the new spend-saving behavior only by naming the day they trust it.
# ---------------------------------------------------------------------------

_TRIAGE_CLASSES = ('accept', 'accept_generous', 'reject')
_TRIAGE_MIN_TRAIN = 40          # never arm on a cold corpus
_TRIAGE_DEFAULT_THRESHOLD = 0.9
_TRIAGE_MODEL_TTL_S = 600.0     # refit at most every ten minutes
_TRIAGE_TRAIN_LIMIT = 400
_TRIAGE_CACHE = {'model': None, 'fitted_at': 0.0, 'rows': 0}


def _triage_stable_hash(token):
    """Process-stable feature id. Python's hash() is salted per process,
    which would make the model differ between server restarts and CI."""
    import hashlib as _hashlib
    return int.from_bytes(
        _hashlib.md5(token.encode('utf-8', errors='replace')).digest()[:4],
        'big')


def _triage_tokens(text):
    """Lowercased word unigrams, length >= 2, as a sparse count vector over
    stable feature ids."""
    import re as _re
    counts = {}
    if not text:
        return counts
    for tok in _re.findall(r'[a-z0-9]{2,}', str(text).lower()):
        key = _triage_stable_hash(tok)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _triage_train_rows():
    """Labeled corpus: the tape's own groom decisions. Cascade-produced rows
    are excluded so the model cannot train on its own outputs and drift
    lock-in cannot compound."""
    import serve as _serve_mod
    rows = []
    try:
        with _serve_mod._db() as conn:
            rows = conn.execute(
                'SELECT prompt, choice FROM decision_tape '
                "WHERE kind = 'groom' AND ok = 1 AND model != 'cascade-nb' "
                'ORDER BY id DESC LIMIT ?', (_TRIAGE_TRAIN_LIMIT,)).fetchall()
    except Exception:
        return []
    return [(p, c) for p, c in rows
            if c in _TRIAGE_CLASSES and p]


def _triage_fit(rows):
    """Multinomial NB over token counts: class priors from document counts,
    per-class token tallies, Laplace +1 smoothing over the observed vocab."""
    prior_num = {}
    token_num = {}
    token_den = {}
    vocab = set()
    for prompt, choice in rows:
        prior_num[choice] = prior_num.get(choice, 0) + 1
        bucket = token_num.setdefault(choice, {})
        for key, count in _triage_tokens(prompt).items():
            bucket[key] = bucket.get(key, 0) + count
            vocab.add(key)
    for choice, bucket in token_num.items():
        token_den[choice] = sum(bucket.values())
    return {'priors': prior_num, 'tokens': token_num, 'token_den': token_den,
            'vocab': len(vocab), 'rows': len(rows)}


def _triage_classify(model, tokens):
    """(best_class, posterior) via log-space multinomial NB. Returns
    (None, 0.0) when the model is empty or a class has no support."""
    priors = (model or {}).get('priors') or {}
    classes = [c for c in _TRIAGE_CLASSES if priors.get(c)]
    if not classes or not tokens:
        return None, 0.0
    total_docs = sum(priors.values())
    vocab = model['vocab'] + 1
    logps = {}
    for choice in classes:
        lp = math.log(priors[choice] / total_docs)
        den = model['token_den'].get(choice, 0) + vocab
        bucket = model['tokens'].get(choice, {})
        for key, count in tokens.items():
            lp += count * math.log((bucket.get(key, 0) + 1) / den)
        logps[choice] = lp
    peak = max(logps.values())
    if peak == -math.inf:
        return None, 0.0
    z = peak + math.log(sum(math.exp(lp - peak) for lp in logps.values()))
    best = max(logps, key=logps.get)
    return best, math.exp(logps[best] - z)


def _triage_resolve_model():
    """Refit at most every _TRIAGE_MODEL_TTL_S; a groom ceremony is rare
    enough that the tape scan inside the TTL window stays amortized to
    nothing."""
    now = time.time()
    if _TRIAGE_CACHE['model'] is not None and \
            now - _TRIAGE_CACHE['fitted_at'] < _TRIAGE_MODEL_TTL_S:
        return _TRIAGE_CACHE['model'], _TRIAGE_CACHE['rows']
    rows = _triage_train_rows()
    if len(rows) < _TRIAGE_MIN_TRAIN:
        _TRIAGE_CACHE.update(model=None, fitted_at=now, rows=len(rows))
        return None, len(rows)
    model = _triage_fit(rows)
    _TRIAGE_CACHE.update(model=model, fitted_at=now, rows=len(rows))
    return model, len(rows)


def _triage_cascade_choice(instructions, criteria):
    """The cascade's verdict, or None to fall through to the real JEV
    decider. Fire-and-forget by contract: ANY failure inside here (tape
    unavailable, settings malformed, math edge) returns None so grooming
    proceeds exactly as it did before this existed."""
    try:
        import serve as _serve_mod
        if str(_serve_mod._get_setting('triage_cascade_enabled', 'off')).strip().lower() != 'on':
            return None
        # Operator posture tie-in: a live confidence bar at/above 0.9 means
        # the operator has pushed the tank into distrust-uncertain-actions
        # territory (or the tank's own realized reliability demanded it).
        # Either way, spend the judge; the cascade stands down.
        if _serve_mod._effective_safety_confidence() >= 0.9:
            return None
        try:
            threshold = float(_serve_mod._get_setting(
                'triage_cascade_threshold', _TRIAGE_DEFAULT_THRESHOLD))
        except (TypeError, ValueError):
            threshold = _TRIAGE_DEFAULT_THRESHOLD
        threshold = max(0.5, min(0.99, threshold))
        model, trained = _triage_resolve_model()
        if not model or trained < _TRIAGE_MIN_TRAIN:
            return None
        text = str(instructions or '') + ' ' + ' '.join(
            str(c.get('description') or '') for c in (criteria or [])
            if isinstance(c, dict))
        choice, posterior = _triage_classify(model, _triage_tokens(text))
        if choice is None or posterior < threshold:
            return None
        # Tape the auto-decision: same kind as a real groom so downstream
        # audit sees one continuous decision stream, with model/meta saying
        # who actually decided.
        _serve_mod._append_decision_tape(
            'groom', 'cascade-nb', str(instructions or '')[:2000],
            json.dumps([c.get('id') for c in (criteria or [])
                        if isinstance(c, dict)]),
            choice, float(posterior), 0.0,
            json.dumps({'cascade': {'trainedOn': trained,
                                    'posterior': round(float(posterior), 4)}}),
            True, meta={'cascade': True})
        return choice
    except Exception:
        return None


_TRIAGE_NIGHT_WINDOW = (2, 5)      # local hours 02:00-04:59
_TRIAGE_HOLDOUT_MIN = 8            # smallest evaluable holdout
_TRIAGE_HOLDOUT_FRAC = 0.25        # newest quarter is the exam
_TRIAGE_HOLDOUT_MAX = 100
_TRIAGE_ADOPT_ACCURACY = 0.7       # below this the fit is not trusted
_TRIAGE_COLD_CORPUS = 16           # 2x holdout min: nothing to evaluate on


def _triage_nightly_retrain(now=None, _hour=None):
    """The nightly, eval-gated retrain. Between nightly runs the 10-minute
    TTL refit keeps the model fresh; this pass is the quality gate: fit on
    everything but the newest quarter, examine on that quarter (the day's
    own decisions), and adopt the full-data fit ONLY when the holdout
    accuracy clears 0.7 AND beats the majority-class baseline. A classifier
    that cannot beat 'always say the common thing' must not silently
    replace a working one.

    Runs at most once per local night (02:00-04:59), tracked via the
    settings table so restarts and multiple loop ticks cannot double-run.
    Every outcome -- adopted, rejected, cold corpus -- lands in action_log
    under agent 'system' so the operator can read the classifier's report
    card each morning. Returns a stats dict; never raises (the calling
    loop swallows, but a silent failure here would be invisible)."""
    try:
        import serve as _serve_mod
        now = time.time() if now is None else now
        lt = time.localtime(now)
        hour = lt.tm_hour if _hour is None else _hour
        if not (_TRIAGE_NIGHT_WINDOW[0] <= hour < _TRIAGE_NIGHT_WINDOW[1]):
            return {'ran': False, 'reason': 'not-night'}
        today = time.strftime('%Y-%m-%d', lt)
        try:
            last = json.loads(_serve_mod._get_setting(
                'triage_last_retrain', '{}') or '{}')
        except Exception:
            last = {}
        if last.get('date') == today:
            return {'ran': False, 'reason': 'already-retrained',
                    'date': today}
        rows = _triage_train_rows()
        if len(rows) < _TRIAGE_COLD_CORPUS:
            _serve_mod.log_action('system', 'triage_nightly_retrain', {
                'date': today, 'adopted': False, 'reason': 'cold-corpus',
                'rows': len(rows), 'needed': _TRIAGE_COLD_CORPUS})
            return {'ran': True, 'adopted': False, 'reason': 'cold-corpus',
                    'rows': len(rows), 'date': today}
        holdout_n = max(_TRIAGE_HOLDOUT_MIN,
                        min(_TRIAGE_HOLDOUT_MAX,
                            int(len(rows) * _TRIAGE_HOLDOUT_FRAC)))
        train_rows, holdout = rows[:-holdout_n], rows[-holdout_n:]
        eval_model = _triage_fit(train_rows)
        correct = 0
        for prompt, truth in holdout:
            choice, _post = _triage_classify(
                eval_model, _triage_tokens(prompt))
            correct += 1 if choice == truth else 0
        accuracy = correct / len(holdout)
        counts = {}
        for _p, truth in holdout:
            counts[truth] = counts.get(truth, 0) + 1
        baseline = max(counts.values()) / len(holdout)
        degenerate = baseline >= 0.999
        adopted = (not degenerate and accuracy >= _TRIAGE_ADOPT_ACCURACY
                   and accuracy > baseline)
        stats = {'date': today, 'adopted': adopted, 'accuracy': round(accuracy, 4),
                 'baseline': round(baseline, 4), 'rows': len(rows),
                 'holdout': len(holdout), 'reason': 'eval' if not degenerate else 'degenerate-holdout'}
        if adopted:
            # Publish the full-data fit (holdout included -- it is now
            # labeled data, not exam material).
            full = _triage_fit(rows)
            _TRIAGE_CACHE.update(model=full, fitted_at=now, rows=len(rows))
        _serve_mod._set_setting('triage_last_retrain', json.dumps(stats))
        _serve_mod.log_action('system', 'triage_nightly_retrain', stats)
        return dict(stats, ran=True)
    except Exception as e:  # noqa: BLE001 -- the retrain must never break its loop
        try:
            import serve as _serve_mod
            _serve_mod.log_action('system', 'triage_nightly_retrain', {
                'error': f'{type(e).__name__}: {str(e)[:160]}'})
        except Exception:
            pass
        return {'ran': False, 'reason': 'error'}


def _triage_cascade_report():
    """Operator visibility: is the cascade armed, on what, how often has it
    decided, and what has it chosen. DB-only."""
    import serve as _serve_mod
    model, trained = _triage_resolve_model()
    out = {'armed': bool(model) and trained >= _TRIAGE_MIN_TRAIN,
           'trainedRows': trained, 'minTrain': _TRIAGE_MIN_TRAIN}
    try:
        last = json.loads(_serve_mod._get_setting('triage_last_retrain', '{}') or '{}')
        if isinstance(last, dict) and last:
            out['lastRetrain'] = last
    except Exception:
        pass
    try:
        out['enabled'] = str(_serve_mod._get_setting(
            'triage_cascade_enabled', 'on')).strip().lower() == 'on'
        out['standDown'] = _serve_mod._effective_safety_confidence() >= 0.9
        with _serve_mod._db() as conn:
            out['autoDecisions'] = conn.execute(
                "SELECT COUNT(*) FROM decision_tape WHERE model = 'cascade-nb'"
            ).fetchone()[0]
            out['byChoice'] = {c: n for c, n in conn.execute(
                "SELECT choice, COUNT(*) FROM decision_tape "
                "WHERE model = 'cascade-nb' GROUP BY choice")}
    except Exception:
        pass
    return out
