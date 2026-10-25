

def _task_horizon_metrics(now):
    """METR reliability-at-horizon, computed from action_log's
    task_assigned -> task_completed pairs (joined in Python on the taskId the
    details JSON carries -- no JSON SQL, matching the rest of this file). Pure
    DB reads, no network.

    Pairing rules (retuned after the standing-duty-card incident):

    - Each completion pairs with the LATEST assignment of the same task id
      that STRICTLY PRECEDES it. The previous join kept the first completion
      per task id, so a duty card re-assigned after completing (task-1
      "Maintain tooling": 39 pickups/24h) paired its later assignments with
      the old completion and produced impossible NEGATIVE durations
      (median -13h) that made a healthy colony read as stalled.
    - Distinct task ids are the denominator. Re-assignment echoes of one
      standing card count once, not 39 times.
    - A task counts as fast/horizon-completed when ANY of its evidenced
      assign->complete cycles fits the horizon; its most recent pickup may
      still be in flight at snapshot time, which must not erase the finished
      cycles before it. Tasks with no completion yet count against the
      rates -- that is precisely the reliability being measured."""
    with _db() as conn:
        assigns = conn.execute(
            "SELECT details, ts FROM action_log WHERE action = 'task_assigned' AND ts > ?",
            (now - METR_HORIZON_S,),
        ).fetchall()
        completions = conn.execute(
            "SELECT details, ts FROM action_log WHERE action = 'task_completed' AND ts > ?",
            (now - METR_HORIZON_S,),
        ).fetchall()
        retired_rows = conn.execute(
            "SELECT details FROM action_log WHERE action = 'task_auto_closed' AND ts > ?",
            (now - METR_HORIZON_S,),
        ).fetchall()

    def _tid(details):
        try:
            tid = json.loads(details).get('taskId') if details else None
        except Exception:
            tid = None
        return tid

    assigns_by = {}
    for details, ts in assigns:
        tid = _tid(details)
        if tid and tid not in assigns_by:
            assigns_by[tid] = []
        if tid:
            assigns_by[tid].append(ts)
    comps_by = {}
    for details, ts in completions:
        tid = _tid(details)
        if tid and tid not in comps_by:
            comps_by[tid] = []
        if tid:
            comps_by[tid].append(ts)
    retired = {_tid(details) for (details,) in retired_rows} - {None}

    durations = []
    fast = full = 0
    completed_count = 0
    for tid in assigns_by:          # distinct tasks = the denominator
        epochs = sorted(assigns_by[tid])
        pairs = []
        for comp_ts in sorted(comps_by.get(tid, ())):
            prev = None
            for a_ts in epochs:
                if a_ts < comp_ts:
                    prev = a_ts
                else:
                    break
            if prev is not None:
                pairs.append(comp_ts - prev)
        if not pairs:
            continue                # assigned in-window, never finished
        completed_count += 1
        if min(pairs) <= METR_FAST_HORIZON_S:
            fast += 1
        if min(pairs) <= METR_HORIZON_S:
            full += 1
        durations.extend(pairs)
    sorted_durs = sorted(durations)
    n = len(sorted_durs)
    if n == 0:
        median_h = None
    elif n % 2 == 1:
        median_h = sorted_durs[n // 2] / 3600.0
    else:
        median_h = (sorted_durs[n // 2 - 1] + sorted_durs[n // 2]) / 2 / 3600.0
    causes = _unfinished_task_causes(set(assigns_by) - set(comps_by))
    return {
        'assigned': len(assigns_by),
        'completed': completed_count,
        # Assigned in the window AND retired by the pipeline: a distinct
        # terminal from "still in flight" and from a genuine completion.
        # The logged auto-closes win; the task record fills the pre-logging
        # window so a historical failure is not misreported as slow.
        'abandoned': max(len(retired & set(assigns_by)), causes['retired']),
        'cancelled': causes['cancelled'],
        'failed': causes['failed'],
        'in_flight': causes['in_flight'],
        'median_completion_hours': round(median_h, 2) if median_h is not None else None,
        'fast_completion_rate': round(fast / len(assigns_by), 4) if assigns_by else None,
        'horizon_completion_rate': round(full / len(assigns_by), 4) if assigns_by else None,
    }


def _task_fix_spirals(now, min_tasks=3):
    """"Fix:" retry spirals: distinct task ids minted in the last 24h whose
    titles reduce to the same root after stripping the 'Fix: ' prefix and the
    '(agent)' suffix. Each review retry creates a NEW task id, so loop
    detection over task ids never sees a spiral (trudy churned 7 "Fix: Act on
    mail" cards in 30 minutes) -- counting title roots catches it. Pure DB
    reads; returns {'spirals': n, 'tasks': total task ids involved}."""
    fix_re = re.compile(r'^\s*fix\s*:\s*(.+?)\s*$', re.IGNORECASE)
    by_root = {}
    with _db() as conn:
        rows = conn.execute(
            "SELECT details FROM action_log WHERE action = 'task_assigned' AND ts > ?",
            (now - METR_HORIZON_S,),
        ).fetchall()
    for (details,) in rows:
        try:
            data = json.loads(details) if details else {}
        except Exception:
            continue
        title = (data.get('title') or '').strip()
        tid = data.get('taskId')
        if not title or not tid:
            continue
        m = fix_re.match(title)
        if not m:
            continue
        root = re.sub(r'\s*\([^)]*\)\s*$', '', m.group(1)).strip().lower()
        if not root:
            continue
        by_root.setdefault(root, set()).add(tid)
    spirals = [s for s in by_root.values() if len(s) >= min_tasks]
    return {'spirals': len(spirals), 'tasks': sum(len(s) for s in spirals)}


def compute_health_snapshot():
    # Every signal here comes from data the app already writes for other
    # reasons (kv_state, action_log, model_tiers) -- no network calls, no
    # LLM spend, so this is free to run on a timer. Built to close the
    # "no monitoring layer -- everything gets caught reactively" gap.
    now = time.time()

    try:
        with _db() as conn:
            state_row = conn.execute('SELECT blob, updated_at FROM kv_state WHERE id = 1').fetchone()
    except Exception as e:
        return {'checked_at': now, 'db_ok': False, 'alerts': [
            {'category': 'db', 'severity': 'critical', 'message': f'think_tank.db unreachable: {e}'}
        ]}

    if state_row is None:
        think_tank_active, seconds_since_last_save, work_queue_size, work_queue_due_size, agents_count = False, None, 0, 0, 0
        aging_in_flight, open_escalations, bank_over_cap, bank_used, bank_cap = 0, 0, [], 0.0, 0.0
    else:
        blob, updated_at = state_row
        seconds_since_last_save = now - updated_at
        think_tank_active = seconds_since_last_save < HEALTH_STATE_STALE_AFTER_S
        data = json.loads(blob)
        work_queue = data.get('workQueue', [])
        work_queue_size = len(work_queue)
        work_queue_due_size = _count_due_work_items(work_queue, now * 1000)
        agents_count = len(data.get('agents', {}))
        # Admin gap 1: aging in-flight work -- a non-bug task wedged past its
        # budget (the stale-work sweep's domain, SM gap 1) is a health signal
        # here too: it means the sweep hasn't re-planned it yet, so the digest
        # surfaces it instead of waiting for a later pass. While DORMANT the
        # sim is intentionally paused, so wall age here is not wedged work and
        # the sweep cannot run; reporting it would be a false alarm.
        aging_in_flight = 0 if _dormant() else _aging_in_flight_work(data, now * 1000)
        # Open escalations: a pending one, plus products with an unresolved
        # escalation record -- an incident the on-call hasn't put to bed yet.
        open_escalations = (1 if data.get('_pendingEscalation') else 0) + len(
            data.get('_escalatedProducts') or {})
        # Bank over-cap: any service at or past its spend cap (the director
        # teller's warning, surfaced to the chief of staff digest -- not just the bank
        # room readout a director has to open on purpose).
        bank_view = _bank_budget_view(data)
        bank_over_cap = [row['service'] for row in bank_view.values() if row['over']]
        bank_used = sum(row['used'] for row in bank_view.values())
        bank_cap = sum(row['cap'] for row in bank_view.values())

    with _db() as conn:
        abandoned = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE action = 'work_item_abandoned' AND ts > ?",
            (now - 86400,),
        ).fetchone()[0]
        login_failures = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE action = 'login_failed' AND ts > ?",
            (now - 3600,),
        ).fetchone()[0]
        # A plain LIKE against the stored JSON details -- not a proper
        # JSON query, but every real blocked/failed case (see the
        # log_action call sites for browse/curl/execute) writes a
        # "blocked" decision or a "..._failed" decision string, so this
        # is a cheap, real match rather than a guess.
        blocked_or_failed_rows = conn.execute(
            "SELECT action, COUNT(*) FROM action_log "
            "WHERE action IN ('browse', 'curl', 'execute') AND ts > ? "
            "AND (details LIKE '%\"blocked\"%' OR details LIKE '%_failed%') "
            "GROUP BY action",
            (now - 3600,),
        ).fetchall()
        tier_rows = conn.execute('SELECT band, chosen_at FROM model_tiers').fetchall()
        ceremony_count = conn.execute(
            f"SELECT COUNT(*) FROM action_log WHERE action IN "  # nosec B608 -- action names are a fixed constant tuple, values parameterized
            f"({','.join('?' for _ in _CEREMONY_ACTIONS)}) AND ts > ?",
            (*_CEREMONY_ACTIONS, now - 86400),
        ).fetchone()[0]
        progress_count = conn.execute(
            f"SELECT COUNT(*) FROM action_log WHERE action IN "  # nosec B608 -- action names are a fixed constant tuple, values parameterized
            f"({','.join('?' for _ in _PROGRESS_ACTIONS)}) AND ts > ?",
            (*_PROGRESS_ACTIONS, now - 86400),
        ).fetchone()[0]
        # Coordination-imbalance scalar: raw TIMESTAMPS of
        # ceremony/progress actions within the decay horizon, so the signals
        # can weight events by freshness instead of counting a week of stale
        # artifacts as if they happened now (the reason the old count-ratio
        # stayed loud after the Sep 26 restart storm faded).
        ceremony_ts = [r[0] for r in conn.execute(
            f"SELECT ts FROM action_log WHERE action IN "  # nosec B608 -- action names are a fixed constant tuple, values parameterized
            f"({','.join('?' for _ in _CEREMONY_ACTIONS)}) AND ts > ?",
            (*_CEREMONY_ACTIONS, now - _IMBALANCE_HORIZON_S),
        ).fetchall()]
        progress_ts = [r[0] for r in conn.execute(
            f"SELECT ts FROM action_log WHERE action IN "  # nosec B608 -- action names are a fixed constant tuple, values parameterized
            f"({','.join('?' for _ in _PROGRESS_ACTIONS)}) AND ts > ?",
            (*_PROGRESS_ACTIONS, now - _IMBALANCE_HORIZON_S),
        ).fetchall()]
        # Behavioral anomaly signal: per-agent tool-call volume in
        # a short window. A runaway loop or injection-driven tool churn shows
        # up as one agent firing an abnormal number of spend-inducing actions
        # (browse/execute/curl/chat) in a few minutes -- the same shape as the
        # escalation storm the project actually survived. Detected here, not
        # reactively after the budget is gone.
        tool_volume_rows = conn.execute(
            "SELECT agent_id, COUNT(*) FROM action_log "
            "WHERE action IN ('browse', 'execute', 'curl', 'chat') AND ts > ? "
            "AND agent_id IS NOT NULL AND agent_id != 'player' "
            "GROUP BY agent_id HAVING COUNT(*) >= ?",
            (now - ANOMALY_WINDOW_S, ANOMALY_AGENT_TOOL_THRESHOLD),
        ).fetchall()
        # Jev availability: decision_tape records every Jev call
        # with its ok flag, so a degrading/absent decisions model (the SPOF --
        # single slug, single provider, deliberately no circuit breaker) is
        # visible as a failure rate instead of silently falling back to
        # deterministic rules for hours. Count attempts AND failures so the
        # alert can judge the RATIO, not a raw count that a busy think tank
        # would trip on with a couple of blips.
        jev_attempt_rows = conn.execute(
            'SELECT COUNT(*), SUM(ok = 0) FROM decision_tape WHERE ts > ?',
            (now - JEV_HEALTH_WINDOW_S,),
        ).fetchone()
        # Absolute Zero rejection signal: refinement carryaway rows
        # whose details carry the selfProposedRejected marker (see
        # sim._resolve_refinement). Like the blocked/failed LIKE checks above,
        # a JSON-substring match on the marker, not a JSON query.
        self_proposed_rejected = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE action = 'refinement_carryaway' "
            "AND details LIKE '%\"selfProposedRejected\": true%' AND ts > ?",
            (now - SELF_PROPOSED_REJECT_WINDOW_S,),
        ).fetchone()[0]
        # red_pipeline telemetry: every fail-closed quality-gate reject (the
        # gate catching red content), and the subset that ESCAPED to done
        # anyway -- a broken invariant, so any nonzero escape count is the
        # regression signal. The gate logs both caught + escaped in the same
        # row (redPipelineEscaped flag), so two LIKE counts over one action.
        red_rejects = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE action = 'quality_gate_reject' "
            "AND ts > ?",
            (now - RED_PIPELINE_WINDOW_S,),
        ).fetchone()[0]
        red_escapes = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE action = 'quality_gate_reject' "
            "AND details LIKE '%\"redPipelineEscaped\": true%' AND ts > ?",
            (now - RED_PIPELINE_WINDOW_S,),
        ).fetchone()[0]
        # METR reliability-at-horizon: the action_log's
        # task_assigned/task_completed pairs (see _task_horizon_metrics) --
        # whether the colony finishes the work it starts, measured as a rate
        # at 1h/24h, not a raw completion count.
        metr = _task_horizon_metrics(now)
        spirals = _task_fix_spirals(now)
        # Auto-closed tasks: the lifecycle's give-up terminal ("quality
        # pipeline keeps failing" after N review cycles). Deliberately NOT
        # counted as completion for the METR rates -- an auto-close is
        # finished-but-failed, and hiding it would mask the churn -- but
        # surfaced so the spiral alert can quantify the wreckage.
        auto_closed = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE action = 'task_auto_closed' "
            "AND ts > ?",
            (now - METR_HORIZON_S,),
        ).fetchone()[0]
        # Rework/loop telemetry: how much finished work had to go back and
        # what that rework cost (see _rework_metrics).
        rework = _rework_metrics(now)
        # Grader agreement: the evals two-numbers principle -- every product
        # number needs the grader's own number next to it. The review-grade
        # judge (Jev) is calibrated against the mechanical pipeline anchor;
        # kappa is the base-rate-corrected agreement (raw agreement flatters
        # when most grades agree by chance).
        grader = _review_grade_calibration_report()

    chosen_bands = {row[0] for row in tier_rows}
    signals = {
        'think_tank_active': think_tank_active,
        # Sleep-not-die: True when idle auto-sleep has paused the sim (no spend/
        # churn) but the process is up and bound. Hitting this endpoint is itself
        # a wake request, so a "sleeping" think tank reads False here immediately.
        'dormant': _dormant(),
        'seconds_since_last_save': seconds_since_last_save,
        'work_queue_size': work_queue_size,
        'work_queue_due_size': work_queue_due_size,
        'agents_count': agents_count,
        'work_items_abandoned_last_24h': abandoned,
        'login_failures_last_hour': login_failures,
        'blocked_or_failed_actions_last_hour': {row[0]: row[1] for row in blocked_or_failed_rows},
        'tool_volume_by_agent_last_15m': {row[0]: row[1] for row in tool_volume_rows},
        # Jev availability: (attempts, failures) over the window. A degraded/
        # absent decisions model is a SPOF -- single slug, single provider,
        # no circuit breaker -- so this surfaces an outage as a failure rate
        # instead of letting the colony run deterministic fallbacks for hours.
        'jev_decision_attempts_last_hour': jev_attempt_rows[0],
        'jev_decision_failures_last_hour': jev_attempt_rows[1],
        'self_proposed_rejected_last_24h': self_proposed_rejected,
        # red_pipeline telemetry: rejects = the fail-closed gate catching red
        # content results; escapes = red results that completed anyway (broken
        # invariant -- any nonzero escape is a regression). Mirror of the
        # self_proposed_rejected counter, same JSON-substring marker method.
        'red_pipeline_rejects_last_24h': red_rejects,
        'red_pipeline_escapes_last_24h': red_escapes,
        # METR reliability-at-horizon: median completion time +
        # the fraction of assigned tasks finished within 1h / 24h. None means
        # too few completed tasks to judge (or no assignments at all).
        'task_assigned_last_24h': metr['assigned'],
        'task_completed_last_24h': metr['completed'],
        'task_abandoned_last_24h': metr.get('abandoned', 0),
        'task_cancelled_last_24h': metr.get('cancelled', 0),
        'task_failed_last_24h': metr.get('failed', 0),
        'task_in_flight_last_24h': metr.get('in_flight', 0),
        'task_median_completion_hours': metr['median_completion_hours'],
        'task_fast_completion_rate': metr['fast_completion_rate'],
        'task_horizon_completion_rate': metr['horizon_completion_rate'],
        # Rework/loop telemetry: finished work that looped back, the distinct
        # tasks involved, and the real $ accrued on that looped work -- the
        # article's "price every loop in money and time".
        'rework_loops_last_24h': rework['loops'],
        'looped_tasks_last_24h': rework['looped_tasks'],
        'loop_cost_usd_last_24h': rework['loop_cost_usd'],
        # Straight-through rate: the fraction of finished work that did NOT
        # loop back (progress / (progress + rework)). None when there is
        # nothing to measure. The inverse of "61% of requests looped back".
        'straight_through_rate': (progress_count / (progress_count + rework['loops'])
                                  if (progress_count + rework['loops']) else None),
        # "Fix:" retry spirals: same root title re-minted as fresh task ids,
        # a rework shape loop detection over task ids cannot see. Counted
        # separately; the rework alert speaks up when a spiral is live.
        'fix_spawn_spirals_last_24h': spirals['spirals'],
        'fix_spawn_tasks_last_24h': spirals['tasks'],
        'task_auto_closed_last_24h': auto_closed,
        # Grader agreement: samples, raw agreement, and kappa against the
        # mechanical pipeline anchor -- surfaced so the review-grade judge is
        # audited next to the grades it produces (the evals two-numbers rule).
        'review_grader_samples': grader['samples'],
        'review_grader_agreement_rate': grader['agreement_rate'],
        'review_grader_kappa': grader['kappa'],
        'model_tier_bands': {row[0]: {'chosen_at': row[1], 'age_hours': (now - row[1]) / 3600} for row in tier_rows},
        'missing_model_tier_bands': [b for b in EXPECTED_MODEL_BANDS if b not in chosen_bands],
        'ceremony_actions_last_24h': ceremony_count,
        'progress_actions_last_24h': progress_count,
        # None (not Infinity -- that's not valid JSON, and this crosses to a
        # JS client) means ceremony happened with zero shipped work to show
        # for it; the alert check below treats that the same as a very high
        # ratio.
        'ceremony_to_progress_ratio': (ceremony_count / progress_count) if progress_count else None,
        # Coordination-imbalance scalar: the decayed signals and
        # the log10-compressed score the coordination alert keys off. Sent for
        # client-side trending; the 24h COUNT keys above remain for the alert
        # message and for back-compat.
        'ceremony_signal': _decayed_signal(ceremony_ts, now),
        'progress_signal': _decayed_signal(progress_ts, now),
        'ceremony_imbalance_score': _imbalance_score(
            _decayed_signal(ceremony_ts, now), _decayed_signal(progress_ts, now)),
        # Admin gap 1: the digest's standing signals -- aging in-flight work,
        # open escalations, and Bank over-cap -- so the periodic health digest
        # covers money + wedged work + incidents, not just queue/alerts.
        'aging_in_flight_work': aging_in_flight,
        'open_escalations': open_escalations,
        'bank_over_cap': bank_over_cap,
        'bank_used': bank_used,
        'bank_cap': bank_cap,
        # API-key auth health: services whose LATEST call outcome was a
        # 401/403 (in-process tracker, not a DB read). Cleared the instant a
        # call succeeds again or the operator hits /api/env/reload.
        'api_key_auth_failures': _api_key_auth_failures_snapshot(),
        # OpenRouter account credits: live purchased-minus-spent from the
        # /api/v1/credits probe (bank cache, warmed by
        # _openrouter_credits_probe_loop -- no network call here, the
        # snapshot stays I/O-free). None = no key set or last probe failed;
        # age_s lets the chief of staff tell a cold probe from a live one. The monthly
        # plan is separate planning context ("expect to add $50 this month"),
        # NOT the alert denominator -- the percentage alerts key off the live
        # totalCredits deposit instead.
        'openrouter_credits': _OPENROUTER_CREDITS_CACHE['data'],
        'openrouter_credits_age_s': (
            (now - _OPENROUTER_CREDITS_CACHE['at'])
            if _OPENROUTER_CREDITS_CACHE['at'] else None),
        'openrouter_monthly_budget_usd': OPENROUTER_MONTHLY_BUDGET_USD,
        'openrouter_api_key_set': bool(OPENROUTER_API_KEY),
    }
    return {'checked_at': now, 'db_ok': True, 'alerts': _health_alerts_for_signals(signals), **signals}


def _persist_new_health_alerts(alerts):
    """Persist each alert that isn't already standing within the dedup window,
    and RETURN the newly-persisted ones so the health loop can push exactly
    those to the player (never the repeated standing ones -- the dedup window
    is what stops a repeat push from becoming a 5-min-spam).

    The dedup key is (category, severity) within the window -- NOT the full
    message. Exact-message dedup failed the day the first real outage hit:
    the Jev alert embeds the rolling failure counts in its text, so every
    5-min health cycle saw a 'new' message (53/98 -> 53/86 -> 44/68...) and
    re-persisted + re-pushed it, spamming the player for the whole incident.
    A standing (category, severity) row means that incident is already
    announced; a severity ESCALATION within the window is a different row and
    still pushes, so a worsening sub-cause is never swallowed."""
    if not alerts:
        return []
    now = time.time()
    persisted = []
    with _db() as conn:
        prior = set(conn.execute(
            'SELECT category, severity FROM health_alerts WHERE ts > ?',
            (now - HEALTH_ALERT_DEDUP_WINDOW_S,),
        ).fetchall())
        for a in alerts:
            key = (a['category'], a['severity'])
            if key in prior:
                continue
            conn.execute(
                'INSERT INTO health_alerts (category, severity, message, ts) VALUES (?, ?, ?, ?)',
                (a['category'], a['severity'], a['message'], now),
            )
            prior.add(key)
            persisted.append(a)
    return persisted


def _push_new_health_alerts(alerts):
    """Best-effort outbound push of a newly-persisted health alert to the
    player on both configured channels (email + Telegram), so an outage isn't
    invisible until someone opens the dashboard. Fail-closed by construction:
    each channel is a no-op when its integration isn't configured (returns
    False, never raises), mirroring every other optional-integration gate in
    this file. Only 'warning'/'critical' severities are pushed -- the 'info'
    queue-nudge is dashboard-only on purpose (it would otherwise fire every
    dedup window while idle)."""
    for alert in alerts:
        if alert.get('severity') not in ('warning', 'critical'):
            continue
        subject = f"[AI Think Tank] Health alert ({alert['category']})"
        body = alert['message']
        email_ok = _send_player_email_sync(subject, body)
        telegram_ok = send_player_telegram_sync(subject, body)
        print(f"[health-check] pushed {alert['category']} alert: email={email_ok} telegram={telegram_ok}", flush=True)


# Admin gap 1: periodic health DIGEST. The alert push above fires only on NEW
# warnings; a chief of staff also wants a readable periodic record of the whole picture
# -- alerts + Bank + aging work + escalations -- even when nothing is new. This
# writes a markdown digest to the shared library on HEALTH_DIGEST_INTERVAL_S
# (a cadence guard via an atomic conditional upsert on the settings
# table, so neither a restart nor a concurrent process can reset the
# clock into spamming near-identical digests seconds apart). Never
# load-bearing (a write failure must not crash the health loop).
# Retention: only the newest _DIGEST_KEEP digests are kept -- the
# admin dir had no prune before and accumulated 450+ files."""
_DIGEST_LOCK = threading.Lock()
_DIGEST_STAMP_KEY = 'last_health_digest_at'
_DIGEST_KEEP = 14


def _prune_old_health_digests():
    """Delete oldest health-*.md digests beyond _DIGEST_KEEP.
    Best-effort: a prune failure must never break the digest write."""
    try:
        admin_dir = os.path.join(LIBRARY_DIR, 'admin')
        digests = sorted(f for f in os.listdir(admin_dir)
                         if f.startswith('health-') and f.endswith('.md'))
    except OSError:
        return
    for stale in digests[:-_DIGEST_KEEP]:
        try:
            os.remove(os.path.join(admin_dir, stale))
        except OSError:
            pass


def _write_health_digest(snapshot):
    with _DIGEST_LOCK:
        now = time.time()
        # The conditional upsert IS the cadence guard: only a stamp
        # older than HEALTH_DIGEST_INTERVAL_S (or a corrupt, non-
        # numeric one, which CAST reads as 0.0) can be overwritten,
        # so a second process that read the same stale stamp loses
        # this race instead of both writing a digest. rowcount 0 =
        # the window is already claimed.
        with _db() as conn:
            cur = conn.execute(
                'INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at '
                'WHERE CAST(settings.value AS REAL) <= ?',
                (_DIGEST_STAMP_KEY, str(now), now, now - HEALTH_DIGEST_INTERVAL_S))
            if cur.rowcount == 0:
                return
        try:
            admin_dir = os.path.join(LIBRARY_DIR, 'admin')
            os.makedirs(admin_dir, exist_ok=True)
            path = os.path.join(admin_dir, f'health-{int(now)}.md')
            with open(path, 'w') as f:
                f.write(_health_digest_markdown(snapshot))
            _prune_old_health_digests()
        except Exception:
            pass


def _health_digest_markdown(snapshot):
    """Build the readable health digest body from a computed snapshot. Pure text
    assembly -- separately testable with a synthetic snapshot (no DB/files)."""
    stamp = time.strftime('%Y-%m-%d %H:%M', time.localtime(snapshot.get('checked_at') or time.time()))
    lines = [f'# Think Tank Health Digest -- {stamp}', '']
    alerts = snapshot.get('alerts') or []
    if alerts:
        lines.append(f'{len(alerts)} active alert(s):')
        for a in alerts:
            lines.append(f'- **[{a.get("severity")}] {a.get("category")}** -- {a.get("message")}')
    else:
        lines.append('No active alerts.')
    lines.append('')
    # Bank
    used = snapshot.get('bank_used', 0.0)
    cap = snapshot.get('bank_cap', 0.0)
    over = snapshot.get('bank_over_cap') or []
    bank_line = f'Cumulative spend: ${used:.2f} of ${cap:.2f}'
    if over:
        bank_line += f' -- OVER CAP on {", ".join(over)}'
    lines.append(f'**Bank:** {bank_line}')
    # Aging work
    aging = snapshot.get('aging_in_flight_work', 0)
    lines.append(f'**Aging in-flight work:** {aging} non-bug task(s) wedged past their budget')
    # Rework / straight-through: the loop picture in one line.
    loops = snapshot.get('rework_loops_last_24h', 0)
    straight = snapshot.get('straight_through_rate')
    straight_s = f'{straight:.0%}' if straight is not None else 'n/a'
    loop_cost = snapshot.get('loop_cost_usd_last_24h', 0.0)
    lines.append(f'**Rework:** {loops} loop(s) over {snapshot.get("looped_tasks_last_24h", 0)} '
                 f'task(s), straight-through {straight_s}, ${loop_cost:.2f} accrued on looped work')
    # Grader agreement: the review-grade judge's own number next to the grades
    # it produces -- kappa corrects raw agreement for the base rate.
    gs = snapshot.get('review_grader_samples', 0)
    if gs:
        agr = snapshot.get('review_grader_agreement_rate')
        kap = snapshot.get('review_grader_kappa')
        agr_s = f'{agr:.0%}' if agr is not None else 'n/a'
        kap_s = f'{kap:.2f}' if kap is not None else 'n/a'
        lines.append(f'**Grader:** {gs} anchored review grade(s), agreement {agr_s}, kappa {kap_s}')
    # Escalations
    esc = snapshot.get('open_escalations', 0)
    lines.append(f'**Open escalations:** {esc}')
    advice = build_optimization_advice(snapshot)
    if advice.get('recommendations'):
        focus = advice['recommendations'][0]
        lines.append(f"**Optimization focus:** {focus['title']}. {focus['action']}")
    else:
        lines.append('**Optimization focus:** no dominant constraint detected.')
    lines.append('')
    lines.append('_Periodic server-side digest; alerts push on new warnings between digests._')
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# Phase 3 slice 2: research content executor (PLAN-phase3-slice2-research.md)
#
# Re-homes the browser's runResearchTask scheduled-topic branch (tasks.js:2188)
# -- crawlAndCollect + synthesize + writeSkillFile -- onto the server, so a
# research task performs REAL work (browse > save > chat > Library write) with
# no browser. Registered as sim._content_executor by the lifespan; sim.py's
# arrival dispatch fires it on a background thread against a state snapshot, and
# its result ({'note', 'seenUrls'}) is merged by the next task_cycle pass (the
# single read-modify-write -- see the slice-1 race note).
#
# The executor calls the SAME endpoints the browser does, over loopback with the
# agent's real key: auth, rate-limit, Jev, redaction, and the passport all run
# exactly as they do for browser-driven work. This is the lowest-drift port --
# not a re-implementation of browse/save/chat internals.
SELF_BASE_URL = (os.environ.get('SELF_BASE_URL')
                 or _load_env().get('SELF_BASE_URL', 'http://localhost:8010'))


def _derive_self_base_url(bind_host, port, env_url=None):
    """The base URL self-loopback calls must use. Every consumer of
    SELF_BASE_URL is a call from THIS process back into THIS process (the
    research crawl's browse/evidence/save, chat follow-ups, allowlist
    requests), so the URL has to match the actual bind -- never a stale .env
    guess. A dead self-loopback fails silently (the crawl's evidence POST
    degrades to 'recorded' and the ledger stays empty), which is why boot
    derives it instead of trusting the file. 0.0.0.0/:: map to 127.0.0.1 (a
    client cannot connect to 0.0.0.0); an explicit bind host (e.g. a
    Tailscale IP) is used as-is. Returns (url, is_env_default)."""
    self_host = '127.0.0.1' if bind_host in ('0.0.0.0', '::') else bind_host  # nosec B104 -- string check, not a bind
    bound = f'http://{self_host}:{port}'
    env_url = env_url or SELF_BASE_URL
    return bound, (env_url == bound)
_SANDBOX_RESEARCH_ID = 'research-shared'  # index.html: RESEARCH_SANDBOX_ID
RESEARCH_CRAWL_MAX_PAGES = 50             # tasks.js crawlAndCollect -- raised 6->30->50 for deeper research
RESEARCH_SKILL_SYNTHESIS_TOKENS = 900     # runResearchTask /api/chat max_tokens
# Recurring research searches the period since the last run MINUS this
# overlap, so a discovery landing just after the previous window is caught
# (matches the Research Desk routine's "24-hour overlap to catch late
# discoveries"). Mirrors evidence.DEFAULT_OVERLAP_MS.
RESEARCH_OVERLAP_MS = 24 * 3600 * 1000


def _http_json(method, base, path, body=None, header=None, timeout=30,
               extra_headers=None):
    """Thin loopback HTTP helper mirroring world.js agentFetch(apiFetch).
    `timeout` defaults to 30s; callers doing a long model decomposition (the
    big-task planning call) pass a larger ceiling -- a 4000-token plan can
    exceed 30s on a cold model, which surfaced as flaky 502s. `extra_headers`
    carries remediation identity for change-window traffic (see
    _selfedit_remediation_exempt)."""
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {'Content-Type': 'application/json'}
    if header:
        headers['X-Agent-Key'] = header
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    effective_timeout = timeout
    if path in ('/api/chat', '/api/pipeline'):
        effective_timeout = max(timeout or 0, runtime_limits.transport(path, body))
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=effective_timeout) as resp:  # nosec B310 -- fixed SELF_BASE_URL internal calls; only SSRF-gated browse URLs use this path
            raw = resp.read().decode('utf-8', errors='replace')
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {'_raw': raw}  # e.g. /api/library/file POST returns 'saved'
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace')
        try:
            parsed = json.loads(raw) if raw else {'error': f'HTTP {e.code}'}
        except json.JSONDecodeError:
            return {'error': f'HTTP {e.code}: {raw[:200]}'}
        if isinstance(parsed, dict) and parsed.get('creditsPaused'):
            raise OpenRouterCreditsPaused(parsed.get('error') or 'OpenRouter credits are exhausted')
        return parsed
    except Exception as e:
        return {'error': f'request failed: {e}'}
    finally:
        runtime_limits.observe(path, time.monotonic() - started)


@app.get('/api/health')
async def health():
    snapshot = await asyncio.to_thread(compute_health_snapshot)
    return JSONResponse(snapshot)


@app.get('/api/optimization/advice')
async def optimization_advice(request: Request):
    if not _require_player_session(request):
        return JSONResponse({'error': 'Unauthorized -- player session required'}, status_code=401)
    snapshot = await asyncio.to_thread(compute_health_snapshot)
    return JSONResponse(build_optimization_advice(snapshot))


@app.get('/api/optimization/policies')
async def optimization_policies(request: Request):
    if not _require_player_session(request):
        return JSONResponse({'error': 'Unauthorized -- player session required'}, status_code=401)
    return JSONResponse(policies.snapshot())


@app.get('/api/sim/status')
async def sim_status():
    # Lets the client -- or a "runs closed" probe, or a human -- confirm the
    # server-side sim loop is alive by watching tick increase with no browser
    # attached. Reads the sim section of the authoritative state, not the
    # (possibly stale) in-memory singleton, so it reflects the DB's last tick.
    try:
        import sim as _sim_module
        state = await asyncio.to_thread(get_state_from_db)
    except Exception as e:
        return JSONResponse({'running': False, 'error': str(e)})
    if not state:
        return JSONResponse({'running': False, 'tick': 0, 'lastTickEpochS': None})
    return JSONResponse(_sim_module._engine.status(state))


@app.get('/api/sim/agents')
async def sim_agents():
    # Authoritative agent positions for the client's renderer when the server
    # owns movement (sim.owner == 'server'). Under client ownership this still
    # returns the current agent spatial snapshot (a no-op read) so the browser
    # can key off `owner` and stay backwards-compatible. Reads the DB directly
    # (not the stale in-memory singleton), same as /api/sim/status.
    try:
        state = await asyncio.to_thread(get_state_from_db)
    except Exception as e:
        return JSONResponse({'owner': 'unknown', 'error': str(e)})
    if not state:
        return JSONResponse({'owner': 'client', 'tick': 0, 'agents': {}})
    sim = state.get('sim', {}) if isinstance(state, dict) else {}
    snap = sim.get('agents', {}) if isinstance(sim, dict) else {}
    # Snapshot fields (x/y/dir/busy/task/inRoom/offDuty/pathActive) are the
    # flags the client blends toward when rendering; under server ownership the
    # snapshot already reflects post-step truth (see SimEngine.tick).
    return JSONResponse({
        'owner': sim.get('owner', 'client'),
        'tick': sim.get('tick', 0),
        'agents': snap,
    })


@app.get('/api/health/alerts')
async def health_alerts_endpoint(limit: int = 50):
    with _db() as conn:
        rows = conn.execute(
            'SELECT category, severity, message, ts FROM health_alerts ORDER BY ts DESC LIMIT ?',
            (limit,),
        ).fetchall()
    return JSONResponse({'alerts': [
        {'category': r[0], 'severity': r[1], 'message': r[2], 'ts': r[3]} for r in rows
    ]})


@app.get('/api/activity/summary')
async def activity_summary(agentId: str):
    # Bug: asked to reflect on the think tank with nothing
    # but a role and a vague prompt, agents confabulated confidently --
    # "quarterly review process," "Gemini writes, DeepSeek codes," one
    # agent complaining about a tool it's never had room access to touch.
    # A real, cheap GROUP BY against the same action_log every real action
    # already writes into -- grounding a retrospective in what an agent
    # actually, verifiably did, instead of a free-form guess at what a
    # think tank like this "should" contain.
    return JSONResponse({'agentId': agentId, 'counts': _activity_summary_for(agentId)})


@app.get('/api/model-tiers')
async def model_tiers():
    # Runs the real refresh once if nothing's cached yet (first boot);
    # otherwise just returns what's already chosen -- this is deliberately
    # NOT re-run on every request (real cost, and no reason for the
    # catalog to have meaningfully changed minute to minute).
    cached = get_cached_model_tiers()
    if not cached:
        try:
            fresh = await refresh_model_tiers()
            cached = {band: {'slug': m['id'], 'name': m['name'], 'price': m['price']} for band, m in fresh.items()}
        except Exception as e:
            return JSONResponse({'error': f'could not fetch/select model tiers: {e}'}, status_code=502)
    return JSONResponse(cached)


@app.post('/api/model-tiers/refresh')
async def model_tiers_refresh(request: Request):
    # An explicit, deliberate operator action (a button) -- but also
    # run AUTOMATICALLY on the tier-refresh cadence by
    # _model_tier_refresh_loop (that loop calls refresh_model_tiers()
    # directly, not this endpoint, so the gate does not affect it).
    # Operator-gated and rate-limited: a re-pick walks the whole live
    # catalog and re-scores every model, so it is real cost, not a
    # free-for-all -- the same operator boundary as the other
    # registries (player session or a director/admin agent).
    authed, actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized -- tier refresh is an operator control'}, status_code=401)
    if not check_rate_limit(f'tier-refresh:{actor}'):
        log_action(actor, 'rate_limited', {'endpoint': 'model-tiers-refresh'})
        return JSONResponse({'error': f'Rate limit exceeded -- max {RATE_LIMIT_MAX_CALLS} calls per {RATE_LIMIT_WINDOW_S}s.'}, status_code=429)
    try:
        fresh = await refresh_model_tiers()
        return JSONResponse({band: {'slug': m['id'], 'name': m['name'], 'price': m['price']} for band, m in fresh.items()})
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=502)


@app.get('/api/tier-daily-log')
async def tier_daily_log_get(band: str = '', days: int = 30):
    # Read-only history of each tier band's daily pick: slug, price, and the
    # benchmark score the pick was grounded on. The scheduling logic reads
    # this to see how each band's cost and quality moved day over day.
    return JSONResponse({'log': get_tier_daily_log(band=band or None, days=max(1, min(days, 365)))})


@app.get('/api/catalog-price-history')
async def catalog_price_history_get(modelId: str = '', days: int = 30):
    # Market-wide daily price snapshot for every model in the catalog.
    # Without modelId returns the cheapest models per day; with it returns
    # one model's price series. This is how a price drop (or spike) is
    # visible even when the model never won a tier pick.
    return JSONResponse({'history': get_catalog_price_history(model_id=modelId or None, days=max(1, min(days, 365)))})


@app.get('/api/tier-value-windows')
async def tier_value_windows_get():
    # Read-only: each band's current value ratio (score-per-dollar vs its
    # trailing 7-day median) and how long the current pick has been held. A
    # strong ratio on a fresh pick is a cheap-value window the tank is taking
    # advantage of -- the operator sees the window, not just the resulting pick.
    return JSONResponse(get_tier_value_windows())


@app.get('/api/model-benchmark-scores')
async def model_benchmark_scores_get():
    # Read-only for everyone logged in -- lets the actual selection logic
    # (refresh_model_tiers) and anyone inspecting the think tank's own
    # decisions see exactly what real, cited data each band's pick was
    # grounded in, rather than that being invisible.
    return JSONResponse({'scores': get_model_benchmark_scores()})


@app.post('/api/model-benchmark-scores')
async def model_benchmark_scores_post(request: Request):
    # Player-only, same reasoning as /api/library/ingest -- this is real
    # research data (a published benchmark score with a citation), not
    # something an agent should be able to assert about itself or another
    # model without a source backing it.
    body = await request.json()
    agent_id = body.get('agentId')
    if agent_id != 'player':
        return JSONResponse({'error': 'only the player can record model benchmark scores'}, status_code=403)
    model_id = body.get('modelId')
    benchmark = body.get('benchmark')
    score = body.get('score')
    source_url = body.get('sourceUrl')
    evaluator = (body.get('evaluator') or 'player').strip()
    if not model_id or not benchmark or not isinstance(score, (int, float)):
        return JSONResponse({'error': 'modelId, benchmark, and a numeric score are required'}, status_code=400)
    set_model_benchmark_score(model_id, benchmark, float(score), source_url, evaluator)
    log_action('player', 'model_benchmark_score_recorded', {'modelId': model_id, 'benchmark': benchmark, 'score': score, 'evaluator': evaluator, 'sourceUrl': source_url})
    return JSONResponse({'ok': True})


@app.post('/api/decide')
async def decide(request: Request):
    # Jev, not a chat model -- see _call_openrouter_decision_sync. Same
    # secure-proxy shape as /api/chat: the client builds state/questions,
    # this just forwards it with the key attached and returns the answer.
    if not OPENROUTER_API_KEY:
        return JSONResponse({'error': 'OPENROUTER_API_KEY not set in the repo-root .env'}, status_code=500)
    body = await request.json()
    # The resolved JEV model is authoritative -- a client-provided model is
    # ignored so an operator switch (env or the /api/jev/model DB setting)
    # takes effect for browser-driven calls too (jev.js sends no model; the
    # server, not the client, owns which decisions model this think tank runs on).
    model = _jev_model()
    state = body.get('state')
    questions = body.get('questions')
    agent_id = body.get('agentId')
    if not state or not questions:
        return JSONResponse({'error': 'state and questions are required'}, status_code=400)
    # Every decision must be attributable to a real agent so the log is
    # meaningful and so the sliding-window throttle can defend the budget.
    allowed, reason = _decide_allowed(agent_id)
    if not allowed:
        log_action(agent_id, 'decide_throttled', {'model': model, 'reason': reason})
        return JSONResponse({'error': reason,
                             'throttled': True,
                             'retry_after_ms': int(1000 * DECIDE_MIN_INTERVAL_S)}, status_code=429)
    # Training capture (opt-in): attribute Jev's decision calls to
    # the deciding agent; the request body carries the full state.
    if training_capture.enabled():
        training_capture.CURRENT.set(training_capture.metadata(
            state, agent_id, None, 'decision'))
    try:
        data = await asyncio.to_thread(_call_openrouter_decision_sync, model, state, questions)
        _choice, confidence, cost = _jev_choice(data)
        _accrue_spend('__jev__', cost, village_id=_sim_village_for_agent(agent_id))
        log_action(agent_id, 'decide', {'model': model, 'confidence': confidence, 'cost': cost, 'choice': _choice})
        return JSONResponse(data)
    except urllib.error.HTTPError as e:
        return JSONResponse({'error': e.read().decode()}, status_code=e.code)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)
