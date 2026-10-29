

def _ingest_hf_open_llm_fallback(models):
    """Fallback metric for open-weights models the Artificial Analysis index
    never measured (Mistral NeMo, Llama 3.1 70B, ...): their MMLU-Pro score
    from the archived HF Open LLM Leaderboard. Matched exactly through each
    catalog entry's hugging_face_id where present, else by normalized name.
    INSERT OR IGNORE: an existing row -- player research, an AA score -- is
    never clobbered by a fallback. pyarrow is an optional dependency; when it
    is not installed the fallback is skipped with a log line, exactly like the
    other optional ingestion dependencies (openpyxl, pypdf, ...)."""
    import urllib.request as _ur
    try:
        import pyarrow.parquet  # noqa: F401 -- availability guard
    except ImportError:
        print('[model-tiers] pyarrow not installed, skipping HF Open LLM fallback', flush=True)
        return 0
    parquet_url = _get_data_source('hf_contents_parquet_url')
    source_url = _get_data_source('hf_contents_source_url') or parquet_url
    if not parquet_url:
        print('[model-tiers] data source hf_contents_parquet_url unset -- skipping HF Open LLM fallback', flush=True)
        return 0
    try:
        with _ur.urlopen(parquet_url, timeout=20) as resp:  # nosec B310 -- URL comes from the operator registry, not user input
            meta = json.loads(resp.read().decode('utf-8', 'ignore'))
        data_url = (meta.get('parquet_files') or [{}])[0].get('url')
        if not data_url:
            return 0
        with _ur.urlopen(data_url, timeout=60) as resp:  # nosec B310 -- URL comes from the operator registry, not user input
            data = resp.read()
    except Exception:
        return 0
    hf = _parse_hf_contents(data)
    if not hf:
        return 0
    now = time.time()
    added = 0
    with _db() as conn:
        for m in models:
            mid = m['id']
            hf_id = m.get('hugging_face_id')
            value = hf.get(str(hf_id).lower()) if hf_id else None
            if value is None:
                # No exact hub id: match by name tokens. Prefer the SHORTEST
                # hub id that contains the catalog name's tokens -- fine-tunes
                # carry extra tokens (DPO, remix, ...), so the closest base
                # identity wins.
                nt = _benchmark_name_tokens(m.get('name') or mid)
                best_id, best_len = None, None
                for hub_id, score in hf.items():
                    if not nt or score is None:
                        continue
                    if nt <= _benchmark_name_tokens(hub_id) and (best_len is None or len(_benchmark_name_tokens(hub_id)) < best_len):
                        best_id, best_len = hub_id, len(_benchmark_name_tokens(hub_id))
                if best_id is not None:
                    value = hf[best_id]
            if value is None:
                continue
            cur = conn.execute(
                'INSERT OR IGNORE INTO model_benchmark_scores (model_id, benchmark, score, source_url, evaluator, checked_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (mid, 'MMLU-Pro', value, source_url, EVALUATOR_HUGGINGFACE, now),
            )
            if cur.rowcount:
                added += 1
    return added


def _sync_model_catalog(models):
    """Persist the current OpenRouter catalog snapshot into model_catalog and
    reconcile it against what the DB already knows:
      * new models are inserted (first_seen = now)
      * known models get refreshed prices + carried-over benchmark scores
        (the catalog's own Artificial Analysis indices are auto-ingested into
        model_benchmark_scores first -- _ingest_catalog_benchmarks -- then
        copied into the catalog row; player-entered research under other
        benchmark names is carried over untouched)
      * catalog models the OpenRouter entry carries no AA data for are
        backfilled from Artificial Analysis's own public leaderboard
        (_ingest_aa_leaderboard), then from the HF Open LLM Leaderboard's
        MMLU-Pro fallback for open-weights models AA never measured
        (_ingest_hf_open_llm_fallback); vision MMMU scores -- carried by no
        catalog or AA page -- come from the operator-registered MMMU
        leaderboard (_ingest_mmmu_leaderboard)
      * models that vanished from the live catalog are removed from BOTH
        model_catalog and model_benchmark_scores -- a score record for a model
        that no longer exists on OpenRouter is dead weight the value pick can
        never use, and letting it accumulate would mislead later refreshes.
    Returns the number of models purged (0 normally), so callers can surface
    catalog churn."""
    _ingest_catalog_benchmarks(models)
    _ingest_aa_leaderboard(models)
    _ingest_mmmu_leaderboard(models)
    _ingest_hf_open_llm_fallback(models)
    now = time.time()
    seen_ids = set()
    _log_date = time.strftime('%Y-%m-%d', time.gmtime())
    with _db() as conn:
        for m in models:
            mid = m['id']
            seen_ids.add(mid)
            try:
                prompt_price = float(m['pricing']['prompt'])
                completion_price = float(m['pricing']['completion'])
            except (KeyError, TypeError, ValueError):
                continue
            arch = m.get('architecture') or {}
            image_capable = 1 if 'image' in (arch.get('input_modalities') or []) else 0
            _blended = (prompt_price + completion_price) * 1_000_000
            conn.execute(
                'INSERT INTO model_catalog (model_id, name, prompt_price, completion_price, price_per_m, image_capable, first_seen, last_seen) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(model_id) DO UPDATE SET name=excluded.name, prompt_price=excluded.prompt_price, '
                'completion_price=excluded.completion_price, price_per_m=excluded.price_per_m, '
                'image_capable=excluded.image_capable, last_seen=excluded.last_seen',
                (mid, m.get('name', mid), prompt_price, completion_price,
                 _blended, image_capable, now, now),
            )
            # Daily price snapshot: append today's price for every model so
            # the market-wide history survives model_catalog's in-place
            # overwrite. Same-day re-runs overwrite (PK log_date+model_id).
            conn.execute(
                'INSERT INTO catalog_price_daily (log_date, model_id, price_per_m, logged_at) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(log_date, model_id) DO UPDATE SET price_per_m=excluded.price_per_m, logged_at=excluded.logged_at',
                (_log_date, mid, _blended, now),
            )
        # Carry over each model's known benchmark scores into the catalog row.
        known = {}
        for r in conn.execute('SELECT model_id, benchmark, score, evaluator FROM model_benchmark_scores').fetchall():
            known.setdefault(r[0], {})[r[1]] = {'score': r[2], 'evaluator': r[3]}
        for mid, bench_scores in known.items():
            conn.execute(
                'UPDATE model_catalog SET scores = ? WHERE model_id = ?',
                (json.dumps(bench_scores), mid),
            )
        # Purge models that are gone from the live catalog -- both the catalog
        # row and any player-entered benchmark scores for that model. Scanned
        # from BOTH tables: a score row can exist for a model that was never
        # inserted into model_catalog (e.g. pre-catalog research), so a purge
        # keyed only on catalog rows would orphan it forever.
        #
        # One-cycle GRACE period before purging: a model absent for a single
        # refresh is presumed a transient fetch gap (partial OpenRouter
        # response), and its player-entered scores are research that cannot be
        # re-created from a retry -- same "never cache a blip as permanent"
        # rule as MODEL_VERIFY_ATTEMPTS. Only a model missing from TWO
        # consecutive daily syncs (last_seen older than one refresh interval)
        # is treated as genuinely removed.
        grace_floor = now - MODEL_TIER_REFRESH_INTERVAL_S
        placeholders = ','.join('?' * len(seen_ids)) if seen_ids else ''
        if placeholders:
            gone = {r[0] for r in conn.execute(
                f'SELECT model_id FROM model_catalog WHERE model_id NOT IN ({placeholders}) AND last_seen < ?',  # nosec B608 -- placeholders are parameterized '?'
                tuple(seen_ids) + (grace_floor,)).fetchall()}
            gone |= {r[0] for r in conn.execute(
                f'SELECT model_id FROM model_benchmark_scores WHERE model_id NOT IN ({placeholders}) AND checked_at < ?',  # nosec B608 -- placeholders are parameterized '?'
                tuple(seen_ids) + (grace_floor,)).fetchall()}
        else:
            gone = {r[0] for r in conn.execute(
                'SELECT model_id FROM model_catalog WHERE last_seen < ?', (grace_floor,)).fetchall()}
            gone |= {r[0] for r in conn.execute(
                'SELECT model_id FROM model_benchmark_scores WHERE checked_at < ?', (grace_floor,)).fetchall()}
        for mid in gone:
            conn.execute('DELETE FROM model_benchmark_scores WHERE model_id = ?', (mid,))
            conn.execute('DELETE FROM model_catalog WHERE model_id = ?', (mid,))
    return len(gone)


def _ensure_tier_fallbacks_table(conn):
    """init_db() only runs in the __main__ path, not under uvicorn, so a
    table added after the DB was first created must be ensured where it is
    used."""
    conn.execute('''CREATE TABLE IF NOT EXISTS model_tier_fallbacks (
        band TEXT NOT NULL,
        rank INTEGER NOT NULL,
        slug TEXT NOT NULL,
        name TEXT NOT NULL,
        price_per_m REAL NOT NULL,
        chosen_at REAL NOT NULL,
        PRIMARY KEY (band, rank)
    )''')


def _tier_fallback_candidates(band, pool, scores, picked, limit=3):
    """The next-best alternates for a band after `picked`: in-floor
    qualifiers under the winning benchmark first (cheapest first), then the
    cheapest remaining pool members. Excludes the pick; not live-verified."""
    if not picked:
        return []
    seen = {picked['id']}
    out = []
    for m in _best_value_qualifiers_sync(pool, scores or {},
                                         BAND_QUALITY_FLOOR_GAP.get(band)):
        if m['id'] not in seen:
            out.append(m)
            seen.add(m['id'])
        if len(out) >= limit:
            break
    if len(out) < limit:
        # Quality-first fill for the capability bands: an unavailable planning
        # or coding model must not degrade to the cheapest small model just
        # because it was next by price. Cost bands keep price order, which is
        # their whole purpose.
        if scores and band in ('coding', 'high'):
            rest = sorted(pool, key=lambda x: (-scores.get(x['id'], -1e9),
                                               x['price'], x['id']))
        else:
            rest = sorted(pool, key=lambda x: (x['price'], x['id']))
        for m in rest:
            if m['id'] not in seen:
                out.append(m)
                seen.add(m['id'])
            if len(out) >= limit:
                break
    return out


async def refresh_model_tiers():
    models = await asyncio.to_thread(_fetch_openrouter_catalog_sync)
    _sync_model_catalog(models)
    buckets = _bucket_models_by_price(models)
    all_benchmark_rows = get_model_benchmark_scores()
    pools = _band_candidate_pools(models)
    chosen = {}
    fallbacks = {}
    for band, purpose in MODEL_BAND_PURPOSE.items():
        # Aliased bands have no model of their own, and an operator's custom
        # endpoint is a PIN: re-picking it would overwrite the row the board
        # shows, spend a verify probe, and still never be used at call time
        # (_model_tier_slug returns @provider:<band> first).
        if MODEL_BAND_ALIASES.get(band) or _provider_spec_for_band(band):
            continue
        pool = buckets.get(band, [])

        # 'coding', 'high' (planning) and 'sublow' all search EVERY price
        # band's candidates, not just a same-named price bucket -- for
        # coding/high because price bracket says nothing about whether a
        # model can write correct code or decompose a goal, and for sublow
        # because its $0.09 ceiling does the price scoping itself (a
        # capability-shaped band whose bound is a hard cap, not a bracket;
        # there is deliberately no 'sublow' price bucket to carve out of
        # 'low', so the low band's own pool is untouched). Several of the
        # strongest scores on file belong to models priced well under $5.
        # (There is no 'coding' price bucket at all, which is the point:
        # it's a capability band, not a cost band.) 'low' and 'mid' stay
        # scoped to their own price band instead -- their purpose text
        # ("cheap above all else" / "moderate cost is fine") is explicitly
        # cost-conscious, so widening their search would contradict the
        # very thing that makes them distinct, cheaper tiers.
        candidate_pool = pools[band]
        # BAND_BENCHMARK[band] is an ordered metric CHAIN: use the first metric
        # any candidate in this pool is scored on, fall through only when
        # nothing is. Keeps each pick inside one evaluator's scale -- a
        # fallback-only model is never ranked against a primary-scored one.
        picked = None
        cheapest_qual = None
        scores = {}
        for benchmark in BAND_BENCHMARK[band]:
            scores = {r['model_id']: r['score'] for r in all_benchmark_rows if r['benchmark'] == benchmark}
            picked = await _best_value_pick(candidate_pool, scores, BAND_QUALITY_FLOOR_GAP.get(band), band=band)
            if picked is not None:
                # Cheapest in-floor qualifier under the winning benchmark's
                # scores: the baseline the BP-63 audit compares this pick
                # against (a strictly pricier pick inside the parity band is
                # a latency-decided decision; see _attach_latency_audit).
                quals = _best_value_qualifiers_sync(candidate_pool, scores, BAND_QUALITY_FLOOR_GAP.get(band))
                cheapest_qual = quals[0] if quals else None
                break

        if picked is None and not pool:
            continue

        if picked is None:
            # Cap the shortlist Jev actually sees -- a real band can still
            # have 60+ entries; a classifier call doesn't need all of them,
            # just enough real variety to make a genuine choice from.
            shortlist = pool[:25]
            candidates = [{'id': m['id'], 'description': f"{m['name']} -- ${m['price']:.3f}/M tokens"} for m in shortlist]
            try:
                data = await asyncio.to_thread(
                    _call_openrouter_decision_sync, _jev_model(),
                    {'messages': [], 'signals': {}},
                    {'choice': {'type': 'choice', 'instructions': f'Picking the {band}-cost model tier for a small think tank simulation. {purpose}', 'criteria': {c['id']: c['description'] for c in candidates}}},
                )
                pick_id = _jev_choice(data)[0]
            except Exception:
                pick_id = None
            ranked = [m for m in shortlist if m['id'] == pick_id] + [m for m in shortlist if m['id'] != pick_id]

            # Verify the pick actually works before trusting it -- fall
            # through to the next candidate (up to 3 real attempts) rather
            # than caching something that 404s the first time an agent
            # actually needs to talk.
            for candidate in ranked[:3]:
                if await asyncio.to_thread(_verify_model_works_sync, candidate['id']):
                    picked = candidate
                    break
        if picked is None:
            print(f'[model-tiers] no working candidate found for {band} band after 3 attempts')
            continue
        if picked is not None:
            _attach_latency_audit(picked, cheapest_qual)
        chosen[band] = picked
        fallbacks[band] = _tier_fallback_candidates(
            band, candidate_pool, scores if picked else {}, picked)

    # Vision is a separate axis entirely, not part of the low/mid/high cost
    # ladder -- it's gated on a real, catalog-verifiable capability
    # (architecture.input_modalities includes 'image') that a coding/chat/
    # judgment benchmark says nothing about, so it needs its own candidate
    # pool. not just "cheapest that technically
    # supports images" either -- same real MMMU vision-benchmark grounding
    # and same value policy (_best_value_pick) as every other band, so a
    # vision model that's cheap but bad at actually reading a screenshot
    # doesn't win just for being cheap.
    vision_pool = pools['vision']
    vision_scores = {r['model_id']: r['score'] for r in all_benchmark_rows if r['benchmark'] == 'MMMU'}
    vision_pick = await _best_value_pick(vision_pool, vision_scores, band='vision')
    if vision_pick is None and vision_pool:
        # Same fallback the cost bands use: MMMU is player-entered (the
        # catalog carries no image-comprehension metric), so when no MMMU
        # score is on file the score-grounded pick is empty and the band
        # must still resolve -- pick from the vision-capable shortlist with
        # the Jev classifier and live-verify (up to 3 attempts) rather than
        # leaving the tier stale. Once the player enters real MMMU scores,
        # _best_value_pick takes over and the score-grounded policy is
        # restored.
        shortlist = sorted(vision_pool, key=lambda m: m['price'])[:25]
        candidates = [{'id': m['id'], 'description': f"{m['name']} -- ${m['price']:.3f}/M tokens"} for m in shortlist]
        try:
            data = await asyncio.to_thread(
                _call_openrouter_decision_sync, _jev_model(),
                {'messages': [], 'signals': {}},
                {'choice': {'type': 'choice', 'instructions': 'Picking the vision model tier for a small think tank simulation: a model that can reliably read a real screenshot (a player upload or a live-URL screenshot) as text. Reading what is actually in the image is the whole job -- it must describe the visible content, not just name the file. The candidate that best fits that job wins.', 'criteria': {c['id']: c['description'] for c in candidates}}},
            )
            pick_id = _jev_choice(data)[0]
        except Exception:
            pick_id = None
        ranked = [m for m in shortlist if m['id'] == pick_id] + [m for m in shortlist if m['id'] != pick_id]
        for candidate in ranked[:3]:
            if await asyncio.to_thread(_verify_model_works_sync, candidate['id']):
                vision_pick = candidate
                break
    if vision_pick:
        _vquals = _best_value_qualifiers_sync(vision_pool, vision_scores)
        _attach_latency_audit(vision_pick, _vquals[0] if _vquals else None)
        chosen['vision'] = vision_pick
    else:
        print('[model-tiers] no working vision-capable candidate found after fallback')

    with _db() as conn:
        _ensure_tier_fallbacks_table(conn)
        for band, m in chosen.items():
            conn.execute(
                'INSERT INTO model_tiers (band, slug, name, price_per_m, chosen_at) VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(band) DO UPDATE SET slug=excluded.slug, name=excluded.name, price_per_m=excluded.price_per_m, chosen_at=excluded.chosen_at',
                (band, m['id'], m['name'], m['price'], time.time()),
            )
            conn.execute('DELETE FROM model_tier_fallbacks WHERE band = ?', (band,))
            for _rank, _fb in enumerate(fallbacks.get(band, []), 1):
                conn.execute(
                    'INSERT INTO model_tier_fallbacks (band, rank, slug, name, price_per_m, chosen_at) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (band, _rank, _fb['id'], _fb['name'], _fb['price'], time.time()),
                )
            # Daily price/score log: look up the benchmark score the pick was
            # grounded on (first metric in the band's chain that has a score
            # for this model) and write today's row. Same-day re-runs update
            # the row in place (PK log_date+band) so it always reflects the
            # latest pick -- EXCEPT logged_at, which marks WHEN THE CURRENT
            # PICK FIRST APPEARED, not the last write: a same-slug re-run keeps
            # the original timestamp so the row's age is the value window's age
            # (read by get_tier_value_windows). A slug change resets it.
            _score, _bench = None, None
            for _b in BAND_BENCHMARK.get(band, ()):
                for _r in all_benchmark_rows:
                    if _r['model_id'] == m['id'] and _r['benchmark'] == _b:
                        _score, _bench = _r['score'], _b
                        break
                if _score is not None:
                    break
            # BP-63 audit columns ride the pick's own audit stamp: latency
            # median + sample count only when a usable median exists, and
            # speed_picked only when the pick is strictly pricier than the
            # cheapest in-floor qualifier (i.e. speed decided the order).
            _lat_ms, _lat_n, _speed = m.get('_latencyAudit') or (None, None, False)
            conn.execute(
                'INSERT INTO tier_daily_log (log_date, band, slug, name, price_per_m, benchmark, score, logged_at, '
                'median_latency_ms, latency_samples, speed_picked) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(log_date, band) DO UPDATE SET slug=excluded.slug, name=excluded.name, '
                'price_per_m=excluded.price_per_m, benchmark=excluded.benchmark, score=excluded.score, '
                'median_latency_ms=excluded.median_latency_ms, latency_samples=excluded.latency_samples, '
                'speed_picked=excluded.speed_picked, '
                'logged_at=CASE WHEN tier_daily_log.slug = excluded.slug '
                'THEN tier_daily_log.logged_at ELSE excluded.logged_at END',
                (time.strftime('%Y-%m-%d', time.gmtime()), band, m['id'], m['name'], m['price'], _bench, _score, time.time(),
                 _lat_ms, _lat_n, int(bool(_speed))),
            )
    log_action(None, 'model_tiers_refreshed', {band: m['id'] for band, m in chosen.items()})
    return chosen


def get_cached_model_tiers():
    with _db() as conn:
        rows = conn.execute('SELECT band, slug, name, price_per_m FROM model_tiers').fetchall()
    return {band: {'slug': slug, 'name': name, 'price': price} for band, slug, name, price in rows}


def get_tier_daily_log(band=None, days=30):
    """Read the daily tier price/score history. Optional band filter;
    returns at most `days` days back (most recent first). Each row:
    {log_date, band, slug, name, price_per_m, benchmark, score}."""
    with _db() as conn:
        if band:
            rows = conn.execute(
                'SELECT log_date, band, slug, name, price_per_m, benchmark, score '
                'FROM tier_daily_log WHERE band = ? ORDER BY log_date DESC LIMIT ?',
                (band, days),
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT log_date, band, slug, name, price_per_m, benchmark, score '
                'FROM tier_daily_log ORDER BY log_date DESC LIMIT ?',
                (days * 6,),  # ~6 bands per day
            ).fetchall()
    return [
        {'log_date': r[0], 'band': r[1], 'slug': r[2], 'name': r[3],
         'price_per_m': r[4], 'benchmark': r[5], 'score': r[6]}
        for r in rows
    ]


def get_catalog_price_history(model_id=None, days=30):
    """Read the full-catalog daily price history. Optional model_id filter;
    returns at most `days` days back (most recent first). Each row:
    {log_date, model_id, price_per_m}."""
    with _db() as conn:
        if model_id:
            rows = conn.execute(
                'SELECT log_date, model_id, price_per_m FROM catalog_price_daily '
                'WHERE model_id = ? ORDER BY log_date DESC LIMIT ?',
                (model_id, days),
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT log_date, model_id, price_per_m FROM catalog_price_daily '
                'ORDER BY log_date DESC, price_per_m ASC LIMIT ?',
                (days * 50,),
            ).fetchall()
    return [{'log_date': r[0], 'model_id': r[1], 'price_per_m': r[2]} for r in rows]


def tier_value_ratios():
    """Per-band value ratio: today's (score / price_per_m) divided by the
    band's trailing 7-day median value (excluding today). >1.0 means today's
    pick is a stronger value than usual (cheap day for that band); <1.0
    means weaker (expensive day). Bands with fewer than 2 history days
    return 1.0 (neutral -- not enough data to judge). Read by the sim's
    assignment loop to throttle non-urgent work in contracted bands."""
    today = time.strftime('%Y-%m-%d', time.gmtime())
    with _db() as conn:
        rows = conn.execute(
            'SELECT band, log_date, price_per_m, score FROM tier_daily_log '
            'WHERE score IS NOT NULL AND price_per_m > 0 '
            'ORDER BY log_date DESC'
        ).fetchall()
    by_band = {}
    for band, log_date, price, score in rows:
        by_band.setdefault(band, []).append((log_date, price, score))
    ratios = {}
    for band, entries in by_band.items():
        today_vals = [(p, s) for d, p, s in entries if d == today]
        history = [(p, s) for d, p, s in entries if d != today]
        if not today_vals or len(history) < 2:
            ratios[band] = 1.0
            continue
        today_price, today_score = today_vals[0]
        today_value = today_score / today_price
        hist_values = sorted(s / p for p, s in history)
        n = len(hist_values)
        median = hist_values[n // 2] if n % 2 else (hist_values[n // 2 - 1] + hist_values[n // 2]) / 2
        ratios[band] = today_value / median if median > 0 else 1.0
    return ratios


def get_tier_value_windows():
    """Per-band value snapshot for the operator/player: the band's current
    score-per-dollar ratio vs its trailing 7-day median (tier_value_ratios),
    plus HOW LONG the current pick has been held (tier_daily_log.logged_at for
    today's row, which only advances when the slug changes). A ratio >= 1.3 on
    a freshly-changed pick is a cheap-value window the tank is riding. The
    operator would NOT otherwise see this -- only the resulting pick. Read-only."""
    ratios = tier_value_ratios()
    today = time.strftime('%Y-%m-%d', time.gmtime())
    now = time.time()
    with _db() as conn:
        rows = conn.execute(
            'SELECT band, slug, price_per_m, logged_at FROM tier_daily_log WHERE log_date = ?',
            (today,)).fetchall()
    windows = {}
    for band, slug, price, logged_at in rows:
        since = logged_at or now
        windows[band] = {
            'slug': slug,
            'price_per_m': price,
            'sinceTs': since,
            'sinceAgeS': max(0.0, now - since),
            'valueRatio': ratios.get(band, 1.0),
        }
    return {'ratios': ratios, 'windows': windows}


# Read-only board/status GETs must NOT wake a dormant tank: the browser polls
# state/health/sim continuously, and an open tab would otherwise defeat
# sleep-not-die dormancy (each poll would flip it awake). Pure telemetry
# (device check-in) is likewise non-waking -- it must land while asleep. Waking
# is reserved for anything that produces work, writes state, or is an explicit
# player command (non-GET), plus /api/escalation/resolve -- a GET that MUTATES
# (the player approving a pending escalation must wake the tank so the grant
# actually lands and the egress proxy refreshes). The one exception: pure
# telemetry writes (device check-in) are exempted separately, below.
_NON_WAKING_READS_BASE = {
    '/api/state', '/api/health', '/api/health/alerts', '/api/sim/status',
    '/api/sim/agents', '/api/activity', '/api/activity/summary', '/api/decisions',
    '/api/reviews', '/api/backlog', '/api/rooms', '/api/teams', '/api/library',
    '/api/library/search', '/api/library/file', '/api/library/passport',
    '/api/agent-files', '/api/agent-files/read', '/api/model-tiers',
    '/api/model-benchmark-scores', '/api/jev/model', '/api/jev/calibration',
    '/api/intent/sprints', '/api/intent/issues', '/api/intent/products',
    '/api/intent/wiki', '/api/passport/verify', '/api/player-inbox',
    '/api/pipelines', '/api/keys/credentials', '/api/sandbox-backups',
    '/api/shadow', '/', '/index.html',
}
# Kernel-safe set plus any operator additions from endpoint_policy.json.
_NON_WAKING_READS = set(_NON_WAKING_READS_BASE) | NON_WAKING_READS_EXTRA
# Pure telemetry that may WRITE while dormant WITHOUT waking the tank. Kept
# separate from the GET-only _NON_WAKING_READS and checked by _should_wake
# BEFORE the method test: the device check-in is a POST, but a phone Shortcut
# must never keep the think tank awake. Check-ins still land while asleep
# (the endpoint itself is not gated on _dormant()).
_NON_WAKING_WRITES_BASE = {'/api/device/checkin', '/a2a'}
_NON_WAKING_WRITES = set(_NON_WAKING_WRITES_BASE) | NON_WAKING_WRITES_EXTRA


def _should_wake(request):
    """True when a request should flip a dormant tank back awake: any write
    (non-GET) outside the telemetry set, or any GET outside the read-only
    board/status/telemetry set."""
    path = request.url.path
    if path in _NON_WAKING_READS and request.method == 'GET':
        return False
    if path in _NON_WAKING_WRITES:
        return False
    if request.method != 'GET':
        return True
    if path.startswith('/api/escalation/') and path != '/api/escalation/resolve':
        return False
    return True


def _selfedit_remediation_exempt(request):
    """True when this request is in-window remediation traffic that must
    NOT wake a dormant tank (see no_store). Narrow on purpose: only
    /api/selfedit/* paths, only while a remediation session is actually
    open (fired window + failing rows), and only when the caller proves a
    real agent key for the identity it names."""
    if not request.url.path.startswith('/api/selfedit/'):
        return False
    try:
        active = _selfedit.remediation_window()
    except Exception:
        return False
    if not active or request.headers.get('X-Dormancy-Window') != active:
        return False
    agent_id = request.headers.get('X-Dormancy-Agent') or ''
    if not agent_id or not verify_agent_key(
            agent_id, request.headers.get('X-Agent-Key')):
        return False
    log_action(agent_id, 'selfedit_remediation_traffic',
               {'window': active, 'path': request.url.path}, authorized=True)
    return True


def _selfedit_fix_headers(agent_id):
    """Slot headers the executor attaches to selfedit traffic while an
    in-window remediation session is open; the wake exemption
    (_selfedit_remediation_exempt) validates the same pair. Without an
    open session these calls still work, they just wake the tank if it is
    dormant like any other write."""
    try:
        active = _selfedit.remediation_window()
    except Exception:
        active = None
    if not active:
        return None
    return {'X-Dormancy-Window': active, 'X-Dormancy-Agent': agent_id or ''}


def _selfedit_validation_exempt(request):
    """True when this request is change-request VALIDATION traffic: a fired
    window holds for its validator's push decision, and the validator MUST be
    able to reach the selfedit endpoints without waking the tank (a wake
    would not undo the applied edits, but it disturbs the quiet slot the
    player approved). Narrow on purpose: /api/selfedit/* paths only, only
    while a window actually awaits validation, only the window's own
    validator, with a verified agent key."""
    if not request.url.path.startswith('/api/selfedit/'):
        return False
    try:
        active = _selfedit.validation_window()
    except Exception:
        return False
    if not active or request.headers.get('X-Dormancy-Window') != active:
        return False
    agent_id = request.headers.get('X-Dormancy-Agent') or ''
    if not agent_id or not verify_agent_key(
            agent_id, request.headers.get('X-Agent-Key')):
        return False
    if agent_id != _selfedit.window_validator(active):
        return False
    log_action(agent_id, 'selfedit_validation_traffic',
               {'window': active, 'path': request.url.path}, authorized=True)
    return True


def _selfedit_validate_headers(agent_id):
    """Slot headers for validator traffic while a fired window holds for
    validation; _selfedit_validation_exempt validates the same pair. Without
    a holding window these calls work normally (and wake the tank)."""
    try:
        active = _selfedit.validation_window()
    except Exception:
        active = None
    if not active:
        return None
    return {'X-Dormancy-Window': active, 'X-Dormancy-Agent': agent_id or ''}


@app.middleware('http')
async def no_store(request: Request, call_next):
    # Local dev server for actively-edited files -- a cached stale
    # response is strictly worse than no caching at all. Bit us hard
    # mid-session already: a browser served a stale script from disk
    # cache without even a network round-trip, surviving a hard
    # navigation and a fresh tab.
    global _LAST_REQUEST_TIME
    _LAST_REQUEST_TIME = time.time()
    # Wake-on-request (sleep-not-die): the first request to reach a DORMANT
    # server flips the think tank back awake BEFORE the handler runs, so the
    # admin/browser request that resumes activity does so on an already-warm
    # server. Read-only board/status GETs and pure telemetry do NOT wake
    # (see _NON_WAKING_READS) -- an open tab must not defeat dormancy.
    if _dormant() and _should_wake(request):
        if (_selfedit_remediation_exempt(request)
                or _selfedit_validation_exempt(request)):
            # An open change-window remediation session: the responsible
            # agents MUST be able to reach the fix endpoints (and read their
            # mail) without waking the tank, since a wake exits the quiet
            # window their fixes are happening inside. Narrow by path,
            # remediation state, and verified agent identity.
            # Same for a fired window that HOLDS for its validator's push
            # decision (two-agent change requests): the validator must
            # review and approve/deny without disturbing the quiet slot
            # the player approved.
            pass
        else:
            _set_dormant(False)
            print(f'[idle] wake request from {request.client.host if request.client else "?"} -- think tank resumed', flush=True)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    return response


# DENY-BY-DEFAULT auth. Every route requires a real logged-in session (or a
# valid agent key) UNLESS it appears in this explicit public list. The old
# shape was the inverse -- a protected-prefix allowlist -- and it decayed the
# way those always do: a NEW route was public until someone remembered to add
# it, and forgetting was silent because tests inject sessions, so nothing
# failed. Here forgetting is loud: the route just 401s until it is deliberately
# classified, and tests/test_auth_surface.py pins the list below so an
# accidental public addition fails CI.
#
# What stays public, and why:
#   - the login surface ('/', '/index.html', '/login', '/logout') -- must
#     work before any session exists
#   - open UI reads the unauthenticated shell uses (rooms, teams, backlog,
#     templates, avatars)
#   - routes that enforce their OWN credential in the handler: session-only
#     (_require_player_session / verify_session), operator gates
#     (_operator_gate), agent-key checks, or their own token header (device
#     key, SMS forward secret, sublow/jev grant, the per-escalation resolve
#     token). Their handler gate IS the access control; the middleware must
#     not double-gate or break them, so they are listed here.
#
# Everything else is protected by default with no listing required: state,
# log, decide, browse, execute, curl, pipeline, library, intent, keys,
# player-email, player-inbox, budget, health, sim, model tiers, tier value
# windows, escalation status, review/escalate, sandbox-*, evidence, coverage,
# policy, and the rest. /api/player-email/credential is the historical
# example of why the default must be CLOSED rather than open: its own handler
# only rejects a caller who SELF-IDENTIFIES as an agent, so under the old
# missing-prefix shape a request carrying no credentials at all reached it
# and silently overwrote the player's real Gmail app-password.
AUTH_PUBLIC_PATHS = (
    # NOTE: the login surface '/' and '/index.html' are NOT listed here --
    # they are handled by exact match inside _is_public_path, because a
    # startswith entry of '/' would match every path and disable the gate.
    '/login', '/logout',                                # login surface
    '/api/rooms', '/api/teams', '/api/backlog',         # open UI reads
    '/api/templates', '/api/avatar',
    '/api/escalation/resolve',                          # its own per-escalation token gate
    '/api/chat',                                        # session OR agent key, in-handler
    '/api/mail', '/api/reports', '/api/download',
    '/api/file-grants', '/api/app-grants',
    '/api/device/checkin',                              # X-Device-Key
    '/api/webhook/',                                    # X-Webhook-Key (external watchers, any channel)
    '/api/task-budget', '/api/budget', '/api/env/reload',
    '/api/ais', '/api/market', '/api/rss', '/api/ws',
    '/api/data-sources', '/api/credentials',
    '/api/sms',                                         # incl. inbound (X-SMS-Forward-Secret)
    '/api/experiments',                                 # incl. inference (grant token)
    '/api/rule-proposals', '/api/success-proposals',
    # A2A Agent Card is public metadata (no secrets); the /a2a endpoint itself
    # still requires a player session or a director/admin key.
    '/.well-known/agent-card.json', '/.well-known/agent.json',
    # Enterprise page shell: public HTML only. Its data endpoints stay
    # operator-only and enforce auth in their own handlers.
    '/admin/enterprise',
)
# Static assets the login shell needs before any session exists. This
# extension allowlist is what stops world/*.py, grants.json, api_services.json,
# sandbox_egress_grants.json, and the whole tests/ tree from being fetched by
# an anonymous GET -- the same class of exposure that once served
# .secret_keys/edek.key unauthenticated (the key was moved instead of the
# mount being closed; this closes the class). A path that is not a listed
# asset extension falls through to the session requirement.
AUTH_PUBLIC_STATIC_EXTENSIONS = ('.js', '.css', '.html', '.png', '.jpg', '.jpeg',
                                 '.gif', '.svg', '.ico', '.webp', '.woff', '.woff2', '.map')


def _is_public_path(path):
    # Exact match first: '/' must never be a startswith entry (it would match
    # every path and disable the whole gate). Prefix entries below are chosen
    # so over-matching only ever lands on routes that self-guard in-handler
    # (e.g. '/api/rooms' also covers '/api/rooms/{room}/purpose', which
    # requires an agent key in its own handler).
    if path in ('/', '/index.html'):
        return True
    if any(path.startswith(p) for p in AUTH_PUBLIC_PATHS):
        return True
    ext = os.path.splitext(urllib.parse.urlparse(path).path)[1].lower()
    return ext in AUTH_PUBLIC_STATIC_EXTENSIONS


def _valid_agent_key_presented(presented_key):
    # Reverse lookup: does this bearer key belong to any real agent? The
    # server's own content executors (and other agent-loopback paths) call
    # protected endpoints with ONLY an X-Agent-Key -- no session cookie, since
    # a loopback isn't a browser. Those must authenticate as the agent rather
    # than be bounced here. A key that matches nothing stays rejected. This is
    # the same "a valid agent key IS a credential" rule the /api/chat handler
    # already applies, just centralized for every protected route.
    if not presented_key:
        return False
    with _db() as conn:
        row = conn.execute('SELECT agent_id FROM agent_keys WHERE secret_key = ?',
                           (presented_key,)).fetchone()
    return row is not None


def _valid_theo_ask_credential(request):
    # A granted Theo key (ask 3) is a bearer credential, but a
    # SCOPED one: it opens /api/theo/ask and no other door. The
    # registry verifies hash, revocation, and expiry together --
    # the same check the ask route itself runs -- so a revoked or
    # expired key stops passing the door too. Every other protected
    # route still requires a player session or a real agent key:
    # a brand bot holding a Theo key cannot read the rest of the
    # think tank, and a Theo key is never accepted as a sim agent
    # identity anywhere else.
    if request.url.path != '/api/theo/ask':
        return False
    presented = request.headers.get('X-Agent-Key')
    if not presented or _KEY_REGISTRY is None:
        return False
    return _KEY_REGISTRY.verify(presented) is not None


# Player-authored work trust (Fix B). Three endpoints -- /api/browse,
# /api/browser-act, /api/download -- let a host that a PLAYER-authored task
# explicitly named skip the Jev gate, the same way the domain allowlist does.
# That trust used to ride in a caller-set body boolean (`workContextTrusted`),
# so any agent could set it and launder its OWN authored text (naming any URL)
# past Jev. The trust anchor is now server-side: the executor that read
# `task.playerAuthored` off the STORED task mints a one-time nonce here and
# hands it to the endpoint, which redeems it against this same in-memory
# ledger. The agent's own model call never sees or mints the nonce.
_WORK_TRUST_TTL_MS = 30 * 60 * 1000
_work_trust_nonces: dict = {}  # nonce -> expires_at (ms)


def _mint_work_trust_nonce():
    """Server-side: prove a work-context bypass is backed by a stored
    player-authored task. Only the executors (server code) call this, after
    reading task['playerAuthored'] from the real task -- never an agent's
    request body. Returns an opaque nonce redeemable exactly once."""
    nonce = secrets.token_hex(16)
    now = int(time.time() * 1000)
    # opportunistic GC so the ledger can't grow without bound
    for k in [k for k, exp in _work_trust_nonces.items() if exp <= now]:
        _work_trust_nonces.pop(k, None)
    _work_trust_nonces[nonce] = now + _WORK_TRUST_TTL_MS
    return nonce


def _redeem_work_trust_nonce(nonce):
    """True (and consume) when the nonce is a live, server-minted token for a
    player-authored task. Anything absent, expired, or replayed is False."""
    if not nonce or not isinstance(nonce, str):
        return False
    exp = _work_trust_nonces.pop(nonce, None)
    return bool(exp and exp > int(time.time() * 1000))


def _work_trust_from_body(body):
    """Resolve the work-context trust for a request body. Only a redeemed
    server-minted nonce can be trusted -- the legacy `workContextTrusted`
    boolean is ignored (kept in the payload for older callers, but it no
    longer grants the bypass)."""
    return _redeem_work_trust_nonce(body.get('workTrustNonce'))


def _a2a_bearer_agent(request):
    """A2A clients send `Authorization: Bearer <agent key>`; accept that only
    on /a2a so the rest of the surface keeps its existing auth rules."""
    if request.url.path != '/a2a':
        return False
    auth = (request.headers.get('Authorization') or '').strip()
    if not auth.lower().startswith('bearer '):
        return False
    return _valid_agent_key_presented(auth[7:].strip())


@app.middleware('http')
async def require_login(request: Request, call_next):
    if not _is_public_path(request.url.path):
        if not verify_session(request.cookies.get(SESSION_COOKIE_NAME)) \
                and not _valid_agent_key_presented(request.headers.get('X-Agent-Key')) \
                and not _a2a_bearer_agent(request) \
                and not _valid_theo_ask_credential(request):
            return JSONResponse({'error': 'Unauthorized -- please log in'}, status_code=401)
    return await call_next(request)


@app.post('/save')
async def save_collision(request: Request):
    try:
        data = await request.json()
        assert 'grid' in data and 'cols' in data and 'rows' in data
    except Exception as e:
        return PlainTextResponse(str(e), status_code=400)
    with open(os.path.join(ROOT, 'collision_grid.json'), 'w') as f:
        json.dump(data, f)
    log_action(None, 'save_collision_grid')
    return PlainTextResponse('saved')


@app.post('/save-doors')
async def save_doors(request: Request):
    try:
        data = await request.json()
        for building, d in data.items():
            assert {'x', 'y', 'w', 'h'} <= d.keys()
    except Exception as e:
        return PlainTextResponse(str(e), status_code=400)
    with open(os.path.join(ROOT, 'door_triggers.json'), 'w') as f:
        json.dump(data, f, indent=2)
    log_action(None, 'save_door_triggers')
    return PlainTextResponse('saved')


@app.get('/api/state')
async def get_state(request: Request):
    # A genuinely empty database gets the server-owned default roster
    # seeded here (see _seed_default_roster) so the client never has to
    # generate or hardcode a single agent name -- cold start and warm
    # start both read names from the DB.
    data = get_state_from_db(archive_merge='alias')
    if data is None:
        _seed_default_roster()
        data = get_state_from_db(archive_merge='alias')
    if data is None:
        return JSONResponse(None)
    # Close the race a per-tick-only heal leaves open: a stale client tab's
    # autosave can re-break identity fields in the window between two sim
    # ticks, and a NEW page load landing in exactly that window would adopt
    # the broken snapshot into its own in-memory AGENTS for its whole
    # session (a full re-GET never happens again after boot -- only position
    # deltas do). Healing synchronously on every read guarantees no client
    # ever sees a broken record, regardless of tick timing.
    if _heal_agent_identity(data):
        save_state_to_db(data)
    # Per-agent keys ride along with state ONLY for the player's browser (a
    # real session), where the client uses them for attribution tags on
    # browse/execute/pipeline calls. An agent-key-authenticated caller (a
    # content executor loopback) must NOT be handed every other agent's key:
    # those keys gate key-authenticated endpoints, and a hostile agent would
    # read all of them, mint its own, and impersonate anyone.
    if verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        agent_keys = {}
        for def_ in data.get('agentRoster', []):
            agent_id = def_.get('id')
            if agent_id:
                agent_keys[agent_id] = get_or_create_agent_key(agent_id)
        data['agentKeys'] = agent_keys
    return JSONResponse(data)
