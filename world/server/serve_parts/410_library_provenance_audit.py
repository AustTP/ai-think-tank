

def _library_provenance_audit(now, window_s):
    """The weekly memory audit: what persistent knowledge the colony absorbed
    this window and where it came from (the article's "mark anything that came
    from web content rather than directly from you"). Reads ground truth only:
    the library_write action log (path + source) and the pending_review tree
    on disk. The write gate forces source='external' content into
    pending_review/, so any external write that landed in the TRUSTED tree is
    the memory-poisoning failure class the gate exists to prevent -- report
    it, don't bury it. Best-effort: a DB or filesystem error never breaks the
    weekly review."""
    rows = []
    try:
        with _db() as conn:
            rows = conn.execute(
                'SELECT details FROM action_log WHERE action = ? AND ts > ? AND ts <= ?',
                ('library_write', now - window_s, now),
            ).fetchall()
    except Exception:
        rows = []
    by_source: dict = {}
    external_in_trusted = []
    for (details,) in rows:
        d = {}
        if details:
            try:
                d = json.loads(details)
            except Exception:
                d = {}
        source = d.get('source', 'firsthand')
        by_source[source] = by_source.get(source, 0) + 1
        path = d.get('path') or ''
        if source == 'external' and path and not path.startswith('pending_review/'):
            external_in_trusted.append(path)
    unvetted = []
    try:
        pending_root = os.path.join(LIBRARY_DIR, 'pending_review')
        if os.path.isdir(pending_root):
            for root, _dirs, files in os.walk(pending_root):
                for name in files:
                    full = os.path.join(root, name)
                    unvetted.append(os.path.relpath(full, LIBRARY_DIR))
    except Exception:
        pass
    unvetted.sort()
    return {
        'writes': len(rows),
        'by_source': {k: by_source[k] for k in sorted(by_source)},
        'external_in_trusted': sorted(set(external_in_trusted)),
        'unvetted_pending': unvetted,
    }


def _build_weekly_review(now=None):
    """Assemble one weekly review from GROUND-TRUTH tables (action_log +
    decision_tape), not agent self-reports. `now` is injected for pure
    testability (epoch seconds). Returns a dict with the digest + a markdown
    report, or None if there's nothing to review. Reads two windows -- the
    current one and the immediately prior week -- so the report carries
    week-over-week deltas."""
    now = time.time() if now is None else now
    week = _WEEKLY_REVIEW_WINDOW_S
    since = now - week
    prior_since = since - week
    prior_until = since
    with _db() as conn:
        actions = conn.execute(
            'SELECT agent_id, action, ts FROM action_log WHERE ts > ? AND ts <= ?',
            (since, now),
        ).fetchall()
        prior_actions = conn.execute(
            'SELECT action, ts FROM action_log WHERE ts > ? AND ts <= ?',
            (prior_since, prior_until),
        ).fetchall()
        decisions = conn.execute(
            'SELECT kind, ok, confidence, cost FROM decision_tape WHERE ts > ? AND ts <= ?',
            (since, now),
        ).fetchall()
        prior_decisions = conn.execute(
            'SELECT ok, cost FROM decision_tape WHERE ts > ? AND ts <= ?',
            (prior_since, prior_until),
        ).fetchall()

    # Digest: per-agent action tallies, total action count, decision stats.
    per_agent: dict = {}
    action_counts: dict = {}
    for agent_id, action, _ts in actions:
        action_counts[action] = action_counts.get(action, 0) + 1
        if not agent_id or agent_id == 'player' or agent_id == 'system':
            continue
        bucket = per_agent.setdefault(agent_id, {'actions': 0, 'shipped': 0})
        bucket['actions'] += 1
        if action in _PROGRESS_ACTIONS:
            bucket['shipped'] += 1
    shipped_actions = sum(b.get('shipped', 0) for b in per_agent.values())
    ceremony_count = sum(v for k, v in action_counts.items() if k in _CEREMONY_ACTIONS)

    n_decisions = len(decisions)
    ok_decisions = sum(1 for d in decisions if d[1])
    jep_cost = sum(float(d[3] or 0) for d in decisions)
    per_kind: dict = {}
    for kind, ok, _conf, _cost in decisions:
        kb = per_kind.setdefault(kind, {'n': 0, 'ok': 0})
        kb['n'] += 1
        kb['ok'] += int(ok)

    # Prior-week deltas (ground-truth comparison).
    prior_actions_n = len(prior_actions)
    prior_shipped_n = sum(1 for a in prior_actions if a[0] in _PROGRESS_ACTIONS)
    prior_ceremony_n = sum(1 for a in prior_actions if a[0] in _CEREMONY_ACTIONS)
    prior_decisions_n = len(prior_decisions)
    prior_ok_n = sum(1 for d in prior_decisions if d[0])
    prior_cost = sum(float(d[1] or 0) for d in prior_decisions)

    # Trailing-window collapse check (the "test the latest window by itself"
    # gate): a long window can hide a dying trailing window, so the long trend
    # is never trusted over THIS week. Flag when decision health or the shipped-
    # work rate has collapsed against the prior week, with a minimum sample so
    # a quiet window never reads as decay.
    regime_decay = None
    if n_decisions >= 5 and prior_decisions_n >= 5:
        cur_rate = ok_decisions / n_decisions
        prior_rate = prior_ok_n / prior_decisions_n
        if prior_rate > 0 and cur_rate < prior_rate * 0.5:
            regime_decay = (f'decision ok-rate collapsed to {cur_rate:.0%} this week '
                            f'from {prior_rate:.0%} last week')
    elif prior_shipped_n >= 5 and shipped_actions < prior_shipped_n * 0.5 \
            and len(actions) >= max(3, prior_actions_n * 0.2):
        regime_decay = (f'shipped work dropped to {shipped_actions} this week '
                        f'from {prior_shipped_n} last week')

    # Knowledge provenance audit (the memory audit): what the colony wrote
    # this window and where it came from, plus what external content is still
    # sitting unvetted in pending_review/. Computed up front so a quiet week
    # with only injected/external content still produces a review.
    provenance = _library_provenance_audit(now, week)

    def _delta(cur, prior):
        return round(cur - prior, 1)

    digest = {
        'period_start_ms': _weekly_period_start(now),
        'window_s': week,
        'generated_at': now,
        'total_actions': len(actions),
        'shipped_actions': shipped_actions,
        'ceremony_actions': ceremony_count,
        'decisions': n_decisions,
        'decisions_ok': ok_decisions,
        'decision_cost_usd': round(jep_cost, 4),
        'decision_kinds': {k: v for k, v in sorted(per_kind.items())},
        'per_agent': {aid: v for aid, v in sorted(per_agent.items())},
        'deltas': {
            'actions': _delta(len(actions), prior_actions_n),
            'shipped': _delta(shipped_actions, prior_shipped_n),
            'ceremony': _delta(ceremony_count, prior_ceremony_n),
            'decisions': _delta(n_decisions, prior_decisions_n),
            'decision_cost_usd': round(jep_cost - prior_cost, 4),
        },
        'regime_decay': regime_decay,
        'provenance': {
            'library_writes': provenance['writes'],
            'by_source': provenance['by_source'],
            'external_in_trusted': provenance['external_in_trusted'],
            'unvetted_pending_n': len(provenance['unvetted_pending']),
            'unvetted_pending': provenance['unvetted_pending'][:20],
        },
    }

    if not actions and not decisions and provenance['writes'] == 0 \
            and not provenance['external_in_trusted'] and not provenance['unvetted_pending']:
        return None

    stamp = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))
    lines = [
        f'# Weekly Review -- {stamp}',
        '',
        'Ground truth for this window (from the action_log + decision tape, not',
        'agent self-reports):',
        '',
        f'- Actions logged: **{len(actions)}** ({_delta(len(actions), prior_actions_n):+d} vs prior week)',
        f'- Shipped work (deliverables/approvals): **{shipped_actions}** ({_delta(shipped_actions, prior_shipped_n):+d})',
        f'- Ceremony/process actions: **{ceremony_count}** ({_delta(ceremony_count, prior_ceremony_n):+d})',
        f'- Jev decisions: **{n_decisions}** ({ok_decisions} ok)',
        f'- Decision-model spend: **${round(jep_cost, 4):.4f}** ({round(jep_cost - prior_cost, 4):+.4f})',
        '',
    ]
    if regime_decay:
        lines.append(f'- **Trailing-window decay**: {regime_decay}')
        lines.append('')
    lines.append('## Per-agent activity')
    lines.append('')
    if per_agent:
        for aid, b in sorted(per_agent.items(), key=lambda kv: -kv[1]['shipped']):
            lines.append(f'- **{aid}**: {b["actions"]} action(s), {b["shipped"]} shipped')
    else:
        lines.append('(no per-agent activity recorded in this window)')
    if per_kind:
        lines += ['', '## Decisions by kind', '']
        for kind, v in sorted(per_kind.items()):
            lines.append(f'- {kind}: {v["n"]} ({v["ok"]} ok)')
    # Knowledge provenance audit: what the colony absorbed and where it came
    # from. External (web-browsed) content is the memory-poisoning risk; it is
    # gated into pending_review/, so a write that landed in the trusted tree
    # is a real signal, and unvetted pending content is the article's "mark
    # what came from the web" list.
    if provenance['writes'] or provenance['external_in_trusted'] or provenance['unvetted_pending']:
        lines += ['', '## Knowledge provenance audit', '']
        lines.append(f'- Library writes this window: **{provenance["writes"]}**')
        if provenance['by_source']:
            by_src = ', '.join(f'{k}: {v}' for k, v in provenance['by_source'].items())
            lines.append(f'- By source: {by_src}')
        if provenance['external_in_trusted']:
            lines.append('- **Memory-poisoning signal**: external content landed in the '
                         'trusted tree (the gate should have sent it to pending_review/):')
            for p in provenance['external_in_trusted'][:20]:
                lines.append(f'  - `{p}`')
        if provenance['unvetted_pending']:
            lines.append(f'- Unvetted external content still in pending_review/ '
                         f'({len(provenance["unvetted_pending"])} file(s)):')
            for p in provenance['unvetted_pending'][:20]:
                lines.append(f'  - `{p}`')
    return {'digest': digest, 'markdown': '\n'.join(lines) + '\n'}


def _generate_weekly_review(now=None):
    """Build + persist one weekly review, idempotently (one row per UTC week).
    Returns the stored (digest, markdown) or None if there was nothing to
    review. Best-effort and thread-safe: a DB failure never raises."""
    review = _build_weekly_review(now=now)
    if review is None:
        return None
    try:
        with _db() as conn:
            conn.execute(
                'INSERT INTO weekly_reviews (period_start, generated_at, digest, markdown) '
                'VALUES (?, ?, ?, ?) '
                'ON CONFLICT(period_start) DO UPDATE SET generated_at=excluded.generated_at, '
                'digest=excluded.digest, markdown=excluded.markdown',
                (review['digest']['period_start_ms'], review['digest']['generated_at'],
                 json.dumps(review['digest']), review['markdown']),
            )
    except Exception as e:
        print(f'[weekly-review] persist failed: {e}', flush=True)
    return review


async def _weekly_review_loop():
    # Own slow timer (not piggy-backed on the health loop -- this is a weekly
    # scan over the two heavy tables, and the health loop must stay cheap).
    # Runs in a thread so the DB scan never blocks the event loop.
    while True:
        await asyncio.sleep(WEEKLY_REVIEW_INTERVAL_S)
        try:
            await asyncio.to_thread(_generate_weekly_review)
        except Exception as e:
            print(f'[weekly-review] loop error: {e}', flush=True)


async def _calibration_loop():
    # Runs on its own slow timer (not the 5-minute health loop -- the
    # calibration report is an unindexed ts-window scan over the action_log,
    # and the health loop is supposed to stay cheap). A thread keeps the loop
    # from blocking the event loop during the DB scan. Runs BOTH calibration
    # passes -- the safety-bar pass (_calibration_adjust_pass) and the
    # review-grade pass (_review_grade_calibration_pass) -- on the same cadence.
    while True:
        try:
            await asyncio.to_thread(_calibration_adjust_pass)
            await asyncio.to_thread(_review_grade_calibration_pass)
        except Exception as e:
            print(f'[calibration] loop error: {e}', flush=True)
        await asyncio.sleep(CALIBRATION_ADJUST_INTERVAL_S)


def _activity_summary_for(agent_id):
    with _db() as conn:
        rows = conn.execute(
            'SELECT action, COUNT(*) FROM action_log WHERE agent_id = ? GROUP BY action ORDER BY COUNT(*) DESC',
            (agent_id,),
        ).fetchall()
    return {r[0]: r[1] for r in rows}


EXPECTED_MODEL_BANDS = ('low', 'mid', 'coding', 'high', 'vision')
# One missed 5s client autosave tick (index.html's setInterval(saveState,
# 5000)) plus generous slack for a slow request -- past this, treat it as
# "no browser tab is actually driving the think tank right now" rather than
# a fluke.
HEALTH_STATE_STALE_AFTER_S = 30
# Don't re-log the same standing condition every 5-minute check -- an
# alert this old is either already seen or already acted on.
# Operator-policy knob (healthAlertDedupWindowS), rebound on reload.
HEALTH_ALERT_DEDUP_WINDOW_S = 12 * 3600
# Behavioral anomaly detection: an agent firing more than this
# many spend-inducing tool calls (browse/execute/curl/chat) within this
# window is treated as a likely runaway loop / injection-driven churn -- the
# same shape as an escalation storm the project survived. The health check
# flags it so it's caught while it's happening, not after the budget's gone.
ANOMALY_WINDOW_S = 15 * 60
ANOMALY_AGENT_TOOL_THRESHOLD = 60  # >60 tool calls / 15 min per agent
# Jev availability: how far back to measure Jev decision
# failures, and what failure RATE (not raw count) trips the degraded alert.
# Jev is a single-model/single-provider SPOF with deliberately no circuit
# breaker, so the health check is what surfaces an outage instead of the
# colony silently running deterministic fallbacks.
JEV_HEALTH_WINDOW_S = 60 * 60
JEV_HEALTH_FAILURE_RATE = 0.5      # >=50% of Jev calls failing = degraded
JEV_HEALTH_MIN_ATTEMPTS = 10       # don't trip on a handful of attempts
# Absolute Zero rejection signal: refinement grooms self-proposed
# work-requests, and a REJECTED carryaway is the think tank's own signal that its
# self-proposal pipeline is producing junk (trivial/ill-scoped cards). Counted
# over a day; an info-severity dashboard alert (not a player push -- grooming
# out junk is the normal job of refinement, so this only speaks up on a real
# pattern).
SELF_PROPOSED_REJECT_WINDOW_S = 86400
SELF_PROPOSED_REJECT_THRESHOLD = 5
# red_pipeline telemetry: quality_gate_reject rows (see _log_quality_gate_reject
# in sim.py) mark every fail-closed quality-gate send-back. Counted over a day
# so the health check can see rejects (the gate catching red content) vs.
# escapes (a red result that completed anyway -- the fail-closed invariant
# broken; any escape is a real regression). Mirrors the selfProposedRejected
# pattern: a JSON-substring match on the marker, not a JSON query.
RED_PIPELINE_WINDOW_S = 86400
# METR reliability-at-horizon: does the colony actually FINISH
# the work it starts? Measured from the action_log's task_assigned ->
# task_completed pairs (paired on the taskId the details JSON carries) over a
# rolling window: the median completion time plus the fraction of assigned
# tasks completed within the fast (1h) and full (24h) horizons. A colony that
# starts lots of tasks but finishes few within the horizon has poor
# reliability -- work turning into ceremony or stalling -- even if raw
# "task_completed" counts look healthy.
METR_HORIZON_S = 86400
METR_FAST_HORIZON_S = 3600
METR_MIN_ASSIGNED = 5             # don't judge reliability on a handful of tasks
METR_FAST_COMPLETION_RATE = 0.3   # <30% of started tasks done within 1h = slow to finish
METR_SLOW_MEDIAN_HOURS = 6.0      # median completion over 6h = work is stalling


# Coordination-imbalance scalar: Reddit's Hot ranking is one of
# the few ranking formulas with a genuine DECAY built in -- a post's score is
# (reaction signal) / age^decay, so it matters while fresh and fades over a
# couple of days. The coordination check before this measured a raw 24h count
# of review/escalation/re-queue actions vs shipped work as a RATIO -- so the
# Sep 26 restart storm (79 escalations vs 3 shipped in the same hour) read as
# an acute pathology even a week later, once every artifact was stale. The
# decayed signals here borrow the half-life idea while staying NEGATIVE-safe
# (cooperation can be net-good, so the imbalance is HALF of a log
# COMPRESSION, not a division): each ceremony/progress action contributes
# exp(-age * ln2 / half_life) over a week horizon, and
# `imbalance = log10(1+C) - log10(1+P)` turns that into a continuous scalar.
# 79 fresh vs 3 shipped reads ~1.30 (loud); the same burst two days old has
# decayed every event ~16x and reads ~0.18 (quiet) -- the property the count
# ratio could never express. The full horizon emits from the alert message,
# decayed signals from the /api/health payload so a client can trend them.
_IMBALANCE_HORIZON_S = 7 * 86400          # older than a week is noise, not a signal
_IMBALANCE_HALF_LIFE_S = 12 * 3600        # Reddit uses ~45,000s; a day of age ~= 4x weight loss
_COORDINATION_INFO_SCORE = 0.5
_COORDINATION_WARNING_SCORE = 1.0
_COORDINATION_MIN_CEREMONY_SIGNAL = 3.0  # find the imbalance within a real ceremony signal
# Rework/loop telemetry: finished work that had to go back is process debt.
# The alert needs BOTH a minimum loop volume (a couple of requeues is noise)
# and a low straight-through rate (progress / (progress + rework)) to trip.
_REWORK_WINDOW_S = 86400
_REWORK_MIN_LOOPS_FOR_ALERT = 5
_REWORK_LOW_STRAIGHT_THROUGH = 0.5


def _decayed_signal(timestamps, now, half_life_s=None, horizon_s=None):
    """Sum of exponentially-decayed event weights: each event contributes
    exp(-age * ln2 / half_life). `timestamps` is an iterable of epoch-SECOND
    timestamps (this file's `now` everywhere is epoch seconds). Events older
    than the horizon are excluded entirely; `now` is injected for pures
    testability."""
    half_life_s = half_life_s or _IMBALANCE_HALF_LIFE_S
    horizon_s = horizon_s or _IMBALANCE_HORIZON_S
    total = 0.0
    for ts in timestamps:
        age = now - ts
        if age < 0 or age > horizon_s:
            continue
        total += math.exp(-age * math.log(2) / half_life_s)
    return total


def _imbalance_score(ceremony_signal, progress_signal):
    """log10-compressed difference between ceremony weight and shipped-work
    weight. 0.0 when balanced; >0 when ceremony outruns shipped work.
    Fresh 79-vs-3 => log10(80) - log10(4) ~= 1.30; the same burst aged two
    days (both signals ~16x lighter) => log10(6) - log10(1.25) ~= 0.68 --
    steady-state imbalance decays with its cause."""
    return math.log10(1.0 + max(0.0, ceremony_signal)) - math.log10(1.0 + max(0.0, progress_signal))


def _health_alerts_for_signals(signals):
    # Pure decision logic, deliberately separated from the DB reads in
    # compute_health_snapshot -- lets the actual thresholds be tested
    # with synthetic inputs instead of against whatever real data
    # happens to be in think_tank.db at test time.
    alerts = []

    def alert(category, severity, message):
        alerts.append({'category': category, 'severity': severity, 'message': message})

    # Deliberately keyed off work_queue_due_size, not the raw total -- an
    # item scheduled for later (notBefore) is SUPPOSED to sit untouched
    # with no tab open; that's not stuck, it's just not time yet.
    if signals['work_queue_due_size'] and not signals['think_tank_active']:
        alert('work_queue', 'info',
              f'{signals["work_queue_due_size"]} due item(s) queued but no browser tab has saved state in '
              f'{signals["seconds_since_last_save"]:.0f}s -- nothing will drain the queue until one is open')

    if signals['work_items_abandoned_last_24h'] > 0:
        alert('work_queue', 'warning',
              f'{signals["work_items_abandoned_last_24h"]} work item(s) abandoned in the last 24h '
              f'after repeated failed assignment attempts')

    if signals['login_failures_last_hour'] >= 5:
        alert('security', 'warning', f'{signals["login_failures_last_hour"]} failed login attempt(s) in the last hour')

    for action_name, count in signals['blocked_or_failed_actions_last_hour'].items():
        if count >= 10:
            alert('capability', 'warning', f'{count} blocked/failed "{action_name}" call(s) in the last hour')

    # Behavioral anomaly: an agent firing an abnormal volume of
    # tool calls in the window is a likely runaway loop / injection-driven
    # churn -- surface it while it's happening, not after the budget's gone.
    # The threshold is re-checked here (not just in the query) so the pure
    # decision function is self-contained and independently testable.
    for agent_id, count in signals.get('tool_volume_by_agent_last_15m', {}).items():
        if count >= ANOMALY_AGENT_TOOL_THRESHOLD:
            alert('behavior', 'warning',
                  f'agent "{agent_id}" made {count} browse/execute/curl/chat calls in the last '
                  f'{ANOMALY_WINDOW_S // 60} min -- possible runaway loop or tool churn')

    if signals['missing_model_tier_bands']:
        alert('model_tiers', 'warning',
              f'no chosen model for band(s): {", ".join(signals["missing_model_tier_bands"])} -- '
              f'refresh_model_tiers may not have found a working candidate')

    # Jev availability: a degraded/absent decisions model is a
    # SPOF (single slug, single provider, deliberately no circuit breaker) --
    # when it fails, every Jev feature silently runs its deterministic
    # fallback. The failure RATE over the window, not a raw count, is the
    # signal (a busy think tank blips a call or two and shouldn't alert; a real
    # outage fails most or all of them).
    jev_attempts = signals['jev_decision_attempts_last_hour']
    jev_failures = signals['jev_decision_failures_last_hour']
    if (jev_attempts >= JEV_HEALTH_MIN_ATTEMPTS
            and jev_failures >= jev_attempts * JEV_HEALTH_FAILURE_RATE):
        alert('jev', 'warning',
              f'{jev_failures}/{jev_attempts} Jev decision call(s) failed in the last hour -- '
              f'the decisions model may be down, colony running deterministic fallbacks')

    # API-key auth health: a streak of 401/403s from an external service
    # means its key is likely dead (revoked/expired/wrong). A single blip
    # is tolerated (some providers 403 on temporary quota); the streak
    # resets on any success. Once the streak crosses the threshold the
    # service is BLOCKED (calls fail fast). Alert; clears automatically
    # when a probe succeeds or the operator swaps the key + reloads env.
    for service, info in (signals.get('api_key_auth_failures') or {}).items():
        ago = info.get('seconds_ago') or 0
        state = (f'blocked for {info.get("blocked_for_s"):.0f}s more' if info.get('blocked')
                 else 'half-open -- one probe will be allowed through')
        alert('api_keys', 'warning',
              f'API key for "{service}" failing auth {info.get("consecutive_failures")}x in a row '
              f'(last: HTTP {info.get("status_code")}, {ago:.0f}s ago) -- {state}; '
              f'swap the key in .env and reload env to clear')

    # Coordination-imbalance scalar: replaced the raw 24h ceremony
    # COUNT-ratio check -- {ceremony} and {progress} still read as the 24h
    # counts for the message, but the TRIP is the decayed score (Reddit-Hot
    # style, see _decayed_signal/_imbalance_score): a fresh burst where
    # process outruns shipped work is loud, and the same artifacts two days
    # old read quiet, instead of an eternal count-ratio alert.
    ceremony = signals['ceremony_actions_last_24h']
    progress = signals['progress_actions_last_24h']
    ceremony_signal = signals['ceremony_signal']
    progress_signal = signals['progress_signal']
    # Derived here (not read from the payload) so this pure decision function
    # stays self-contained and independently testable, like the rest of it.
    score = _imbalance_score(ceremony_signal, progress_signal)
    if ceremony_signal >= _COORDINATION_MIN_CEREMONY_SIGNAL and score >= _COORDINATION_INFO_SCORE:
        severity = 'warning' if score >= _COORDINATION_WARNING_SCORE else 'info'
        alert('coordination', severity,
              f'coordination imbalance {score:.2f}: {ceremony_signal:.1f} review/escalation/re-queue '
              f'vs {progress_signal:.1f} shipped action weight (decayed over a week) -- '
              f'{ceremony} ceremony / {progress} shipped in the last 24h; process may be '
              f'outrunning actual work')

    # Loop/rework rate: finished work that had to go back is process debt --
    # the "61% of requests looped back" shape. A low straight-through rate
    # with real loop volume means the review pipeline is redoing work instead
    # of finishing it. Requires BOTH a minimum loop count (a couple of
    # requeues is noise) and a straight-through rate below the floor.
    loops = signals.get('rework_loops_last_24h', 0)
    straight_through = signals.get('straight_through_rate')
    spirals = signals.get('fix_spawn_spirals_last_24h', 0)
    spiral_tasks = signals.get('fix_spawn_tasks_last_24h', 0)
    spiral_note = ''
    if spirals:
        spiral_note = (f' Also {spirals} "Fix:" retry spiral(s) minted {spiral_tasks} task id(s) '
                       f'for the same root work -- each retry is a fresh task id, so loop '
                       f'detection over task ids cannot see the churn.')
    if (loops >= _REWORK_MIN_LOOPS_FOR_ALERT and straight_through is not None
            and straight_through < _REWORK_LOW_STRAIGHT_THROUGH):
        alert('rework', 'warning',
              f'{loops} rework loop(s) in the last 24h across '
              f'{signals.get("looped_tasks_last_24h", 0)} task(s) '
              f'(${signals.get("loop_cost_usd_last_24h", 0.0):.2f} accrued on looped work) '
              f'-- straight-through rate {straight_through:.0%}; the review pipeline is '
              f'redoing work instead of finishing it' + spiral_note)
    elif spirals:
        # A spiral with quiet loop metrics is exactly the shape the taskId
        # join misses (7 "Fix: Act on mail" cards in 30 minutes looked like
        # unrelated fresh work) -- speak up at info so the digest shows it.
        alert('rework', 'info',
              f'{spirals} "Fix:" retry spiral(s) minted {spiral_tasks} task id(s) for the '
              f'same root work in the last 24h -- each retry is a fresh task id, so the '
              f'rework loop counter cannot see it; the root task is churning without resolving'
              + (f' ({auto_closed_n} task(s) auto-closed as quality pipeline failures '
                 f'in the same window)' if (auto_closed_n := signals.get('task_auto_closed_last_24h', 0)) else ''))

    # Absolute Zero rejection signal: a rising count of self-proposed
    # work-requests groomed OUT at refinement means the think tank's own proposals are
    # low-value/trivial -- the default failure mode when agents pick their own next
    # work. Info severity: a dashboard signal, not a player-spam push (rejecting
    # junk is refinement's normal job; this speaks up only on a real pattern).
    if signals['self_proposed_rejected_last_24h'] >= SELF_PROPOSED_REJECT_THRESHOLD:
        alert('coordination', 'info',
              f'{signals["self_proposed_rejected_last_24h"]} self-proposed work request(s) rejected at '
              f'refinement in the last 24h -- proposals trending trivial/ill-scoped; '
              f'coach medium-difficulty cards (Absolute Zero)')

    # red_pipeline telemetry: the fail-closed quality gate caught N red content
    # results in the last 24h (that's the gate working -- routine, info level
    # only) -- but ANY red result that ESCAPED to done means the fail-closed
    # invariant broke (a regression in the gate), so a single escape is a
    # warning, not a trend signal.
    if signals['red_pipeline_escapes_last_24h']:
        alert('quality_gate', 'warning',
              f'{signals["red_pipeline_escapes_last_24h"]} red-pipeline result(s) ESCAPED the '
              f'fail-closed quality gate in the last 24h -- a red result completed as done; '
              f'check _task_cycle fail-closed branches immediately')
    elif signals['red_pipeline_rejects_last_24h']:
        alert('quality_gate', 'info',
              f'{signals["red_pipeline_rejects_last_24h"]} red-pipeline result(s) caught and sent '
              f'back by the fail-closed quality gate in the last 24h')

    # METR reliability-at-horizon: a colony that STARTS tasks but
    # rarely FINISHES them within the horizon has poor reliability -- work
    # turning into ceremony or stalling -- even when raw task_completed counts
    # look healthy. Gated on a minimum number of assignments so a quiet think tank
    # with 2 tasks and 0 finishes isn't judged on noise.
    assigned = signals['task_assigned_last_24h']
    fast_rate = signals['task_fast_completion_rate']
    median_h = signals['task_median_completion_hours']
    if assigned >= METR_MIN_ASSIGNED:
        if fast_rate is not None and fast_rate < METR_FAST_COMPLETION_RATE:
            retired = signals.get('task_abandoned_last_24h', 0)
            cancelled = signals.get('task_cancelled_last_24h', 0)
            failed = signals.get('task_failed_last_24h', 0)
            in_flight = signals.get('task_in_flight_last_24h', 0)
            causes = ''
            if retired:
                causes += f'; {retired} retired after a pipeline failure'
            if cancelled:
                causes += f'; {cancelled} cancelled'
            if failed:
                causes += f'; {failed} failed'
            if in_flight:
                causes += f'; {in_flight} still in flight'
            if not causes:
                causes = '; the rest did not finish'
            alert('throughput', 'warning',
                  f'only {fast_rate * 100:.0f}% of {assigned} task(s) assigned in the last 24h finished within '
                  f'{METR_FAST_HORIZON_S // 3600}h'
                  + (f' (median for finished work {median_h * 60:.0f} min)'
                     if median_h is not None and median_h < 1
                     else (f' (median for finished work {median_h:.1f}h)'
                           if median_h is not None else ''))
                  + causes
                  + ' -- colony is not finishing work it starts')
        elif median_h is not None and median_h > METR_SLOW_MEDIAN_HOURS:
            alert('throughput', 'info',
                  f'median task completion {median_h * 60:.0f} min across {assigned} task(s) assigned in the last 24h '
                  f'-- work taking unusually long to finish')

    # Admin gap 1: aging in-flight work. A non-bug card wedged past its budget
    # (walking forever, or still working long after workUntil) is the stale-work
    # sweep's domain -- but its PRESENCE here means the sweep hasn't re-planned
    # it yet, so the digest speaks up instead of waiting for a later pass.
    if signals.get('aging_in_flight_work'):
        alert('work_queue', 'warning',
              f'{signals["aging_in_flight_work"]} in-flight task(s) wedged past their work budget '
              f'-- the stale-work sweep will re-plan them; check why work is not resolving')

    # Open escalations: an incident the on-call hasn't put to bed yet. A single
    # pending escalation is normal incident response; a stack of unresolved
    # products is a signal the pipeline is backing up.
    if signals.get('open_escalations', 0) >= 2:
        alert('escalations', 'warning',
              f'{signals["open_escalations"]} open escalation(s) -- more than one incident '
              f'awaiting an on-call restore')

    # Bank over-cap: a service spent past its cap -- the director teller's
    # warning, surfaced to the chief of staff digest (not just the bank room readout a
    # director has to open on purpose). Spend is real money, so this rides
    # 'warning' and pushes like the other spend-facing signals.
    if signals.get('bank_over_cap'):
        alert('bank', 'warning',
              f'over-cap spend on: {", ".join(signals["bank_over_cap"])} -- '
              f'${signals.get("bank_used", 0.0):.2f} of ${signals.get("bank_cap", 0.0):.2f} '
              f'cumulative; directors should reallocate or raise a cap')

    # OpenRouter live account funds: the LIVE account-wide balance from the
    # /api/v1/credits probe (account-wide total_credits purchased minus
    # total_usage spent), distinct from the think tank's own spend ledger
    # above. The percentage alerts fire on THIS deposit, not on the monthly
    # plan -- e.g. $10 deposited and $8 spent is 80% used (warning); $9 spent
    # is 90% (critical). No deposit (totalCredits <= 0) skips the math so a
    # never-funded account doesn't divide by zero or cry wolf. The monthly
    # plan ($50 expected) is separate gauge context, not this denominator.
    credits = signals.get('openrouter_credits')
    if credits and (credits.get('totalCredits') or 0) > 0:
        total = float(credits.get('totalCredits') or 0)
        used = float(credits.get('totalUsage') or 0)
        remaining = max(0.0, total - used)
        used_pct = used / total
        spent_msg = (f'${used:.2f} of the ${total:.2f} deposited ({used_pct:.0%} used), '
                     f'${remaining:.2f} remaining')
        if used_pct >= 1 - OPENROUTER_CREDITS_CRIT_PCT:
            alert('budget', 'critical',
                  f'OpenRouter has spent {used_pct:.0%} of deposited funds -- {spent_msg}. '
                  f'Top up now or model calls will fail closed')
        elif used_pct >= 1 - OPENROUTER_CREDITS_WARN_PCT:
            alert('budget', 'warning',
                  f'OpenRouter has spent {used_pct:.0%} of deposited funds -- {spent_msg}. '
                  f'Top up soon or model calls will fail closed')
    elif not credits and signals.get('openrouter_api_key_set'):
        # Key is set but the probe yielded nothing (network failure or a
        # revoked key the auth tracker hasn't seen yet). Info severity +
        # the 12h (category, severity) dedup window keeps a flaky network
        # from becoming a push-spam source; a dead key also trips the
        # api_keys auth-streak alert once real calls start 401/403-ing.
        alert('budget', 'info',
              'OpenRouter credits probe unavailable (network error or invalid key) -- '
              'account balance gauge is empty until the next successful probe')

    return alerts


def _count_due_work_items(work_queue, now_ms):
    # notBefore (tasks.js's queueWork) is a real epoch-MILLISECOND
    # timestamp for "can't start this yet" (matching JS's Date.now()) --
    # a scheduled-for-later item sitting in the queue is expected and
    # fine, not stuck. Pulled out of compute_health_snapshot as its own
    # function specifically so the unit conversion (this file's `now` is
    # epoch SECONDS, like everywhere else in serve.py) is something a
    # test can pin down directly, rather than trusting it inline.
    return sum(1 for item in work_queue if not item.get('notBefore') or item['notBefore'] <= now_ms)


def _aging_in_flight_work(data, now_ms):
    """Count non-bug tasks wedged in 'walking'/'working' past any legitimate
    budget -- the health-snapshot mirror of sim's stale-work sweep (SM gap 1),
    so the periodic admin digest surfaces the same wedged work the sweep will
    re-plan, instead of only the queue-length/abandoned signals. Mirrors the
    sweep's own thresholds: 'walking' older than STALE_WORK_TIMEOUT_MS, or
    'working' still working more than STALE_WORK_BUDGET_GRACE_S past workUntil.
    Ages exclude recorded pause/dormancy windows, exactly like the sweep, so a
    sleeping tank is not reported as wedged work. Pure read over the state
    blob -- no DB, no network, testable directly."""
    import sim as _sim
    tasks = data.get('tasks') or {}
    if not isinstance(tasks, dict):
        return 0
    n = 0
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        if task.get('status') not in ('walking', 'working'):
            continue
        if task.get('taskType') == 'bug' or task.get('incident') or task.get('shadow') or task.get('reviewOf'):
            continue  # bugs own an alarm; shadows/reviews are other watchdogs' domain
        if not (task.get('openedAt') or task.get('assignedAt') or task.get('createdAt')):
            continue  # no start stamp -> cannot judge age (matches the sweep)
        if task.get('status') == 'walking':
            age_ms = _sim._task_active_age_ms(data, task, now_ms)
            if age_ms is None or age_ms < _sim.STALE_WORK_TIMEOUT_MS:
                continue
        else:
            grace_ms = _sim.STALE_WORK_BUDGET_GRACE_S * 1000
            if now_ms <= _sim._effective_work_deadline_ms(data, task, now_ms) + grace_ms:
                continue
        n += 1
    return n


def _rework_metrics(now):
    """Loop/rework telemetry: how much finished work had to go back, and what
    that rework cost in real spend. Straight-through rate (progress /
    (progress + rework)) is computed by the caller from the 24h counts. Loop
    cost is the per-task ledger spend (by taskId bucket, falling back to the
    title key) of the DISTINCT tasks that looped in the window -- the money
    already spent on work that had to be redone, never double-counted when a
    task loops twice. Pure DB reads, no network, no LLM spend."""
    with _db() as conn:
        rows = conn.execute(
            f"SELECT details FROM action_log WHERE action IN "  # nosec B608 -- fixed constant tuple, values parameterized
            f"({','.join('?' for _ in _REWORK_ACTIONS)}) AND ts > ?",
            (*_REWORK_ACTIONS, now - _REWORK_WINDOW_S),
        ).fetchall()
    loops = len(rows)
    looped = {}
    for (details,) in rows:
        if not details:
            continue
        try:
            d = json.loads(details)
        except Exception:
            continue
        key = d.get('taskId') or d.get('title')
        if key and key not in looped:
            looped[key] = d
    loop_cost = 0.0
    if looped:
        ledger = _spend_ledger_read() or {}
        for d in looped.values():
            used = 0.0
            tid = d.get('taskId')
            if tid:
                used = float((ledger.get(_task_spend_key(tid)) or {}).get('used', 0) or 0)
            if not used and d.get('title'):
                used = float((ledger.get(d['title']) or {}).get('used', 0) or 0)
            loop_cost += used
    return {'loops': loops, 'looped_tasks': len(looped),
            'loop_cost_usd': round(loop_cost, 4)}


_FAILURE_NOTE_MARKERS = (
    'execution exited', 'pipeline', 'incomplete', 'truncated', 'heredoc',
    'not executed', 'no tests', 'failed',
)


def _unfinished_task_causes(task_ids):
    """Classify assigned-but-uncompleted tasks by their terminal state.

    The action log is authoritative for NEW auto-closes (task_auto_closed),
    but the window before that logging existed carries 290 pipeline failures
    marked done with only a failure note. Reading the task record (live or
    archived) recovers the real cause instead of calling every one of them
    "still in flight".
    """
    out = {'retired': 0, 'cancelled': 0, 'failed': 0, 'in_flight': 0}
    if not task_ids:
        return out
    live = {}
    archived = {}
    with _db() as conn:
        try:
            row = conn.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
        except Exception:
            row = None
        if row and row[0]:
            try:
                live = (json.loads(row[0]).get('tasks') or {})
            except Exception:
                live = {}
        try:
            rows = conn.execute('SELECT id, blob FROM task_archive').fetchall()
        except Exception:
            rows = []
    for tid, blob in rows:
        if tid not in task_ids:
            continue
        try:
            archived[tid] = json.loads(blob)
        except Exception:
            continue
    for tid in task_ids:
        task = live.get(tid) or archived.get(tid) or {}
        status = task.get('status')
        note = (task.get('note') or '').lower()
        if status == 'done' and (task.get('_pipelineFailedAt')
                                 or any(marker in note for marker in _FAILURE_NOTE_MARKERS)):
            out['retired'] += 1
        elif status == 'cancelled':
            out['cancelled'] += 1
        elif status == 'failed':
            out['failed'] += 1
        else:
            out['in_flight'] += 1
    return out
