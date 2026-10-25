

def _director_second_opinion(agent_id, action, noun, target, purpose,
                             confidence, trace_id=None):
    """Ask the senior director to decide a low-confidence JEV allow.

    A low-confidence allow is not a block: it is uncertainty. The director
    gets the same concrete command/action and purpose and makes the call.
    Returns True only for a confident approve; every other shape fails closed.
    """
    try:
        state = get_state_from_db() or {}
    except Exception:
        state = {}
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])}
    senior_id = _senior_most_director_id(state)
    admin_id = _admin_agent_id(state)
    immediate_id = (roster.get(agent_id) or {}).get('director')
    chain = []
    for cid in (immediate_id, senior_id, admin_id):
        if cid and cid != agent_id and cid not in chain and cid in roster:
            chain.append(cid)
    if not chain:
        return False
    threshold = _effective_safety_confidence()
    criteria = {
        'approve': 'Ordinary, legitimate, in-scope work; the low JEV confidence was caution, not a concrete risk.',
        'deny': 'Unsafe, destructive, credential/secret access or exfiltration, out of scope, or not justified.',
    }
    for level, reviewer_id in enumerate(chain):
        reviewer = roster.get(reviewer_id) or {}
        name = reviewer.get('name') or reviewer_id
        role = ('chief of staff' if reviewer_id == admin_id else
                'senior director' if reviewer_id == senior_id else 'director')
        instr = (
            f'You are {name}, the {role} of the AI think tank. '
            f'An agent asked to run this {noun}: {target}\n'
            f'Stated purpose: {purpose or "not given"}\n'
            f'JEV returned allow at low confidence ({confidence:.2f}, below '
            f'the {threshold:.2f} safety threshold). Decide whether the action '
            'should go ahead. Approve only if it is ordinary, in-scope work and '
            'the low confidence was caution rather than a real risk. Deny if it '
            'is unsafe, destructive, exfiltrating, out of scope, or unjustified.'
        )
        decision, reviewer_conf, _cost = _jev_quorum_choice_sync(instr, criteria)
        # A confident deny is final. A LOW-confidence deny is not a decision
        # yet -- it means this reviewer also cannot tell. Continue up the
        # chain (Nora -> Theo), exactly like a low-confidence approve. Before
        # this, a hesitant Nora ended the chain and the chief of staff never
        # saw the request.
        if decision == 'deny' and reviewer_conf >= threshold:
            log_action(reviewer_id, 'jev_chain_denied',
                       {'agent': agent_id, 'action': action, 'target': target,
                        'purpose': purpose, 'jevConfidence': confidence,
                        'reviewerConfidence': reviewer_conf, 'level': level,
                        'traceId': trace_id}, authorized=False)
            return False
        if decision == 'approve' and reviewer_conf >= threshold:
            log_action(reviewer_id, 'jev_chain_approved',
                       {'agent': agent_id, 'action': action, 'target': target,
                        'purpose': purpose, 'jevConfidence': confidence,
                        'reviewerConfidence': reviewer_conf, 'level': level,
                        'traceId': trace_id}, authorized=False)
            return True
        log_action(reviewer_id, 'jev_chain_unsure',
                   {'agent': agent_id, 'action': action, 'target': target,
                    'purpose': purpose, 'jevConfidence': confidence,
                    'reviewerConfidence': reviewer_conf,
                    'decision': decision, 'level': level,
                    'traceId': trace_id}, authorized=False)
    # The whole chain could not decide: this is the one case that reaches
    # the owner, not an automatic block.
    try:
        esc_id = create_escalation(
            'owner safety decision',
            f'Agent {agent_id} needs a decision on this {noun}: {target}\n'
            f'Stated purpose: {purpose or "not given"}\n'
            f'JEV confidence {confidence:.2f} was below the {threshold:.2f} '
            'safety threshold, and the director, senior director, and chief '
            'of staff all declined or could not decide.',
            what_checked='JEV plus the full director -> senior director -> chief of staff chain',
            look_first=target)
        log_action('player', 'jev_chain_owner_escalation',
                   {'escalationId': esc_id, 'agent': agent_id,
                    'action': action, 'target': target,
                    'purpose': purpose, 'traceId': trace_id}, authorized=False)
    except Exception as exc:
        log_action(agent_id, 'jev_chain_owner_escalation_failed',
                   {'error': str(exc)[:200], 'traceId': trace_id},
                   authorized=False)
    return False


def _jev_safety_gate(agent_id, action, noun, target, purpose, decision, confidence, cost, authorized, trace_id=None):
    # Shared low-confidence handling for the Jev safety gates (browse,
    # download, curl/execute, sandbox download/save, temp access). A
    # confident allow passes through; a LOW-confidence allow is "unsure" --
    # escalated to a human rather than acted on, which is Jev's whole reason
    # to exist. Any non-allow is just a normal block. Returns True if the
    # request should be allowed, False if it was blocked (either firmly or
    # because we escalated the uncertainty). `trace_id` is
    # threaded from the quorum call that produced this decision so the gate's
    # audit row links back to the exact decision_tape entry(s) it acted on.
    if decision != 'allow' and decision != 'approve':
        log_action(agent_id, action, {'target': target, 'purpose': purpose, 'decision': 'blocked', 'confidence': confidence, 'cost': cost, 'reason': 'jev: ' + (decision or 'classifier unavailable, failed closed')}, authorized=authorized, trace_id=trace_id)
        return False
    if confidence < _effective_safety_confidence():
        # Low-confidence allow is uncertainty, not a block. The senior
        # director gets the decision; if the director also cannot approve
        # confidently, fail closed exactly as before.
        if _director_second_opinion(agent_id, action, noun, target, purpose,
                                    confidence, trace_id):
            log_action(agent_id, action, {'target': target, 'purpose': purpose, 'decision': 'allowed_by_director', 'confidence': confidence, 'cost': cost, 'reason': 'jev low confidence; director approved'}, authorized=authorized, trace_id=trace_id)
            return True
        log_action(agent_id, action, {'target': target, 'purpose': purpose, 'decision': 'blocked_unsure', 'confidence': confidence, 'cost': cost, 'reason': 'jev low confidence; director did not approve'}, authorized=authorized, trace_id=trace_id)
        return False
    # Confident allow -- passes. Log it so cost/confidence are visible in the
    # activity feed (Phase 2c: "log cost + confidence per decision").
    log_action(agent_id, action, {'target': target, 'purpose': purpose, 'decision': 'allowed', 'confidence': confidence, 'cost': cost, 'reason': 'jev allow'}, authorized=authorized, trace_id=trace_id)
    return True


async def _jev_re_gate_redirect_final(agent_id, action, noun, approved_url, final_url, purpose,
                                      work_context, work_context_trusted, authorized, trace_id=None):
    # Closing a REAL gap confirmed by code reading: every fetch path
    # (_fetch_page_sync, _fetch_rendered_page_sync, _curl_request_sync,
    # _api_request_sync, _download_file_sync) follows redirects and re-checks
    # the FINAL host for NETWORK safety only (_is_safe_public_host) -- Jev's
    # content classification judges the ORIGINAL url, never the destination a
    # redirect chain lands on. A short link (bit.ly/... or any URL whose
    # location header points elsewhere) is therefore a laundering vector: Jev
    # approves the link's host + the agent's stated purpose, then the redirect
    # silently delivers content from a host Jev never judged -- exactly the
    # shape that could end with agents reading inappropriate content (CSAM
    # among the blocked categories) off an innocently-approved shortener.
    #
    # This re-runs the SAME content gate on the FINAL url after the redirect
    # resolves, and fails closed: a blocked (or unsure/escalated) final-host
    # decision denies the whole fetch, so no content from an unjudged host
    # reaches the agent. Same-host redirects (http->https, path moves) skip
    # the extra round trip -- the host Jev judged is unchanged. A redirect to
    # a host the PLAYER vetted (allowlist, or explicitly named in a
    # player-authored task's work) skips it too -- the player vetted that
    # destination directly by writing it into the assignment.
    try:
        approved_host = (urllib.parse.urlparse(approved_url).hostname or '').lower()
        final_host = (urllib.parse.urlparse(final_url).hostname or '').lower()
    except Exception:
        return True, None
    if not final_host or final_host == approved_host:
        return True, None
    if _is_allowlisted_host(final_host) or _is_standing_safe_host(final_host) or (work_context_trusted and _is_work_context_host(work_context, final_host)):
        return True, None
    criteria = {
        'allow': 'The URL and stated purpose look like ordinary, legal browsing (reference material, news, weather, general research, public information).'
                 + (' A URL that is clearly part of, or relevant to, the agent\'s CURRENT assigned work is allowed, provided it does not fall into any blocked category.' if work_context else ''),
        'block': 'The URL, domain, or stated purpose suggests: ' + '; '.join(BROWSE_BLOCK_CATEGORIES) + '. A blocked category ALWAYS blocks: relevance to the assigned work or a stated purpose never overrides it -- an agent can describe its work however it likes, and that must not launder an inappropriate site.',
    }
    instructions = (
        f'An in-game agent requested {approved_url}, which REDIRECTED to a different host: {final_url}\n'
        f'Stated reason: {purpose or "not given"}\n'
    )
    if work_context:
        instructions += f'The agent is currently working on this assigned task: {work_context[:600]}\n'
    instructions += 'Decide allow or block based on the FINAL URL/domain (the redirect destination), the stated purpose, and the work it serves. If the URL falls into any blocked category, you MUST block it regardless of work relevance or stated purpose.'
    decision, confidence, cost, redirect_trace_id = await _jev_quorum_decision(instructions, criteria)
    if not _jev_safety_gate(agent_id, action, noun, final_url, purpose, decision, confidence, cost, authorized, redirect_trace_id):
        return False, 'The site this URL redirected to was not approved for a think tank agent to visit.'
    return True, None


# Model tiers are chosen by Jev from OpenRouter's real, current catalog: this
# file does the price bucketing in plain arithmetic and hands Jev a pre-filtered
# shortlist per band. The bands themselves -- price ranges, the job each band
# does, the benchmark chain backing each pick, and the tier-gate criteria -- are
# operator data in world/model_bands.json (config.py), so prices and benchmark
# names can be re-tuned without editing this kernel. The benchmark metric
# catalog below is a fixed code contract with the ingested data.
# The Artificial Analysis capability indices the OpenRouter catalog carries in
# each model's `benchmarks.artificial_analysis` block, ingested automatically.
# Source citation for auto-ingested rows is the model's own OpenRouter page.
CATALOG_ARTIFICIAL_ANALYSIS_METRICS = ('intelligence_index', 'coding_index', 'agentic_index')
CATALOG_BENCHMARK_SOURCE_URL = 'https://openrouter.ai/'
# Evaluator labels (who actually measured a score). The catalog's AA block and
# the AA leaderboard backfill are the SAME evaluator, delivered two ways.
EVALUATOR_ARTIFICIAL_ANALYSIS = 'artificial_analysis'
EVALUATOR_HUGGINGFACE = 'huggingface_open_llm'
EVALUATOR_PLAYER = 'player'
# The data-source registry keys. The KEYS are a fixed code contract (the
# config interface); the VALUES (the actual URLs) are operator data held in the
# data_sources table, PUT via /api/data-sources with no restart -- these links
# drift over time and the code must not hard-code them.
DATA_SOURCE_KEYS = ('aa_leaderboard_url', 'hf_contents_parquet_url', 'hf_contents_source_url', 'mmmu_leaderboard_url')


def _get_data_source(key, default=None):
    """Current value of a benchmark data-source key, or `default` when unset.
    Reads the registry on every call (small table, hot-applied by design --
    an operator's PUT is picked up immediately, no cache to invalidate)."""
    if key not in DATA_SOURCE_KEYS:
        return default
    try:
        with _db() as conn:
            row = conn.execute('SELECT value FROM data_sources WHERE key = ?', (key,)).fetchone()
        return row[0] if row else default
    except Exception:  # noqa: BLE001 -- registry unavailable -> default (fail closed)
        return default
# How many points below the single best score on file, FOR THAT BAND'S OWN
# BENCHMARK, still counts as "good enough" to let price break the tie --
# picked for the coding band:
# Qwen3-Coder-480B (72.5%, $1.30/M) over Claude Sonnet 4.5 (77.2%, $18/M),
# a 4.7-point gap. Set with a little headroom above that exact gap. Score keeps
# its place: a meaningfully worse model does NOT win on price.
BENCHMARK_QUALITY_FLOOR_GAP = 6
# ...except the planning band, where the player is willing to spend a
# lot. Operationalized as a TIGHTER quality floor rather than "ignore
# price": the pick still has to justify itself on score, but far less
# compromise is accepted before cost breaks the tie, because every
# downstream call in a request depends on this one decomposition.


# BP-63: observed-latency speed tie-break. OpenRouter's own speed numbers
# are only consumable through request-time routing prefs (provider.sort etc.)
# -- the models catalog carries no latency field at all -- so the tank
# measures its own: every successful POST through _openrouter_chat_post
# records wall-clock duration per model slug, and the tier pick orders
# near-price qualifiers by the per-model observed median. Motivation
# (2026-10-18): two similarly priced qualifiers can differ enormously in
# real felt speed, and for agent loops wall-clock IS throughput. Boundaries
# the tie-break must never cross: the quality floor and the price rank do
# not move. Latency only reorders models already within
# TIER_SPEED_TIE_PARITY of the cheapest qualifier, and any band whose data
# is missing or under-sampled keeps the plain cheapest-first order. Which
# bands participate at all is deliberate (TIER_SPEED_BANDS below): only the
# high-frequency bands can build a median that describes the provider
# state *now*; a rare band's 5 samples accrue over days and can describe a
# transient provider event for the rest of the week, while the money at
# stake there is fractions of a cent per occurrence.
TIER_SPEED_TIE_PARITY = 0.15  # share of the cheapest qualifier's price inside which speed decides
# Bands whose qualifiers may be latency-reordered at all. low/coding are
# called constantly (every background completion and every coding task),
# so their samples arrive within minutes and the tie-break reflects the
# provider's current reality. mid/vision/high/sublow are called rarely or
# are budget-capped, so their medians go stale inside the 7-day window and
# speed-buying there trades real price caps for noise -- they keep the
# plain cheapest-first order forever (2026-10-18). Recording and the
# audit columns still cover every band, so relative latencies are learned
# for free and a future widening of this tuple is evidence-based.
TIER_SPEED_BANDS = ('low', 'coding')
MODEL_LATENCY_WINDOW_S = 7 * 86400    # recency window the median reads
MODEL_LATENCY_MIN_SAMPLES = 5         # below this a model has no usable speed opinion
MODEL_LATENCY_FETCH_LIMIT = 400       # most recent rows read per median query
MODEL_LATENCY_RETENTION_S = 7 * 86400  # older samples are pruned
MODEL_LATENCY_PRUNE_INTERVAL_S = 3600  # prune at most once per hour

_LAST_LATENCY_PRUNE = 0.0


def _record_model_latency(model, duration_ms):
    """Persist one successful model POST's wall-clock duration for the BP-63
    speed tie-break. Fail-open: stats collection must never break the call
    it is observing."""
    global _LAST_LATENCY_PRUNE
    if not model or not duration_ms or duration_ms <= 0:
        return
    try:
        with _db() as conn:
            conn.execute('INSERT INTO model_latency_samples (model, ts, duration_ms) VALUES (?, ?, ?)',
                         (model, time.time(), float(duration_ms)))
            now = time.time()
            if now - _LAST_LATENCY_PRUNE >= MODEL_LATENCY_PRUNE_INTERVAL_S:
                _LAST_LATENCY_PRUNE = now
                conn.execute('DELETE FROM model_latency_samples WHERE ts < ?',
                             (now - MODEL_LATENCY_RETENTION_S,))
    except Exception:  # noqa: BLE001 -- fail-open: stats never break the call
        pass


def _model_median_latency_ms(model_id, now=None):
    """Median wall-clock duration of this model's recent successful POSTs,
    or None when the model has fewer than MODEL_LATENCY_MIN_SAMPLES inside
    MODEL_LATENCY_WINDOW_S (an under-sampled model keeps the plain
    cheapest-first order instead of pretending to know its speed)."""
    if not model_id:
        return None
    try:
        now = time.time() if now is None else now
        with _db() as conn:
            rows = conn.execute(
                'SELECT duration_ms FROM model_latency_samples WHERE model = ? AND ts >= ? '
                'ORDER BY ts DESC LIMIT ?',
                (model_id, now - MODEL_LATENCY_WINDOW_S, MODEL_LATENCY_FETCH_LIMIT)).fetchall()
    except Exception:  # noqa: BLE001 -- a stats read must never break a pick
        return None
    if len(rows) < MODEL_LATENCY_MIN_SAMPLES:
        return None
    return statistics.median(float(r[0]) for r in rows)


def _model_latency_sample_count(model_id, now=None):
    """How many successful samples this model has inside the recency window
    (0 on any DB problem -- a count is only audit color, never a gate)."""
    if not model_id:
        return 0
    try:
        now = time.time() if now is None else now
        with _db() as conn:
            row = conn.execute(
                'SELECT COUNT(*) FROM model_latency_samples WHERE model = ? AND ts >= ?',
                (model_id, now - MODEL_LATENCY_WINDOW_S)).fetchone()
    except Exception:  # noqa: BLE001 -- audit color must never break a pick
        return 0
    return int(row[0]) if row else 0


def _attach_latency_audit(picked, cheapest_qual):
    """BP-63 audit payload for tier_daily_log: when a pick was latency-decided
    (the pick is a pricier qualifier inside the parity band than the cheapest
    qualifier on file), the daily row must carry the observed median and the
    sample count that grounded it -- otherwise a later review cannot tell a
    deliberate speed choice from a mistake. Stamped as ('_latencyAudit',
    (median_ms, sample_count, speed_picked)) on the picked dict; (None, None,
    False) when the pick had no usable latency data or matches cheapest-first.
    The extra key rides the in-memory pick only; model_tiers writes ignore it."""
    audit = (None, None, False)
    median = _model_median_latency_ms(picked['id'])
    if median is not None:
        speed = bool(cheapest_qual is not None
                     and cheapest_qual['id'] != picked['id']
                     and cheapest_qual['price'] < picked['price'] - 1e-9)
        audit = (round(median, 1), _model_latency_sample_count(picked['id']), speed)
    picked['_latencyAudit'] = audit
    return picked


def _speed_tie_order(qualifiers, band):
    """BP-63 reorder of price-sorted qualifiers: among models within
    TIER_SPEED_TIE_PARITY of the cheapest qualifier, models with a usable
    observed-median latency lead, fastest first (price breaks latency ties);
    models with no latency data follow them in price order, and models
    beyond the parity band keep the cheapest-first order after everyone.
    Only bands listed in TIER_SPEED_BANDS participate at all -- any other
    band (and callers with no band) gets the qualifiers back unchanged in
    price order, exactly as before the tie-break existed.
    Returns the SAME members, possibly reordered -- never adds, drops or
    re-scores anything, and short lists or lists with no latency data pass
    through unchanged. The price watch mirrors this exact order so a speed
    pick is never misread as a perpetually-available cheaper qualifier."""
    if band not in TIER_SPEED_BANDS:
        return list(qualifiers)
    if len(qualifiers) < 2:
        return list(qualifiers)
    near = [m for m in qualifiers if m['price'] <= qualifiers[0]['price'] * (1 + TIER_SPEED_TIE_PARITY)]
    if len(near) < 2:
        return list(qualifiers)
    latencies = {m['id']: _model_median_latency_ms(m['id']) for m in near}
    timed = sorted(
        (m for m in near if latencies[m['id']] is not None),
        key=lambda m: (latencies[m['id']], m['price']))
    if not timed:
        return list(qualifiers)
    return (timed
            + [m for m in near if latencies[m['id']] is None]
            + [m for m in qualifiers if m not in near])


def get_model_benchmark_scores():
    with _db() as conn:
        rows = conn.execute(
            'SELECT model_id, benchmark, score, source_url, evaluator, checked_at FROM model_benchmark_scores'
        ).fetchall()
    return [
        {'model_id': r[0], 'benchmark': r[1], 'score': r[2], 'source_url': r[3], 'evaluator': r[4], 'checked_at': r[5]}
        for r in rows
    ]


def set_model_benchmark_score(model_id, benchmark, score, source_url, evaluator='player'):
    with _db() as conn:
        conn.execute(
            'INSERT INTO model_benchmark_scores (model_id, benchmark, score, source_url, evaluator, checked_at) VALUES (?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(model_id, benchmark) DO UPDATE SET score=excluded.score, source_url=excluded.source_url, evaluator=excluded.evaluator, checked_at=excluded.checked_at',
            (model_id, benchmark, score, source_url, evaluator, time.time()),
        )


def _best_value_qualifiers_sync(candidates, scores, floor_gap=None):
    """The candidates that clear the band's quality bar, cheapest first: those
    within `floor_gap` points of the best stored score in the pool. Pure and
    network-free -- `_best_value_pick` live-verifies down this list, and the
    price watch reuses the same list to spot a cheaper qualifier appearing
    WITHOUT paying for verify probes. Returns [] when nothing in `candidates`
    has a stored score yet."""
    if floor_gap is None:
        floor_gap = BAND_QUALITY_FLOOR_GAP.get('default',
                                               BENCHMARK_QUALITY_FLOOR_GAP)
    scored = [m for m in candidates if m['id'] in scores]
    if not scored:
        return []
    best_score = max(scores[m['id']] for m in scored)
    return sorted(
        (m for m in scored if scores[m['id']] >= best_score - floor_gap),
        key=lambda m: m['price'],
    )


async def _best_value_pick(candidates, scores, floor_gap=None, band=None):
    # Shared by every band (restored): score keeps its place. Among
    # candidates with a real stored score, keep only those within the band's
    # quality floor of the best score present, then take the first one on
    # the walk order that actually still works (live-verified, same as every
    # other pick here). Score guards against picking a meaningfully worse
    # model purely on price; price then breaks ties among models that pass
    # the quality bar -- with the BP-63 speed tie-break acting as the final
    # reorder inside a narrow parity of the cheapest qualifier, so the
    # cheapest-and-slowest model no longer wins a band it barely deserved on
    # price alone. The reorder only runs for bands in TIER_SPEED_BANDS;
    # band=None (and every band outside the tuple) keeps cheapest-first.
    # Returns None if nothing in `candidates` has a stored score yet, or
    # nothing verified works -- callers fall back to Jev's classifier in
    # that case.
    for candidate in _speed_tie_order(_best_value_qualifiers_sync(candidates, scores, floor_gap), band):
        if await asyncio.to_thread(_verify_model_works_sync, candidate['id']):
            return candidate
    return None


def _fetch_openrouter_catalog_sync():
    with urllib.request.urlopen('https://openrouter.ai/api/v1/models', timeout=20) as resp:  # nosec B310 -- fixed known-good URL, no user input, SSRF guard applies at callers
        return json.loads(resp.read())['data']


def _bucket_models_by_price(models):
    buckets = {band: [] for band in MODEL_PRICE_BANDS}
    for m in models:
        # Bug: ":batch" variants (e.g. gpt-5.6-sol-pro:batch)
        # are only usable through OpenRouter's separate Batch API (24h+
        # turnaround), not real-time chat completions -- confirmed by a
        # direct call returning a 404 pointing at /api/v1/batches instead.
        # This whole system needs a synchronous reply, so these are never
        # valid candidates regardless of price.
        if ':batch' in m['id']:
            continue
        # Bug: a "mid" pick (Qwen3-30B-A3B) returned null
        # content on a completely ordinary single-turn prompt. Traced to
        # the model's own catalog entry: `reasoning: {default_enabled:
        # True}` -- it spends tokens on hidden reasoning before ever
        # producing visible output, and finish_reason was "length": with
        # this system's deliberately tight max_tokens (150 default, 600
        # cap), the reasoning alone can consume the whole budget and leave
        # nothing for an actual answer.
        #
        # The original fix for that (excluding ANY model with a `reasoning`
        # key at all) was too broad -- by late 2026 most current frontier
        # models carry a `reasoning` block even when reasoning is optional
        # and off by default (e.g. {"mandatory": False} with no
        # default_enabled), which is a completely different, harmless case:
        # nothing gets spent unless a request explicitly turns it on, which
        # this system never does. That blanket exclusion was silently
        # hiding the Claude 4.5/5 family, GPT-5.1, DeepSeek V3.2 and others
        # from every price band -- confirmed: with the old rule, the
        # "high" band's actual best-available pick was GPT-4.1 (48.6% on
        # SWE-bench Verified) while Claude Haiku 4.5, cheaper at $6 vs
        # GPT-4.1's $10/M, was invisible to Jev despite scoring 73.3% on
        # the same benchmark.
        #
        # The narrowed fix (excluding `default_enabled` too) was still too
        # broad after the call layer grew an explicit reasoning override:
        # _call_openrouter_once_sync and _post_openrouter_raw now always send
        # a `reasoning` field -- disabled for every tier that doesn't read
        # reasoning output, capped at half the request budget for the tiers
        # that do -- so a default_enabled model can no longer burn the whole
        # budget on hidden reasoning; the override is the explicit switch the
        # old incident lacked. That made DeepSeek V4 Flash Vision (and the
        # Claude 4.5/5 family, GPT-5.1, etc.) needlessly invisible again --
        # a real cost: the vision band couldn't even consider a 76.7-MMMU
        # model at $0.86/M.
        #
        # Mandatory reasoning is admissible too, as of the z-ai/glm-5.3-flash
        # finding: the catalog flags it `reasoning: {mandatory: true,
        # default_effort: "max"}` and it beats the then-coding tier on every
        # benchmark at half the price, but it never entered a pool. The old
        # exclusion was too blunt -- a mandatory model rejects
        # reasoning={'enabled': False} with a 400, but answers fine at any
        # budget when reasoning is CAPPED (the exact shape the tier-aware
        # override already sends), and _openrouter_chat_post recovers that
        # 400 with the capped retry. Pool admission is therefore safe: the
        # live verification probe is the real gate, and a mandatory model
        # that cannot answer within its budget still fails verify and is
        # skipped -- exactly what kept the broken picks out before.
        arch = m.get('architecture') or {}
        arch = m.get('architecture') or {}
        if 'text' not in (arch.get('output_modalities') or []) or 'text' not in (arch.get('input_modalities') or []):
            continue
        try:
            prompt_price = float(m['pricing']['prompt'])
            completion_price = float(m['pricing']['completion'])
        except (KeyError, TypeError, ValueError):
            continue
        if prompt_price <= 0 or completion_price <= 0:
            continue  # exclude free tier -- often rate-limited/unreliable for a think tank that needs to run reliably
        price_per_m = (prompt_price + completion_price) * 1_000_000
        for band, (lo, hi) in MODEL_PRICE_BANDS.items():
            if lo <= price_per_m < hi:
                buckets[band].append({'id': m['id'], 'name': m.get('name', m['id']), 'price': price_per_m})
                break
    for band in buckets:
        buckets[band].sort(key=lambda x: x['price'])
    return buckets


# Bug: the old probe asked for max_tokens=5, which any
# model that does hidden reasoning before answering will fail by
# construction -- it spends the whole budget thinking and returns
# content: None with a perfectly successful HTTP 200. Confirmed against
# deepseek-v4-pro-0813, which fails at 5 tokens and answers normally at a
# realistic budget. 64 was still too tight for the next class of model:
# z-ai/glm-5.3-flash (mandatory reasoning, default_effort "max") ran its
# hidden chain past 300 tokens in most probes and returned content: None
# even with reasoning capped at 32 -- the provider enforces the reasoning
# cap only intermittently at tiny budgets. 512 gives the chain room to
# finish and the answer room to appear, at trivial probe cost.
MODEL_VERIFY_MAX_TOKENS = 512
# And a transient failure must not be cached as a permanent verdict. Also
# Deepseek-v4-flash verified fine three times, failed once
# mid-refresh (a blip/rate-limit), and that single failure silently
# downgraded the whole 'low' band for the rest of the session, because
# the fallback result is what gets written to model_tiers. One retry is
# enough to tell "genuinely not served" from "unlucky moment."
MODEL_VERIFY_ATTEMPTS = 2


def _verify_model_works_sync(model_id):
    # Catalog metadata alone can't catch this -- a model can be listed
    # with valid pricing and architecture yet have zero actual serving
    # endpoints behind it right now (confirmed: openai/gpt-5.2-chat
    # 404'd with "No endpoints found" despite a perfectly normal-looking
    # catalog entry). A real test call is the only real authority, same
    # lesson as Jev's own routing earlier in.
    for attempt in range(MODEL_VERIFY_ATTEMPTS):
        try:
            data = _call_openrouter_sync(model_id, [{'role': 'user', 'content': 'hi'}], MODEL_VERIFY_MAX_TOKENS)
            content = data['choices'][0]['message']['content']
            if content and content.strip():
                # Housekeeping spend is real spend: the daily tier-refresh
                # probes hit the live API, so they must accrue like every
                # other model call -- otherwise the cap and the Bank would
                # undercount the "just existing" cost of these probes.
                _accrue_spend('__model_verify__', ((data or {}).get('usage') or {}).get('cost', 0.0))
                return True
        except Exception:
            pass
        if attempt + 1 < MODEL_VERIFY_ATTEMPTS:
            time.sleep(1)  # a blip and a rate-limit both clear on their own; a dead model won't
    return False


def _verify_decision_model_works_sync(model_id):
    """Decision-model-aware sibling of _verify_model_works_sync. Jev-class
    slugs are TYPED decisions models: they answer only on /api/alpha/decisions
    and REJECT /v1/chat/completions outright ("use the decisions endpoint
    instead"), so a chat-completions probe would call a healthy Jev 'dead'
    and the daily auto-failover would wrongly replace it. Probes the real
    decisions wire format instead: a one-question multiple-choice call that
    must come back with a parseable answer, not an error."""
    if '://' in model_id:
        # Loopback/standby (Colab/Laya) entries can't be probed from here --
        # they only answer while the CLI forward is up, which is not what this
        # check is for. Treat them as reachable-by-construction (failover
        # already handles them) so the daily loop never 'fixes' the chain by
        # dropping them.
        return True
    for attempt in range(MODEL_VERIFY_ATTEMPTS):
        try:
            req = _decision_request(model_id, {'messages': [], 'signals': {}},
                                    {'choice': {'type': 'choice',
                                                'instructions': 'Reply with one of the given options.',
                                                'criteria': {'a': 'first option', 'b': 'second option'}}})
            data = json.loads(_urlopen_with_resilience(req, timeout=30))
            if data.get('answers', {}).get('choice', {}).get('choice'):
                # Same accrual rule as the chat probe above: a real API call,
                # so its cost belongs in the ledger (visible in the Bank,
                # counted against the monthly cap) -- never silently free.
                _accrue_spend('__model_verify__', ((data or {}).get('usage') or {}).get('cost', 0.0))
                return True
        except Exception:
            pass
        if attempt + 1 < MODEL_VERIFY_ATTEMPTS:
            time.sleep(1)
    return False


def _apply_band_price_ceiling(band, candidate_pool):
    """The high, coding, vision and sublow tiers carry a per-model PRICE
    CEILING: the refresh only presents candidates at or below the matching
    *_TIER_MAX_PRICE_USD constant. A model over the ceiling is never offered,
    even if it tops the score table -- price is a hard bound for these
    tiers, not a tiebreak. For sublow the ceiling IS the band (see
    SUBLOW_TIER_MAX_PRICE_USD). Every other band passes through unchanged."""
    if band == 'high' and HIGH_TIER_MAX_PRICE_USD:
        return [m for m in candidate_pool if m['price'] <= HIGH_TIER_MAX_PRICE_USD]
    if band == 'coding' and CODING_TIER_MAX_PRICE_USD:
        return [m for m in candidate_pool if m['price'] <= CODING_TIER_MAX_PRICE_USD]
    if band == 'vision' and VISION_TIER_MAX_PRICE_USD:
        return [m for m in candidate_pool if m['price'] <= VISION_TIER_MAX_PRICE_USD]
    if band == 'sublow' and SUBLOW_TIER_MAX_PRICE_USD:
        return [m for m in candidate_pool if m['price'] <= SUBLOW_TIER_MAX_PRICE_USD]
    return candidate_pool


def _band_candidate_pools(models):
    """{band: candidate_pool} for every band refresh_model_tiers picks for --
    the cost bands (low/mid/high/coding/sublow) plus 'vision'. Applies the SAME
    rules the refresh does: capability bands (coding/high/sublow) search every
    price bucket because a price bracket says nothing about whether a model can
    code or decompose a goal; every band then gets its price ceiling; vision
    additionally requires real image input. Extracted so the price watch can
    reason about the SAME pools the pick uses, without duplicating the logic
    and drifting from it."""
    buckets = _bucket_models_by_price(models)
    pools = {}
    for band in MODEL_BAND_PURPOSE:
        capability_band = band in ('coding', 'high', 'sublow')
        pool = [m for bp in buckets.values() for m in bp] if capability_band else buckets.get(band, [])
        pools[band] = _apply_band_price_ceiling(band, pool)
    vision = []
    for m in models:
        if ':batch' in m['id']:
            continue
        arch = m.get('architecture') or {}
        if 'image' not in (arch.get('input_modalities') or []) or 'text' not in (arch.get('output_modalities') or []):
            continue
        reasoning_info = m.get('reasoning')
        if reasoning_info and reasoning_info.get('mandatory'):
            continue
        try:
            p, c = float(m['pricing']['prompt']), float(m['pricing']['completion'])
        except (KeyError, TypeError, ValueError):
            continue
        if p <= 0 or c <= 0:
            continue
        vision.append({'id': m['id'], 'name': m.get('name', m['id']), 'price': (p + c) * 1_000_000})
    pools['vision'] = _apply_band_price_ceiling('vision', vision)
    return pools


def _ingest_catalog_benchmarks(models):
    """Auto-ingest the OpenRouter catalog's own capability indices into
    model_benchmark_scores on every sync -- this is what keeps the benchmark
    table fresh as models appear and as scores update, without the player
    hand-entering research for every new model. Upserts (never deletes) rows
    keyed (model_id, benchmark); a model's rows are then retired by the same
    removal purge that drops models gone from the catalog (checked_at goes
    stale, the purge deletes it -- so a deleted OpenRouter model's scores
    die with it, exactly like the catalog row).

    Only the Artificial Analysis indices are ingested: they're the catalog's
    general capability signals the bands actually consume (see BAND_BENCHMARK).
    The catalog's other benchmark block (design_arena) is visual-GENERATION
    elo that no band uses, so it is not stored. Player-entered scores under
    OTHER benchmark names (MMLU, SWE-bench Verified, Humanity's Last Exam,
    MMMU) are separate rows and are never touched here."""
    now = time.time()
    written = 0
    with _db() as conn:
        for m in models:
            aa = (m.get('benchmarks') or {}).get('artificial_analysis')
            if not isinstance(aa, dict):
                continue
            for metric in CATALOG_ARTIFICIAL_ANALYSIS_METRICS:
                value = aa.get(metric)
                if not isinstance(value, (int, float)):
                    continue
                conn.execute(
                    'INSERT INTO model_benchmark_scores (model_id, benchmark, score, source_url, evaluator, checked_at) '
                    'VALUES (?, ?, ?, ?, ?, ?) '
                    'ON CONFLICT(model_id, benchmark) DO UPDATE SET '
                    'score=excluded.score, source_url=excluded.source_url, evaluator=excluded.evaluator, checked_at=excluded.checked_at',
                    (m['id'], metric, float(value), CATALOG_BENCHMARK_SOURCE_URL + m['id'],
                     'artificial_analysis', now),
                )
                written += 1
    return written


def _benchmark_name_tokens(name):
    """Normalize a model display name into a token set for fuzzy matching
    between the catalog ('OpenAI: GPT-6 Luna Pro') and an evaluator's own
    names ('GPT-6 Luna (max)', 'meta-llama/llama-3.1-70b-instruct').
    Parentheticals are reasoning-effort variants, not model identity, so they
    are dropped; every other run of non-alphanumerics is a SEPARATOR (a dash,
    dot or slash in a hub id must not glue tokens like '70b' and 'instruct'
    into '70binstruct'), and the vendor prefix and punctuation collapse."""
    name = re.sub(r'\(.*?\)', '', (name or '').lower())
    name = re.sub(r'[^a-z0-9]+', ' ', name)
    return set(name.split())


def _aa_leaderboard_scores(html):
    """Parse the Artificial Analysis LLM leaderboard page into
    {display_name: intelligence_index}. Tolerates the '*' flags and the
    non-numeric cells that mark models AA has not indexed."""
    out = {}
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S):
        cells = [' '.join(re.sub(r'<[^>]+>', ' ', c).split())
                 for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.S)]
        if len(cells) >= 4 and cells[0]:
            index = cells[3].replace('*', '').replace('.', '', 1)
            if index.isdigit():
                out[cells[0]] = float(index)
    return out


def _match_aa_score(model_name, aa_scores, min_overlap=0.6):
    """Fuzzy-match a catalog model name against AA's display names. The score
    is the shared-token fraction of the SMALLER token set, so 'GPT-6 Luna Pro'
    matches 'GPT-6 Luna (max)' (3/3) but not 'GPT-6 Luna (low)' with less
    overlap than a different model's. When several AA variants tie, prefer the
    'max' / 'default fallback' variant -- the same variant the OpenRouter
    catalog resolves for its own AA block. Returns None when nothing clears
    the overlap bar."""
    nt = _benchmark_name_tokens(model_name)
    if not nt:
        return None
    candidates = []
    for key, value in aa_scores.items():
        at = _benchmark_name_tokens(key)
        inter = len(nt & at)
        if inter == 0:
            continue
        candidates.append((inter / min(len(nt), len(at)), key, value))
    if not candidates:
        return None
    best_score = max(c[0] for c in candidates)
    if best_score < min_overlap:
        return None
    best = [c for c in candidates if c[0] == best_score]
    best.sort(key=lambda c: 0 if ('max' in c[1].lower() or 'default fallback' in c[1].lower()) else 1)
    return best[0][2]


def _ingest_aa_leaderboard(models):
    """Backfill intelligence_index for catalog models the OpenRouter catalog
    carries no AA data for (their entry has no artificial_analysis block),
    straight from Artificial Analysis's public LLM leaderboard. The leaderboard
    page is the same source the catalog's AA block comes from, with wider
    coverage, and it is publicly scrapeable (the login wall is on the /models
    charts page, not /leaderboards/models). Backfill-only: INSERT OR IGNORE, so
    an already-resolved catalog value is never disputed. A model AA never
    evaluated (e.g. legacy 2024 open-weights) simply gets no row -- there is no
    number to import. Returns the number of new rows."""
    url = _get_data_source('aa_leaderboard_url')
    if not url:
        print('[model-tiers] data source aa_leaderboard_url unset -- skipping AA leaderboard backfill', flush=True)
        return 0
    import urllib.request as _ur
    try:
        with _ur.urlopen(url, timeout=20) as resp:  # nosec B310 -- URL comes from the operator registry, not user input
            html = resp.read().decode('utf-8', 'ignore')
    except Exception:
        return 0
    aa_scores = _aa_leaderboard_scores(html)
    if not aa_scores:
        return 0
    now = time.time()
    added = 0
    with _db() as conn:
        for m in models:
            mid = m['id']
            have = conn.execute(
                'SELECT 1 FROM model_benchmark_scores WHERE model_id=? AND benchmark=?',
                (mid, 'intelligence_index'),
            ).fetchone()
            if have:
                continue
            value = _match_aa_score(m.get('name') or mid, aa_scores)
            if value is None:
                continue
            cur = conn.execute(
                'INSERT OR IGNORE INTO model_benchmark_scores (model_id, benchmark, score, source_url, evaluator, checked_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (mid, 'intelligence_index', value, url, EVALUATOR_ARTIFICIAL_ANALYSIS, now),
            )
            if cur.rowcount:
                added += 1
    return added


def _mmmu_leaderboard_scores(html):
    """Parse the MMMU leaderboard table into {display_name: mmmu}. The page
    (pricepertoken.com/leaderboards/benchmark/mmmu, data sourced from
    Artificial Analysis) is a six-column table: provider, model, input $/M,
    output $/M, MMMU, actions. The '-' cells mark models AA has not measured
    on MMMU and are skipped, as is the header row."""
    out = {}
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S):
        cells = [' '.join(re.sub(r'<[^>]+>', ' ', c).split())
                 for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.S)]
        if len(cells) < 5 or not cells[1]:
            continue
        try:
            out[cells[1]] = float(cells[4])
        except ValueError:
            continue
    return out


def _ingest_mmmu_leaderboard(models):
    """Backfill MMMU (vision) scores from the operator-registered MMMU
    leaderboard URL -- the only benchmark the OpenRouter catalog and the AA
    leaderboard carry no data for, and exactly why the vision band kept
    resolving to a stale classifier placeholder instead of a score-grounded
    pick. Mirrors the AA leaderboard backfill: INSERT OR IGNORE, so an
    already-resolved score (player-entered, or an earlier backfill) is never
    disputed. Matches by the same fuzzy display-name tokens as the AA
    backfill. Returns the number of new rows."""
    url = _get_data_source('mmmu_leaderboard_url')
    if not url:
        print('[model-tiers] data source mmmu_leaderboard_url unset -- skipping MMMU backfill', flush=True)
        return 0
    import urllib.request as _ur
    try:
        # The leaderboard blocks plain urllib with a 403 -- send a browser
        # User-Agent or the daily MMMU backfill silently ingests nothing and
        # the vision band falls back to a classification guess instead of a
        # score-grounded pick.
        _req = _ur.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'})
        with _ur.urlopen(_req, timeout=20) as resp:  # nosec B310 -- URL comes from the operator registry, not user input
            html = resp.read().decode('utf-8', 'ignore')
    except Exception:
        return 0
    mmmu_scores = _mmmu_leaderboard_scores(html)
    if not mmmu_scores:
        return 0
    now = time.time()
    added = 0
    with _db() as conn:
        for m in models:
            mid = m['id']
            have = conn.execute(
                'SELECT 1 FROM model_benchmark_scores WHERE model_id=? AND benchmark=?',
                (mid, 'MMMU'),
            ).fetchone()
            if have:
                continue
            value = _match_aa_score(m.get('name') or mid, mmmu_scores)
            if value is None:
                continue
            cur = conn.execute(
                'INSERT OR IGNORE INTO model_benchmark_scores (model_id, benchmark, score, source_url, evaluator, checked_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (mid, 'MMMU', value, url, EVALUATOR_ARTIFICIAL_ANALYSIS, now),
            )
            if cur.rowcount:
                added += 1
    return added


def _parse_hf_contents(parquet_bytes):
    """Read the Open LLM Leaderboard `contents` parquet into
    {hub_model_id_lower: mmlu_pro}. Returns None when it cannot be read
    (pyarrow absent, format change, ...) so callers skip quietly."""
    try:
        import io as _io
        import pyarrow.parquet as pq
        table = pq.read_table(_io.BytesIO(parquet_bytes))
        names = table.column('fullname').to_pylist()
        scores = table.column('MMLU-PRO').to_pylist()
        return {
            str(name).lower(): (float(score) if score is not None else None)
            for name, score in zip(names, scores)
        }
    except Exception:
        return None
