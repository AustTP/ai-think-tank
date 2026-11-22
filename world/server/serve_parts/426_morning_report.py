# Morning report (owner direction 2026-11-21): one nightly summary pushed to
# the operator's Telegram at 07:30 America/New_York, modeled on the sample
# night-shift dashboard the operator sent (world/telegram-media/
# tg-20261121-170304.JPG): runs + delta vs the prior night, share finished
# without a person, plans waiting on the player, spend against the cap, and
# per-village / per-agent activity across the WHOLE org (every
# villages_registry row, not just the tank itself). Unlike the alert digest
# (425), a morning report is a heartbeat: it sends even when the window was
# quiet, because "quiet night" is itself the result the operator wants to see
# once a day.

_MORNING_FAILURE_ACTIONS = (
    'task_requeued_unreachable', 'failed_work_dropped',
    'failed_work_sm_replan', 'work_item_abandoned',
    'grade_artifact_mismatch', 'work_queue_failed')

def _morning_report_enabled():
    return (os.environ.get('MORNING_REPORT_ENABLED') or '1') \
        .strip().lower() not in ('0', 'false', 'no')

def _morning_report_hour_et():
    raw = (os.environ.get('MORNING_REPORT_HOUR_ET') or '07:30').strip()
    try:
        hh, mm = raw.split(':')
        hour, minute = int(hh), int(mm)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return hour, minute
    except Exception:
        return 7, 30

def _morning_report_window_hours():
    try:
        h = float(os.environ.get('MORNING_REPORT_WINDOW_HOURS') or 12)
        return max(1.0, min(h, 24.0))
    except Exception:
        return 12.0

def _morning_et_now(now=None):
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo('America/New_York')
    except Exception:
        tz = datetime.timezone(datetime.timedelta(hours=-5))
    return datetime.datetime.fromtimestamp(
        now if now is not None else time.time(), datetime.timezone.utc).astimezone(tz)

def _morning_next_run_delay_seconds(now_ts=None):
    """Seconds until the next 07:30 ET (or configured hour) fires."""
    now_ts = now_ts if now_ts is not None else time.time()
    et = _morning_et_now(now_ts)
    hour, minute = _morning_report_hour_et()
    target = et.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= et:
        target += datetime.timedelta(days=1)
    return max(1.0, (target - et).total_seconds())

def _morning_pending_plans():
    """Plans waiting on the player: (count, oldest_created_ts) from the
    plan-approvals store (world/core/plan_approvals.py owns that database
    file -- query it through the module's own connection so the report reads
    the same store the approve/reject endpoints see)."""
    count, oldest = 0, None
    try:
        from world.core import plan_approvals
        with plan_approvals._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MIN(created_at) FROM plan_revisions "
                "WHERE status='pending'").fetchone()
            if row:
                count = int(row[0] or 0)
                oldest = float(row[1]) if row[1] else None
    except Exception:
        pass
    return count, oldest

def _morning_spend_window(cutoff, now_ts):
    """USD spent inside the window, summed from the spend ledger's byDay
    series (day granularity: a window that straddles two days charges both
    days' totals against the window -- coarse, but never understated)."""
    spent = 0.0
    try:
        ledger = _spend_ledger_read() or {}
        days = set()
        ts = cutoff
        while ts <= now_ts:
            days.add(datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc).strftime('%Y-%m-%d'))
            ts += 3600
        for bucket in ledger.values():
            if not isinstance(bucket, dict):
                continue
            by_day = bucket.get('byDay')
            if not isinstance(by_day, dict):
                continue
            for day, amount in by_day.items():
                if day in days:
                    try:
                        spent += float(amount)
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass
    return round(spent, 2)

def _morning_org_rows():
    """Every registered village, its agent count, and heartbeat age."""
    villages, teams = [], 0
    try:
        with _db() as conn:
            villages = conn.execute(
                'SELECT id, name, last_heartbeat_at, last_status '
                'FROM villages_registry ORDER BY created_at ASC').fetchall()
    except Exception:
        villages = []
    # Each read is separately best-effort: a village registered before its
    # first agent/heartbeat upsert (194/195 create these lazily) must still
    # appear in the report rather than vanish because one table is absent.
    agent_counts = {}
    try:
        with _db() as conn:
            agent_counts = dict(conn.execute(
                'SELECT village_id, COUNT(*) FROM villages_agents '
                'GROUP BY village_id').fetchall())
    except Exception:
        agent_counts = {}
    try:
        with _db() as conn:
            teams = conn.execute(
                'SELECT COUNT(DISTINCT team_id) FROM villages_teams'
            ).fetchone()[0]
    except Exception:
        teams = 0
    out = []
    now_ts = time.time()
    for vid, name, hb, status in villages:
        age_h = max(0.0, (now_ts - hb) / 3600.0) if hb else None
        out.append({'id': vid,
                    'name': name or vid,
                    'agents': int(agent_counts.get(vid, 0)),
                    'heartbeat_age_h': age_h,
                    'status': status or ''})
    return out, teams

def compile_morning_report(now=None):
    """Compile the morning report. Returns (text, stats). Pure read: no
    writes, no sends, so tests and a manual preview endpoint can call it
    freely. Every section is best-effort -- a missing table yields an
    honest 0 rather than a crash."""
    now_ts = now if now is not None else time.time()
    window_h = _morning_report_window_hours()
    cutoff = now_ts - window_h * 3600
    prev_cutoff = cutoff - window_h * 3600

    runs = 0
    prev_runs = 0
    failed = 0
    marks = ','.join('?' * len(_MORNING_FAILURE_ACTIONS))
    try:
        with _db() as conn:
            runs = conn.execute(
                'SELECT COUNT(*) FROM action_log '
                'WHERE agent_id IS NOT NULL AND ts >= ?', (cutoff,)).fetchone()[0]
            prev_runs = conn.execute(
                'SELECT COUNT(*) FROM action_log '
                'WHERE agent_id IS NOT NULL AND ts >= ? AND ts < ?',
                (prev_cutoff, cutoff)).fetchone()[0]
            failed = conn.execute(
                f'SELECT COUNT(*) FROM action_log WHERE ts >= ? '
                f'AND action IN ({marks})', (cutoff, *_MORNING_FAILURE_ACTIONS)
            ).fetchone()[0]
            top_agents = conn.execute(
                'SELECT agent_id, COUNT(*) FROM action_log '
                'WHERE agent_id IS NOT NULL AND ts >= ? '
                'GROUP BY agent_id ORDER BY COUNT(*) DESC LIMIT 8',
                (cutoff,)).fetchall()
            scoreboard = _morning_scoreboard(conn, cutoff)
    except Exception:
        top_agents = []
        scoreboard = {}
    except Exception:
        top_agents = []

    delta = 0
    if prev_runs:
        delta = round(100.0 * (runs - prev_runs) / prev_runs)

    pending, oldest_pending = _morning_pending_plans()
    oldest_h = None
    if oldest_pending:
        oldest_h = max(0.0, (now_ts - oldest_pending) / 3600.0)

    spent = _morning_spend_window(cutoff, now_ts)
    try:
        cap = float(SPEND_CAP_USD or 0)
    except Exception:
        cap = 0.0

    villages, team_count = _morning_org_rows()
    villages_total = len(villages)
    agents_total = sum(v['agents'] for v in villages)
    down = [v for v in villages
            if v['heartbeat_age_h'] is None or v['heartbeat_age_h'] > 1.0]

    et = _morning_et_now(now_ts)
    et_prev = et - datetime.timedelta(hours=window_h)
    label = (f'{et_prev.strftime("%a %d %b %H:%M")} to '
             f'{et.strftime("%a %d %b %H:%M")} ET')
    day = et.strftime('%a %d %b')

    lines = [f'Morning report {day}', f'Window: {label}', '']
    lines.append(f'Runs: {runs} ({delta:+d}% on the previous window)' if prev_runs
                 else f'Runs: {runs} (no prior window to compare)')
    auto = 100.0 if (runs and not failed) else (
        round(100.0 * (runs - failed) / runs, 1) if runs else 0.0)
    lines.append(f'Finished without a person: {auto:.1f}% ({failed} failed)')
    wait_line = f'Waiting on you: {pending}'
    if oldest_h is not None and pending:
        wait_line += f' (oldest {oldest_h:.1f}h)'
    lines.append(wait_line)
    spend_line = f'Spent in window: ${spent:.2f}'
    if cap:
        spend_line += f' (month cap ${cap:.0f})'
    lines.append(spend_line)
    lines.append('')
    lines.append(f'Org: {villages_total} villages, {team_count} teams, '
                 f'{agents_total} agents; {len(down)} quiet')
    lines.append('')
    lines.append('Villages')
    if villages:
        for v in villages:
            status = 'quiet >1h' if v in down else 'live'
            lines.append(f'- {v["name"]}: {v["agents"]} agents, {status}')
    else:
        lines.append('- no villages registered')
    lines.append('')
    lines.append('Most active agents (window)')
    if top_agents:
        for agent_id, count in top_agents:
            lines.append(f'- {agent_id}: {count} actions')
    else:
        lines.append('- no agent activity in the window')
    lines.append('')
    lines.append('')
    sb = scoreboard or {}
    lines.append('Scoreboard (verified output, window)')
    lines.append(f"- PRs opened: {sb.get('prOpened', 0)}, merged: {sb.get('prMerged', 0)}")
    lines.append(f"- Blocked at the local test gate: {sb.get('blocked', 0)}")
    lines.append(f"- Reopen signals: {sb.get('reopened', 0)}")
    lines.append(f"- Escalations needing you: {sb.get('escalations', 0)}")
    lines.append(f"- Cards completed vs auto-closed: {sb.get('completions', 0)} vs {sb.get('autoCloses', 0)}")
    lines.append('(Full record: library/analysis/morning-report-*.)')
    text = '\n'.join(lines)[:3800]
    stats = {'runs': runs, 'prevRuns': prev_runs, 'deltaPct': delta,
             'failed': failed, 'pendingPlans': pending,
             'oldestPendingH': oldest_h, 'spentWindow': spent,
             'capUsd': cap, 'villages': villages_total,
             'teams': team_count, 'agents': agents_total,
             'quietVillages': len(down), 'windowH': window_h}
    return text, stats

def morning_report_path(now=None):
    et = _morning_et_now(now)
    return (f'analysis/morning-report-{et.strftime("%Y-%m-%d")}'
            f'-{et.strftime("%H%M")}.md')

def send_morning_report(now=None):
    """Compile, file to the Library, and push to every allowlisted Telegram
    chat. Returns the library path, or None when Telegram push is off."""
    text, stats = compile_morning_report(now)
    now_ts = now if now is not None else time.time()
    rel = morning_report_path(now_ts)
    target = _safe_library_path(rel)
    if target:
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'w') as f:
                f.write(text + '\n')
        except Exception:
            pass
    sent = _telegram_send_text_sync(text)
    log_action('system', 'morning_report_sent',
               {'path': rel, 'sent': sent, **stats}, authorized=True)
    return rel if sent else None

async def _morning_report_loop():
    """Lifespan loop: sleep to the next 07:30 ET, send, repeat. Disabled by
    MORNING_REPORT_ENABLED=0 (tests set it; the loop also refuses to run
    when Telegram push is not configured)."""
    while True:
        try:
            delay = _morning_next_run_delay_seconds()
            await asyncio.sleep(delay)
            await asyncio.to_thread(send_morning_report)
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                log_action('system', 'morning_report_failed',
                           {'delayNextS': _morning_next_run_delay_seconds()},
                           authorized=True)
            except Exception:
                pass
            await asyncio.sleep(300)
def _morning_scoreboard(conn, cutoff):
    """Exported-work scoreboard (operator, 2026-11-22, kozh piece): measure
    verified output, not agent count. Deterministic action_log queries; any
    failure returns an honest 0."""
    def one(sql, params=()):
        try:
            return conn.execute(sql, params).fetchone()[0]
        except Exception:
            return 0
    pr_opened = one(
        "SELECT COUNT(*) FROM action_log WHERE ts >= ? "
        "AND action = 'change_lane_pr_opened'", (cutoff,))
    merged = one(
        "SELECT COUNT(*) FROM action_log WHERE ts >= ? "
        "AND action = 'change_lane_merged'", (cutoff,))
    reopened = one(
        "SELECT COUNT(*) FROM action_log WHERE ts >= ? "
        "AND action = 'operator_veto_window_expired'", (cutoff,))
    blocked = one(
        "SELECT COUNT(*) FROM action_log WHERE ts >= ? "
        "AND action = 'change_lane_blocked_local_tests'", (cutoff,))
    escalations = one(
        "SELECT COUNT(*) FROM action_log WHERE ts >= ? "
        "AND action LIKE 'escalation_%'", (cutoff,))
    completions = one(
        "SELECT COUNT(*) FROM action_log WHERE ts >= ? "
        "AND action = 'task_completed'", (cutoff,))
    auto_closes = one(
        "SELECT COUNT(*) FROM action_log WHERE ts >= ? "
        "AND action = 'task_auto_closed'", (cutoff,))
    return {
        'prOpened': pr_opened, 'prMerged': merged, 'reopened': reopened,
        'blocked': blocked, 'escalations': escalations,
        'completions': completions, 'autoCloses': auto_closes,
    }
