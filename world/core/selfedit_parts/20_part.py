

def revert(edit_id, by='operator'):
    """Restore an applied edit from its backup and reload. Player or admin
    only; the endpoint enforces that."""
    serve = _s()
    row = _get_row(edit_id)
    if not row:
        return False, {'error': 'unknown edit id'}
    display, status = row[0], row[1]
    if status != 'applied':
        return False, {'error': f'edit is {status!r}, not applied -- '
                                'nothing to revert'}
    backup = os.path.join(_world(), '.selfedit_backups', edit_id)
    if not os.path.isfile(backup):
        return False, {'error': 'backup missing; recover the file from git '
                                'instead (`git checkout -- world/...`)'}
    backup_candidate = os.path.join(_world(), '.selfedit_backups',
                                    f'{edit_id}.cand')
    target = _target_from_display(display)
    shutil.copyfile(backup, target)
    if is_kernel(target):
        _schedule_kernel_restart()
    else:
        _reload(target)
    _update(edit_id, status='reverted', reverted_at=time.time())
    serve.log_action(edit_id, 'self_edit_reverted', {'by': by}, authorized=True)
    return True, {'ok': True, 'status': 'reverted',
                  'backup_candidate_kept': os.path.isfile(backup_candidate)}


def status_snapshot(limit=25):
    """Bounded, newest-first view for the status tool and endpoint."""
    with _s()._db() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            'SELECT id, created, agent, path, summary, status, note, '
            'jev_confidence, validator_note, applied_at, window_id, '
            'apply_policy FROM self_edit_log '
            'ORDER BY created DESC LIMIT ?', (int(limit),)).fetchall()
    return [{'id': r[0], 'created': r[1], 'agent': r[2], 'path': r[3],
             'summary': r[4], 'status': r[5], 'note': r[6],
             'jevConfidence': r[7], 'validator': r[8], 'appliedAt': r[9],
             'windowId': r[10], 'applyPolicy': r[11] or 'idle_or_window'}
            for r in rows]


# ---------------------------------------------------------------------------
# One-shot dormancy windows (the change-request process for applying edits).
# See the docstring contract above: conflict scan -> supervisor JEV judgment
# -> player-only floor-1.0 escalation link. A window owns a wall-clock slot;
# the firing loop lives in serve (_dormancy_window_loop) and calls
# window_tick() through asyncio.to_thread.
# ---------------------------------------------------------------------------

WINDOW_MIN_DURATION_MIN = 5
WINDOW_MAX_DURATION_MIN = 240
WINDOW_DEFAULT_DURATION_MIN = 15
# Change-request lead time (owner rule, 2026-10-27): a change window must be
# requested at least 24 hours ahead so the owner has a real chance to review
# the request and use the veto link, and no more than 7 days out -- requests
# filed further ahead go stale before the window arrives; re-request closer
# to the work.
#
# The emergency lane (owner decision, 2026-10-27): changeType 'emergency' may
# use a shorter runway, but never under one hour -- the owner's veto link
# still needs a realistic chance to land. The lane is loud: the request is
# logged as emergency_change_requested and telegram-notified immediately, and
# the supervisor review + owner veto still apply unchanged. Labeling routine
# work as emergency to skip the notice period is a Rule 12 violation.
WINDOW_MIN_LEAD_S = 24 * 60 * 60
WINDOW_MAX_LEAD_S = 7 * 24 * 60 * 60
WINDOW_EMERGENCY_MIN_LEAD_S = 60 * 60

# Validator turn (operator decision, 2026-10-19). The tank is dormant while a
# change window holds for validation, so the sim tick cannot run and the
# validator cannot be reached through the normal mailbox -> task path. The
# dormancy loop therefore runs ONE narrow decision turn for the named
# validator only. It is the same two-agent contract, not a new approver: the
# decision still goes through validate_window(), and only the window's own
# validator identity may record it.
VALIDATOR_TURN_MAX_ATTEMPTS = 3
VALIDATOR_TURN_RETRY_S = 60
VALIDATOR_TURN_MAX_DIFF_CHARS = 16000
VALIDATOR_TURN_MAX_TAIL_CHARS = 6000
_VALIDATION_LOCK = threading.Lock()
MAX_WINDOW_REASON_LEN = 500
MAX_WINDOW_TITLE_LEN = 200
MAX_WINDOW_TEXT_LEN = 4000
MAX_MANIFEST_ITEMS = 100
MAX_BLACKOUT_ITEMS = 50

WINDOW_CHANGE_TYPES = ('standard', 'normal', 'emergency')
WINDOW_RISK_LEVELS = ('low', 'medium', 'high', 'critical')
WINDOW_SERVICE_MODES = ('idle_ok', 'batch', 'event_driven',
                        'always_on_trading')
HIGH_RISK_LEVELS = ('high', 'critical')
HIGH_RISK_SERVICE_MODES = ('event_driven', 'always_on_trading')
MAX_TRADING_WINDOW_MIN = 5
MAX_EVENT_WINDOW_MIN = 15


def _norm_choice(value, allowed, default):
    """Return the normalized choice, the default when omitted, or None for
    an invalid value. None must be rejected by the caller: silently mapping a
    typo like 'hgih' to 'low' would bypass the high-risk gate."""
    if value is None or not str(value).strip():
        return default
    text = str(value).strip().lower()
    return text if text in allowed else None


def _is_high_risk(risk_level, service_mode):
    return (risk_level in HIGH_RISK_LEVELS
            or service_mode in HIGH_RISK_SERVICE_MODES)


def _manifest_paths(items):
    """Normalize a declared file manifest to a list of repo paths."""
    if items is None:
        return None, None
    if not isinstance(items, (list, tuple)):
        return None, 'fileManifest must be a list of paths or {path} objects'
    out = []
    for item in items:
        if isinstance(item, str):
            path = item.strip()
        elif isinstance(item, dict):
            path = str(item.get('path') or '').strip()
        else:
            path = ''
        if not path:
            return None, 'fileManifest entries need a non-empty path'
        if path not in out:
            out.append(path)
        if len(out) > MAX_MANIFEST_ITEMS:
            return None, f'fileManifest is limited to {MAX_MANIFEST_ITEMS} paths'
    return out, None


def _ack_agents(items):
    """Normalize acknowledgements to a list of {agent, at} records."""
    if items is None:
        return None, None
    if not isinstance(items, (list, tuple)):
        return None, 'acknowledgements must be a list of agent ids'
    out = []
    now = time.time()
    for item in items:
        if isinstance(item, str):
            agent = item.strip()
            at = now
        elif isinstance(item, dict):
            agent = str(item.get('agent') or item.get('id') or '').strip()
            try:
                at = float(item.get('at') or now)
            except (TypeError, ValueError):
                at = now
        else:
            agent = ''
            at = now
        if not agent:
            return None, 'each acknowledgement needs a non-empty agent id'
        if not any(r['agent'] == agent for r in out):
            out.append({'agent': agent, 'at': at})
        if len(out) > MAX_MANIFEST_ITEMS:
            return None, 'too many acknowledgements'
    return out, None


def _parse_clock(value, fmt):
    import datetime as _dt
    try:
        return _dt.datetime.strptime((value or '').strip(), fmt)
    except ValueError:
        return None


def _parse_blackout_windows(items):
    """Normalize blackout windows.

    Each item is either an absolute interval
    {"start": "YYYY-MM-DDTHH:MM", "end": "...", "label": "..."}
    or a daily recurrence
    {"start": "HH:MM", "end": "HH:MM", "days": [0..6], "label": "..."}.
    Monday is 0. Local tank time is used for both forms.
    """
    if items is None:
        return None, None
    if not isinstance(items, (list, tuple)):
        return None, 'blackoutWindows must be a list of intervals'
    out = []
    for item in items:
        if not isinstance(item, dict):
            return None, 'each blackout window must be an object'
        start_raw = str(item.get('start') or '').strip()
        end_raw = str(item.get('end') or '').strip()
        label = str(item.get('label') or '').strip()[:120]
        if not start_raw or not end_raw:
            return None, 'each blackout window needs start and end'
        absolute = False
        start_dt = end_dt = None
        for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M',
                    '%Y-%m-%dT%H:%M:%S'):
            start_dt = _parse_clock(start_raw, fmt)
            end_dt = _parse_clock(end_raw, fmt)
            if start_dt is not None and end_dt is not None:
                absolute = True
                break
        if absolute:
            if end_dt <= start_dt:
                return None, f'blackout {label or start_raw} must end after it starts'
            out.append({'kind': 'absolute', 'label': label,
                        'start': start_dt.strftime('%Y-%m-%dT%H:%M'),
                        'end': end_dt.strftime('%Y-%m-%dT%H:%M')})
        else:
            start_clock = _parse_clock(start_raw, '%H:%M')
            end_clock = _parse_clock(end_raw, '%H:%M')
            if start_clock is None or end_clock is None:
                return None, ('blackout windows need absolute '
                              'YYYY-MM-DDTHH:MM times or HH:MM daily times')
            if end_clock <= start_clock:
                return None, f'blackout {label or start_raw} must end after it starts'
            days = item.get('days')
            if days is None:
                days = list(range(7))
            if not isinstance(days, (list, tuple)):
                return None, 'blackout days must be a list of 0..6'
            try:
                days = sorted({int(d) for d in days})
            except (TypeError, ValueError):
                return None, 'blackout days must be a list of 0..6'
            if any(d < 0 or d > 6 for d in days):
                return None, 'blackout days must be 0..6 (Monday is 0)'
            out.append({'kind': 'daily', 'label': label,
                        'start': start_clock.strftime('%H:%M'),
                        'end': end_clock.strftime('%H:%M'),
                        'days': days})
        if len(out) > MAX_BLACKOUT_ITEMS:
            return None, f'blackoutWindows is limited to {MAX_BLACKOUT_ITEMS}'
    return out, None


def _blackout_conflicts(start_ts, end_ts, windows):
    """Return the blackout labels covering any minute of the slot."""
    import datetime as _dt
    if not windows:
        return []
    hits = []
    step = 60
    cursor = start_ts
    while cursor < end_ts:
        moment = _dt.datetime.fromtimestamp(cursor)
        minute = moment.hour * 60 + moment.minute
        for window in windows:
            if window.get('kind') == 'absolute':
                w_start = _parse_clock(window.get('start'), '%Y-%m-%dT%H:%M')
                w_end = _parse_clock(window.get('end'), '%Y-%m-%dT%H:%M')
                if not w_start or not w_end:
                    continue
                if w_start.timestamp() <= cursor < w_end.timestamp():
                    hits.append(window)
            else:
                if moment.weekday() not in (window.get('days') or []):
                    continue
                w_start = _parse_clock(window.get('start'), '%H:%M')
                w_end = _parse_clock(window.get('end'), '%H:%M')
                if not w_start or not w_end:
                    continue
                start_min = w_start.hour * 60 + w_start.minute
                end_min = w_end.hour * 60 + w_end.minute
                if start_min <= minute < end_min:
                    hits.append(window)
        if hits:
            break
        cursor += step
    out = []
    for window in hits:
        label = window.get('label') or 'blackout window'
        if label not in out:
            out.append(label)
    return out


def _reserved_edit_rows(agent_id):
    with _s()._db() as conn:
        _ensure_table(conn)
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT id, path, apply_policy FROM self_edit_log "
            "WHERE status = 'armed' AND agent = ? ORDER BY created",
            (agent_id,)).fetchall()]


# The post-apply gate: the change window tests the LIVE tree after edits
# land, using the repo's own test gate (the same bar every contributors'
# change must meet). A long window is not needed for tests to finish, but a
# window shorter than the test script would roll back half-seen work -- keep
# the default 15 minutes and the gate capped so a stuck runner cannot eat a
# whole window.
POST_APPLY_TEST_CMD = ['bash', 'world/tests/run_all.sh']
POST_APPLY_TEST_TIMEOUT_S = 900
POST_APPLY_TAIL_LEN = 2000

# Push contract (operator decision, 2026-10-18). A windowed change request
# ends in a push to origin main ONLY after the repo gate passed on the live
# tree AND a second agent (the validator, never one of the proposers)
# approved the result. Outcomes are categorized; successful_with_issues
# cannot land without a note. Git failures and protocol failures roll the
# window back exactly like a failed gate, so local main never sits ahead of
# origin with unpushed validated bytes.
PUSH_CATEGORIES = ('successful', 'successful_with_issues', 'unsuccessful')
MAX_PUSH_NOTE_LEN = 800
GIT_STEP_TIMEOUT_S = 120

_WINDOW_JEV_KIND = 'dormancy window request'

_WINDOW_INSTRUCTIONS = (
    'You are the supervisor of an AI think tank agent that wants to schedule '
    'a short window during which the WHOLE think tank goes dormant (the '
    'simulation pauses) so its already-reviewed code self-edits can be '
    'applied and reloaded. Sleeping the tank interrupts every working agent '
    'for the length of the window. Approve only when the stated reason is a '
    'real maintenance need, the slot is a reasonable off-hours or quiet time '
    'rather than the middle of active work, and the duration is modest. '
    'Reject when the reason is vague, the slot would collide with normal '
    'working hours without justification, or the window looks like an excuse '
    'to pause oversight. The player still has to approve the window '
    'afterwards; this judgment is the supervisory pre-clearance.'
)
_WINDOW_CRITERIA = [
    {'id': 'approve',
     'description': ('Approve when the maintenance reason is real, the slot is '
                     'a reasonable quiet time, and the duration is modest.')},
    {'id': 'reject',
     'description': ('Reject vague reasons, suspicious prime-hours slots, or '
                     'oversized durations. The player still approves or denies '
                     'the final link separately.')},
]


def _ensure_windows_table(conn):
    conn.execute(
        'CREATE TABLE IF NOT EXISTS dormancy_windows ('
        'id TEXT PRIMARY KEY, created REAL NOT NULL, agent TEXT NOT NULL, '
        'date TEXT NOT NULL, start TEXT NOT NULL, start_ts REAL NOT NULL, '
        'end_ts REAL NOT NULL, duration_min INTEGER NOT NULL, '
        'reason TEXT NOT NULL, status TEXT NOT NULL, note TEXT, '
        'escalation_id TEXT, fired_at REAL)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_dormancy_windows_status '
                 'ON dormancy_windows (status, start_ts)')
    # Push contract (operator decision, 2026-10-18): a change request is
    # completed by TWO agents -- the implementer requests the window, a
    # DIFFERENT agent validates the applied result, and only then may the
    # validated bytes be pushed to origin main. Every push outcome carries a
    # category (successful / successful_with_issues / unsuccessful); a
    # successful_with_issues push REQUIRES a non-empty note.
    for col, decl in (
            ('validator', 'TEXT'),
            ('validator_status', 'TEXT'),
            ('validator_note', 'TEXT'),
            ('validated_at', 'REAL'),
            ('remediation_used', 'INTEGER'),
            ('gate_tail', 'TEXT'),
            ('validator_attempt_at', 'REAL'),
            ('validator_attempts', 'INTEGER'),
            ('push_status', 'TEXT'),
            ('push_category', 'TEXT'),
            ('push_note', 'TEXT'),
            ('push_sha', 'TEXT'),
            ('pushed_at', 'REAL'),
            ('admin_status', 'TEXT'),
            ('admin_note', 'TEXT'),
            ('admin_approved_at', 'REAL'),
            ('admin_agent', 'TEXT'),
            ('supervisor', 'TEXT'),
            # Change-request detail (2026-10-22): enough context to review
            # the change, not just the slot. High-risk modes require these.
            ('title', 'TEXT'),
            ('description', 'TEXT'),
            ('change_type', 'TEXT'),
            ('risk_level', 'TEXT'),
            ('service_mode', 'TEXT'),
            ('affected_services', 'TEXT'),
            ('file_manifest', 'TEXT'),
            ('rollback_plan', 'TEXT'),
            ('rollback_owner', 'TEXT'),
            ('success_criteria', 'TEXT'),
            ('abort_criteria', 'TEXT'),
            ('post_apply_checks', 'TEXT'),
            ('safe_state_plan', 'TEXT'),
            ('restore_plan', 'TEXT'),
            ('on_call', 'TEXT'),
            ('acknowledgements', 'TEXT'),
            ('blackout_windows', 'TEXT'),
            ('pre_apply_result', 'TEXT'),
            ('pre_apply_tail', 'TEXT'),
            ('fire_recheck', 'TEXT'),
            ('deferred_reason', 'TEXT'),
            ('vetoed_by', 'TEXT'),
            ('vetoed_at', 'REAL'),
            ('veto_reason', 'TEXT'),
            ('veto_token', 'TEXT')):
        try:
            conn.execute(f'ALTER TABLE dormancy_windows ADD COLUMN {col} {decl}')
        except sqlite3.OperationalError:
            pass  # column already exists


def _notify_agent(agent_id, text, about=''):
    """File one action-needed mail item for an agent (the sim mailbox is
    how server-originated directions reach them). Reads and saves the full
    state around the mailbox mutation, mirroring sim's own callers."""
    serve = _s()
    try:
        import sim as _sim
        state = serve.get_state_from_db() or {}
        payload = {'text': text}
        if about:
            payload['about'] = about
        dispatched = _sim._deliver_mail(state, agent_id, 'selfedit', payload)
        if dispatched:
            serve.save_state_to_db(state)
        return dispatched
    except Exception as exc:
        serve.log_action('selfedit', 'selfedit_mail_failed',
                         {'agent': agent_id, 'error': str(exc)[:200]},
                         authorized=False)
        return False


def _window_record(window_id, created, agent, date, start, start_ts, end_ts,
                   duration_min, reason, status):
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        conn.execute(
            'INSERT INTO dormancy_windows (id, created, agent, date, start, '
            'start_ts, end_ts, duration_min, reason, status) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (window_id, created, agent, date, start, start_ts, end_ts,
             duration_min, reason, status))


def _update_window(window_id, **fields):
    if not fields:
        return
    cols = ', '.join(f'{k} = ?' for k in fields)
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        conn.execute(f'UPDATE dormancy_windows SET {cols} WHERE id = ?',
                     (*fields.values(), window_id))


def _occupied_window_conflicts(start_ts, end_ts, exclude_window_id=None):
    """Pending/approved/fired dormancy windows that overlap the slot."""
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        rows = conn.execute(
            "SELECT id, date, start, duration_min, agent FROM "
            "dormancy_windows WHERE status IN ('pending', 'approved', "
            "'fired') AND start_ts < ? AND end_ts > ? ORDER BY start_ts",
            (end_ts, start_ts)).fetchall()
    return [{'kind': 'dormancy window', 'name': r[0],
             'date': r[1], 'start': r[2], 'durationMin': r[3],
             'agent': r[4]} for r in rows
            if not exclude_window_id or r[0] != exclude_window_id]


def _standing_task_conflicts(start_ts, end_ts):
    """Standing-task schedule windows covering any minute of the slot.

    Mirrors the declaration -> schedule.StandingTask conversion the sim
    sweep uses, and reads the scheduler's live runtime state so hours-mode
    anchors are honored instead of treated as always-on. One sample per
    minute: a 4-hour window is 240 samples of a pure predicate."""
    import datetime as _dt
    import village.schedule as _schedule
    import sim as _sim
    state = _s().get_state_from_db() or {}
    runtime = {}
    scheduler = getattr(_sim, '_STANDING_SCHEDULER', None)
    if scheduler is not None:
        runtime = dict(getattr(scheduler, 'states', {}) or {})
    conflicts = []
    now_iter = start_ts
    seen_tasks = set()
    while now_iter < end_ts:
        now_dt = _dt.datetime.fromtimestamp(now_iter)
        for declaration in (state.get('standingTasks') or []):
            if not isinstance(declaration, dict):
                continue
            task_id = declaration.get('taskId')
            if task_id in seen_tasks:
                continue
            try:
                task = _schedule.StandingTask(
                    task_id=task_id,
                    name=declaration.get('name') or declaration.get('taskId') or task_id,
                    mode=declaration.get('mode') or _schedule.ALWAYS,
                    agent_count=declaration.get('agentCount'),
                    duration_hours=declaration.get('durationHours'),
                    start_time=declaration.get('startTime'),
                    end_time=declaration.get('endTime'),
                    days_of_week=set(declaration.get('daysOfWeek') or ()),
                    day_of_month=declaration.get('dayOfMonth'),
                    yearly_date=(tuple(declaration['yearlyDate'])
                                 if declaration.get('yearlyDate') else None))
            except (TypeError, ValueError):
                continue
            state_rt = runtime.get(task_id) or _schedule.TaskState()
            if _schedule.StandingScheduler().is_active(task, state_rt, now_dt):
                seen_tasks.add(task_id)
                conflicts.append({
                    'kind': 'standing task', 'name': task.name,
                    'taskId': task_id,
                    'mode': task.mode,
                    'startTime': task.start_time or '',
                    'endTime': task.end_time or ''})
                break
        now_iter += 60
    return conflicts


def _window_conflicts(start_ts, end_ts, exclude_window_id=None):
    return (_standing_task_conflicts(start_ts, end_ts)
            + _occupied_window_conflicts(start_ts, end_ts,
                                         exclude_window_id=exclude_window_id))


def _parse_window_target(date_text, start_text, now):
    """Parse 'YYYY-MM-DD' + 'HH:MM' into a local timestamp. Returns
    (start_ts, end_of_day_ts, None) or (None, None, error)."""
    import calendar
    import datetime as _dt
    try:
        parsed_date = time.strptime((date_text or '').strip(), '%Y-%m-%d')
    except ValueError:
        return None, None, 'date must be YYYY-MM-DD'
    try:
        parsed_time = time.strptime((start_text or '').strip(), '%H:%M')
    except ValueError:
        return None, None, 'start must be HH:MM (24h)'
    y, m, d = parsed_date.tm_year, parsed_date.tm_mon, parsed_date.tm_mday
    hh, mm = parsed_time.tm_hour, parsed_time.tm_min
    try:
        start_ts = time.mktime((y, m, d, hh, mm, 0, 0, 0, -1))
    except (OverflowError, ValueError) as e:
        return None, None, f'invalid window start: {e}'
    last_day = calendar.monthrange(y, m)[1]
    day_end = time.mktime((y, m, min(d, last_day), 23, 59, 0, 0, 0, -1))
    _dt.datetime.fromtimestamp(start_ts)  # raises on impossible times (DST gap)
    return start_ts, day_end, None


def request_window(agent_id, date, start, duration_min, reason, validator=None,
                   title=None, description=None, change_type='normal',
                   risk_level='low', service_mode='idle_ok',
                   affected_services=None, file_manifest=None,
                   rollback_plan=None, rollback_owner=None,
                   success_criteria=None, abort_criteria=None,
                   post_apply_checks=None, safe_state_plan=None,
                   restore_plan=None, on_call=None,
                   acknowledgements=None, blackout_windows=None,
                   notify_created=False):
    """Run the change-request chain for one dormancy slot.

    Returns (ok, payload); ok=False carries the refusal (with 'conflicts'
    when the slot was blocked by the schedule). ok=True means the supervisor
    judgment passed and the window is approved or waiting on the player link.

    Two-agent contract (operator decision, 2026-10-18): the requester is the
    IMPLEMENTER and must name a DIFFERENT agent as validator; the validator
    reviews the applied result before anything is pushed to main. The
    implementer's currently armed edits are RESERVED for this window -- they
    apply inside the slot and nowhere else, so the validator reviews exactly
    the change request's bytes.

    High-risk changes (risk high/critical, or an always-on service mode)
    must declare the files, the rollback plan, success and abort criteria,
    post-apply checks, an on-call owner, acknowledgements, and window-only
    edits. Trading and event modes also declare a safe-state plan, a restore
    plan, and blackout windows. Those requests run the full repo gate on a
    quarantine copy before they are scheduled.
    """
    serve = _s()
    agent_id = (agent_id or '').strip() or 'unknown'
    reason = (reason or '').strip()[:MAX_WINDOW_REASON_LEN]
    if not reason:
        return False, {'error': 'a reason is required -- say WHY the tank '
                                'should sleep then (emergency fix, risky '
                                'reload, quiet hours maintenance)'}
    validator = (validator or '').strip()
    change_type = _norm_choice(change_type, WINDOW_CHANGE_TYPES, 'normal')
    if change_type is None:
        return False, {'error': 'changeType must be one of: '
                                + ', '.join(WINDOW_CHANGE_TYPES)}
    risk_level = _norm_choice(risk_level, WINDOW_RISK_LEVELS, 'low')
    if risk_level is None:
        return False, {'error': 'riskLevel must be one of: '
                                + ', '.join(WINDOW_RISK_LEVELS)}
    service_mode = _norm_choice(service_mode, WINDOW_SERVICE_MODES, 'idle_ok')
    if service_mode is None:
        return False, {'error': 'serviceMode must be one of: '
                                + ', '.join(WINDOW_SERVICE_MODES)}
    title = (title or '').strip()[:MAX_WINDOW_TITLE_LEN]
    description = (description or '').strip()[:MAX_WINDOW_TEXT_LEN]
    rollback_plan = (rollback_plan or '').strip()[:MAX_WINDOW_TEXT_LEN]
    rollback_owner = (rollback_owner or '').strip()[:200]
    success_criteria = (success_criteria or '').strip()[:MAX_WINDOW_TEXT_LEN]
    abort_criteria = (abort_criteria or '').strip()[:MAX_WINDOW_TEXT_LEN]
    post_apply_checks = (post_apply_checks or '').strip()[:MAX_WINDOW_TEXT_LEN]
    safe_state_plan = (safe_state_plan or '').strip()[:MAX_WINDOW_TEXT_LEN]
    restore_plan = (restore_plan or '').strip()[:MAX_WINDOW_TEXT_LEN]
    on_call = (on_call or '').strip()[:200]
    try:
        duration_min = int(duration_min or WINDOW_DEFAULT_DURATION_MIN)
    except (TypeError, ValueError):
        duration_min = WINDOW_DEFAULT_DURATION_MIN
    duration_min = max(WINDOW_MIN_DURATION_MIN,
                       min(WINDOW_MAX_DURATION_MIN, duration_min))
    if service_mode == 'always_on_trading':
        duration_min = min(duration_min, MAX_TRADING_WINDOW_MIN)
    elif service_mode == 'event_driven':
        duration_min = min(duration_min, MAX_EVENT_WINDOW_MIN)
    now = time.time()
    start_ts, _day_end, err = _parse_window_target(date, start, now)
    if err:
        return False, {'error': err}
    emergency = change_type == 'emergency'
    min_lead = WINDOW_EMERGENCY_MIN_LEAD_S if emergency else WINDOW_MIN_LEAD_S
    if start_ts < now + min_lead:
        if emergency:
            return False, {'error': 'emergency change requests must start at '
                                    'least 1 hour from now -- the owner '
                                    'still needs a chance to veto'}
        return False, {'error': 'the window must start at least 24 hours '
                                'from now -- change requests need a day of '
                                'notice so the owner can review and veto'}
    if start_ts > now + WINDOW_MAX_LEAD_S:
        return False, {'error': 'the window may not be more than 7 days out '
                                '-- file the request closer to the window'}
    end_ts = start_ts + duration_min * 60
    conflicts = _window_conflicts(start_ts, end_ts)
    if conflicts:
        listed = '; '.join(
            f"{c['kind']} {c['name']}"
            + (f" ({c.get('startTime') or c['date']} "
               f"{c.get('start', '')})" if (c.get('startTime') or c['date'])
               else '')
            for c in conflicts)
        serve.log_action(agent_id, 'dormancy_window_conflict',
                         {'date': date, 'start': start, 'conflicts': listed[:300]},
                         authorized=True)
        return False, {'error': 'the requested slot collides with already '
                                'scheduled items -- pick another time: '
                                f'{listed}', 'conflicts': conflicts}
    # two-agent change requests (operator decision, 2026-10-18): the
    # implementer names a DIFFERENT validator, and the request only proceeds
    # if the implementer actually has armed edits to reserve for the slot
    if not validator:
        return False, {'error': 'a validator is required: name a DIFFERENT '
                                'agent who will review the applied result '
                                'before it is pushed to GitHub'}
    if validator == agent_id:
        return False, {'error': 'the validator must be a different agent than '
                                'the implementer -- change requests complete '
                                'with two agents (implementer + validator)'}
    state = serve.get_state_from_db() or {}
    roster = state.get('agents') or {}
    names = (set(roster.keys()) if isinstance(roster, dict)
             else {a.get('id') for a in roster if isinstance(a, dict)})
    if names and validator not in names:
        return False, {'error': f'{validator} is not a known agent -- name a '
                                'real validator from the tank roster'}
    admin_mode = governance.admin_change_request_enabled(state)
    if admin_mode and names and not serve._is_director(state, validator):
        return False, {'error': 'the validator must be a director -- change '
                                'requests need a director approval, not a '
                                'peer worker'}
    if admin_mode and validator == serve._admin_agent_id(state):
        return False, {'error': 'name a director other than the admin; the '
                                'admin approves the push as the second, '
                                'independent approver'}
    reserved_rows = _reserved_edit_rows(agent_id)
    if not reserved_rows:
        return False, {'error': 'you have no armed edits to schedule a window '
                                'for -- propose and arm your changes first '
                                '(self_edit), then schedule the window'}
    armed_ids = [r['id'] for r in reserved_rows]
    reserved_paths = [r['path'] for r in reserved_rows]
    high_risk = _is_high_risk(risk_level, service_mode)
    if high_risk:
        unsafe = [r['path'] for r in reserved_rows
                  if (r.get('apply_policy') or 'idle_or_window') != 'window_only']
        if unsafe:
            return False, {'error': (
                'high-risk change requests require window-only edits; '
                're-propose the following paths with windowOnly=true so '
                'they cannot apply on the idle path: '
                + ', '.join(sorted(set(unsafe))))}
    manifest, err = _manifest_paths(file_manifest)
    if err:
        return False, {'error': err}
    if high_risk and manifest is None:
        return False, {'error': 'fileManifest is required for a high-risk or '
                                'always-on change request: list every path '
                                'the window will modify'}
    if manifest is None:
        manifest = list(dict.fromkeys(reserved_paths))
    if sorted(manifest) != sorted(set(reserved_paths)):
        return False, {'error': 'fileManifest does not match the reserved '
                                'edits -- declared: '
                                f'{sorted(manifest)}; reserved: '
                                f'{sorted(set(reserved_paths))}'}
    acks, err = _ack_agents(acknowledgements)
    if err:
        return False, {'error': err}
    if high_risk:
        missing = [name for name, value in (
            ('title', title), ('description', description),
            ('rollbackPlan', rollback_plan), ('rollbackOwner', rollback_owner),
            ('successCriteria', success_criteria),
            ('abortCriteria', abort_criteria),
            ('postApplyChecks', post_apply_checks), ('onCall', on_call),
        ) if not value]
        if missing:
            return False, {'error': 'high-risk change requests require: '
                                    + ', '.join(missing)}
        if not acks:
            return False, {'error': 'high-risk change requests require at '
                                    'least one acknowledgement from the '
                                    'affected team or risk owner'}
        if service_mode in HIGH_RISK_SERVICE_MODES:
            trading_missing = [name for name, value in (
                ('safeStatePlan', safe_state_plan),
                ('restorePlan', restore_plan),
            ) if not value]
            if trading_missing:
                return False, {'error': 'trading and event modes require: '
                                        + ', '.join(trading_missing)}
    if names:
        unknown = [a['agent'] for a in (acks or []) if a['agent'] not in names]
        if unknown:
            return False, {'error': 'unknown acknowledgement agent(s): '
                                    + ', '.join(unknown)}
    blackouts, err = _parse_blackout_windows(blackout_windows)
    if err:
        return False, {'error': err}
    if service_mode in HIGH_RISK_SERVICE_MODES and not blackouts:
        return False, {'error': 'trading and event modes require '
                                'blackoutWindows: declare the market, event, '
                                'settlement, or funding windows the change '
                                'must not overlap'}
    blackout_hits = _blackout_conflicts(start_ts, end_ts, blackouts)
    if blackout_hits:
        return False, {'error': 'the requested slot overlaps a declared '
                                'blackout window: ' + '; '.join(blackout_hits)}
    pre_apply_result = None
    pre_apply_tail = ''
    # Run the repo gate BEFORE the window for every change request, not just
    # high-risk ones. A low-risk HTML edit still shares the repo with every
    # other test, and a red baseline used to be discovered only after the tank
    # went dormant. The post-apply gate remains the final authority. A hermetic
    # unit-test tree has no serve.py/kernel to initialize, so skip there.
    if os.path.exists(os.path.join(_world(), 'serve.py')):
        ok_tests, pre_apply_tail = _run_pre_apply_tests(reserved_rows)
    else:
        ok_tests, pre_apply_tail = True, 'pre-apply gate skipped: hermetic test tree'
    if not ok_tests:
        serve.log_action(agent_id, 'dormancy_window_pre_apply_failed',
                         {'tail': pre_apply_tail[:400]}, authorized=True)
        return False, {'error': 'the pre-apply repo gate failed against '
                                'the candidate tree -- nothing was '
                                'scheduled', 'detail': pre_apply_tail[-2000:]}
    pre_apply_result = 'pass'
    affected = (affected_services if isinstance(affected_services, list)
                else ([affected_services] if affected_services else []))
    affected = [str(a).strip()[:120] for a in affected if str(a).strip()]
    question = (f'Agent {agent_id} requests a dormancy window to apply '
                f'reviewed code self-edits:\n'
                f'Date: {date}  Start: {start} (tank local time)  '
                f'Duration: {duration_min} min\n'
                f'Reason: {reason}\n'
                f'Change type: {change_type}  Risk: {risk_level}  '
                f'Service mode: {service_mode}\n'
                f'Files: {", ".join(manifest)}\n'
                f'Rollback: {rollback_plan[:300] or "(not declared)"}\n\n'
                f'Approving sets the tank dormant for that slot so armed '
                f'self-edits apply. The supervisor pre-cleared this request '
                f'through the JEV quorum.')
    decision, confidence, cost = serve._jev_quorum_choice_sync(
        _WINDOW_INSTRUCTIONS + '\n\n' + question,
        [dict(c) for c in _WINDOW_CRITERIA])
    if decision != 'approve':
        serve.log_action(agent_id, 'dormancy_window_refused',
                         {'decision': decision, 'confidence': confidence},
                         authorized=True)
        return False, {'error': f'the supervisor review said {decision!r} '
                                f'at {round(float(confidence or 0.0), 2)} '
                                'confidence -- nothing was scheduled',
                       'status': 'refused'}
    window_id = f'win-{secrets.token_hex(5)}'
    veto_token = secrets.token_urlsafe(24)
    if admin_mode:
        esc_id = None  # no player link in the two-agent chain
        _window_record(window_id, now, agent_id, date, start, start_ts, end_ts,
                       duration_min, reason, 'approved')
    else:
        # Persist the window row BEFORE creating the escalation that points
        # at it. The old order (escalation first, row second) left an
        # escalation whose dormancy_windows row was missing whenever the
        # insert failed, and every later expiry logged the opaque
        # "dormancy window record missing" (task esc-e4dea969).
        _window_record(window_id, now, agent_id, date, start, start_ts, end_ts,
                       duration_min, reason, 'pending')
        esc_id = serve.create_escalation(_WINDOW_JEV_KIND, question,
                                         on_approve_note=window_id)
    _update_window(
        window_id, escalation_id=esc_id, validator=validator,
        validator_status='pending', supervisor=serve._supervisor_of(state, agent_id),
        title=title or None, description=description or None,
        change_type=change_type, risk_level=risk_level,
        service_mode=service_mode,
        affected_services=json.dumps(affected) if affected else None,
        file_manifest=json.dumps(manifest),
        rollback_plan=rollback_plan or None,
        rollback_owner=rollback_owner or None,
        success_criteria=success_criteria or None,
        abort_criteria=abort_criteria or None,
        post_apply_checks=post_apply_checks or None,
        safe_state_plan=safe_state_plan or None,
        restore_plan=restore_plan or None,
        on_call=on_call or None,
        acknowledgements=json.dumps(acks or []),
        blackout_windows=json.dumps(blackouts or []),
        pre_apply_result=pre_apply_result,
        pre_apply_tail=pre_apply_tail[-POST_APPLY_TAIL_LEN:] or None,
        veto_token=veto_token,
        **({'admin_status': 'pending'} if admin_mode else {}))
    with serve._db() as conn:
        _ensure_table(conn)
        for row_id in armed_ids:
            conn.execute('UPDATE self_edit_log SET reserve_window = ? '
                         'WHERE id = ?', (window_id, row_id))
    for ack in (acks or []):
        _notify_agent(ack['agent'], (
            f'Change request {window_id} is scheduled by {agent_id} for '
            f'{date} {start} local, {duration_min} min. You are listed as an '
            f'acknowledged party. Risk {risk_level}; service mode '
            f'{service_mode}. Files: {", ".join(manifest)}.'),
            about=f'selfedit:{window_id}')
    serve.log_action(window_id, 'dormancy_window_requested',
                     {'agent': agent_id, 'date': date, 'start': start,
                      'durationMin': duration_min, 'escalationId': esc_id,
                      'supervisorDecision': 'approved', 'validator': validator,
                      'reservedEdits': armed_ids, 'riskLevel': risk_level,
                      'serviceMode': service_mode, 'changeType': change_type,
                      'files': manifest, 'highRisk': high_risk,
                      'preApply': pre_apply_result},
                     authorized=True)
    if emergency:
        serve.log_action(agent_id, 'emergency_change_requested',
                         {'windowId': window_id, 'reason': reason[:200],
                          'start': f'{date} {start}'}, authorized=True)
    if notify_created or emergency:
        try:
            _telegram_change_request_created(window_id)
        except Exception:
            pass  # notification is best-effort; the window itself is recorded
    return True, {'id': window_id,
                  'status': 'approved' if admin_mode else 'pending',
                  'escalationId': esc_id,
                  'durationMin': duration_min, 'validator': validator,
                  'reservedEdits': armed_ids,
                  'fileManifest': manifest,
                  'riskLevel': risk_level,
                  'serviceMode': service_mode,
                  'changeType': change_type,
                  'acknowledgements': acks or [],
                  'preApplyResult': pre_apply_result,
                  'message': (f'window approved by the supervisor; '
                              f'{len(armed_ids)} armed edit(s) are reserved. '
                              f'{validator} must approve the applied result '
                              f'and the admin must approve the push.'
                              if admin_mode else
                              f'window requested -- the supervisor review '
                              f'approved it; {len(armed_ids)} armed edit(s) '
                              f'are reserved and {validator} will validate '
                              f'the applied result. It now waits on the '
                              f'player approval link.')}


def resolve_window_escalation(esc_id, approved):
    """Side effect for the escalation resolve endpoint: the player's link
    decision lands on the window row. Only a PENDING window changes."""
    serve = _s()
    with serve._db() as conn:
        _ensure_windows_table(conn)
        row = conn.execute(
            'SELECT id, status FROM dormancy_windows '
            'WHERE escalation_id = ?', (esc_id,)).fetchone()
    if not row:
        return False
    window_id, status = row[0], row[1]
    if status != 'pending':
        return False
    if approved:
        _update_window(window_id, status='approved', note='player approved')
    else:
        # denied change requests release their reserved edits back to the
        # idle pool -- they were never applied and stay armed
        _unreserve_window_edits(window_id)
        _update_window(window_id, status='refused',
                       note='player denied')
    serve.log_action(window_id, 'dormancy_window_resolved',
                     {'by': 'player', 'approved': approved},
                     authorized=True)
    return True
