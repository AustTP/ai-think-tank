

def _window_fire_blockers(window):
    """Re-check a due window against live state before it fires.

    The request-time scan is not enough: standing tasks, other windows,
    market/event blackouts, the roster, and the reserved edit set can all
    change after approval. Any change defers the window instead of applying
    bytes into a state that was never reviewed.
    """
    if not window:
        return ['unknown window']
    blockers = []
    try:
        conflicts = _window_conflicts(float(window['start_ts']),
                                      float(window['end_ts']),
                                      exclude_window_id=window['id'])
    except (KeyError, TypeError, ValueError) as exc:
        return [f'conflict scan failed: {exc}']
    for c in conflicts:
        blockers.append(f"{c.get('kind')} {c.get('name')}")
    try:
        blackouts = json.loads(window.get('blackout_windows') or '[]')
    except (TypeError, ValueError):
        blackouts = []
    for label in _blackout_conflicts(float(window['start_ts']),
                                     float(window['end_ts']), blackouts):
        blockers.append(f'blackout {label}')
    # Bare windows in the ledger predate the detailed change-request
    # contract. They still get the schedule and blackout recheck; the
    # validator, manifest, and window-only checks apply once a window carries
    # request metadata (every real request does).
    has_request_meta = bool(window.get('validator')
                            or window.get('file_manifest')
                            or window.get('risk_level')
                            or window.get('service_mode'))
    if not has_request_meta:
        return blockers
    state = _s().get_state_from_db() or {}
    roster = state.get('agents') or {}
    names = (set(roster.keys()) if isinstance(roster, dict)
             else {a.get('id') for a in roster if isinstance(a, dict)})
    validator = (window.get('validator') or '').strip()
    if not validator:
        blockers.append('no validator on record')
    elif names and validator not in names:
        blockers.append(f'validator {validator} is no longer on the roster')
    elif governance.admin_change_request_enabled(state):
        if not _s()._is_director(state, validator):
            blockers.append(f'validator {validator} is no longer a director')
        if validator == _s()._admin_agent_id(state):
            blockers.append('validator is the admin, not an independent director')
    with _s()._db() as conn:
        _ensure_table(conn)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT id, path, apply_policy FROM self_edit_log "
            "WHERE reserve_window = ? AND status = 'armed' "
            'ORDER BY created', (window['id'],)).fetchall()]
    if not rows:
        blockers.append('no reserved edits are still armed')
    try:
        manifest = json.loads(window.get('file_manifest') or '[]')
    except (TypeError, ValueError):
        manifest = []
    if rows and manifest and sorted(set(manifest)) != sorted({r['path'] for r in rows}):
        blockers.append('file manifest no longer matches the reserved edits')
    service_mode = window.get('service_mode') or 'idle_ok'
    risk_level = window.get('risk_level') or 'low'
    if _is_high_risk(risk_level, service_mode):
        unsafe = [r['path'] for r in rows
                  if (r.get('apply_policy') or 'idle_or_window') != 'window_only']
        if unsafe:
            blockers.append('window-only edits required for: '
                            + ', '.join(sorted(set(unsafe))))
        if window.get('pre_apply_result') not in (None, 'pass'):
            blockers.append('pre-apply gate did not pass')
    return blockers


def _defer_window(window_id, blockers):
    """Release a window that cannot safely fire, notify the owner, and keep
    the reservation from stranding the edits."""
    serve = _s()
    note = '; '.join(blockers)[:800]
    _update_window(window_id, status='deferred_conflict',
                   note='deferred at fire time: ' + note,
                   deferred_reason=note)
    _unreserve_window_edits(window_id)
    row = _window_row(window_id) or {}
    if row.get('agent'):
        _notify_agent(row['agent'], (
            f'Change request {window_id} was DEFERRED at fire time and did '
            f'not apply. Reason: {note}. Your edits remain armed and '
            f'unreserved. Re-check the schedule and file a fresh request.'),
            about=f'selfedit:{window_id}')
    serve.log_action(window_id, 'dormancy_window_deferred',
                     {'reason': note}, authorized=True)
    _notify_player_telegram('[AI Think Tank] Change request deferred',
                            f'Change request {window_id} did not fire. {note}')
    return window_id


def window_tick():
    """Serve-loop tick for dormancy windows: sweep missed slots, fire due
    ones by forcing the tank dormant, and keep it dormant through the end
    of the slot. Returns the list of window ids newly fired."""
    serve = _s()
    now = time.time()
    with serve._db() as conn:
        _ensure_windows_table(conn)
        missed = conn.execute(
            "SELECT id FROM dormancy_windows WHERE status = 'approved' "
            'AND end_ts < ?', (now,)).fetchall()
    for (row_id,) in missed:
        _update_window(row_id, status='missed',
                       note='window passed without firing')
        _unreserve_window_edits(row_id)
        serve.log_action(row_id, 'dormancy_window_missed', {},
                         authorized=False)
    with serve._db() as conn:
        _ensure_windows_table(conn)
        due = conn.execute(
            "SELECT id FROM dormancy_windows WHERE status = 'approved' "
            'AND start_ts <= ? ORDER BY start_ts', (now,)).fetchall()
        holding = conn.execute(
            "SELECT id, end_ts FROM dormancy_windows WHERE status = 'fired' "
            'ORDER BY start_ts').fetchall()
    # END OF WINDOW: a fired window whose slot has passed either finished
    # healthy (done) or still has failing edits -- per the change-window
    # contract, rolling back automatically. The responsible agents are
    # mailed to fix off-window and schedule again.
    for row_id, end_ts in holding:
        if end_ts > now:
            continue
        failing = _window_participants(row_id, 'failed')
        if failing:
            _rollback_rows(failing,
                           f'change window {row_id} ended with failing tests',
                           row_id)
            _update_window(row_id, status='rolled_back',
                           note='ended with unresolved test failures; '
                                'candidates saved and edits reverted',
                           push_status='failed',
                           push_category='unsuccessful',
                           push_note='repo tests still failing at the end '
                                     'of the slot')
            serve.log_action(row_id, 'dormancy_window_rolled_back',
                             {'edits': failing}, authorized=True)
            _telegram_change_request(row_id, 'unsuccessful',
                                     'repo tests still failing at the end '
                                     'of the slot')
        else:
            push_status = _window_field(row_id, 'push_status')
            if push_status == 'pushed':
                _update_window(row_id, status='done')
                serve.log_action(row_id, 'dormancy_window_done', {},
                                 authorized=True)
            elif push_status == 'awaiting_validator':
                applied_rows = _window_participants(row_id, 'applied')
                _rollback_rows(applied_rows,
                               f'change window {row_id} ended before the '
                               'validator approved the push', row_id)
                _update_window(row_id, status='rolled_back',
                               note='ended awaiting validation; edits '
                                    'reverted, candidates saved',
                               push_status='failed',
                               push_category='unsuccessful',
                               push_note='window ended before the validator '
                                         'approved the push')
                serve.log_action(row_id, 'dormancy_window_rolled_back',
                                 {'edits': applied_rows,
                                  'reason': 'awaiting validator at slot end'},
                                 authorized=True)
                _telegram_change_request(
                    row_id, 'unsuccessful',
                    'window ended before the validator approved the push')
            else:
                _update_window(row_id, status='unsuccessful',
                               push_status='failed',
                               push_category='unsuccessful',
                               push_note='window ended without a validated, '
                                         'pushed result')
                serve.log_action(row_id, 'dormancy_window_unsuccessful', {},
                                 authorized=True)
                _telegram_change_request(row_id, 'unsuccessful',
                                         'window ended without a validated, '
                                         'pushed result')
        _unreserve_window_edits(row_id)
    fired = []
    for (row_id,) in due:
        window = _window_row(row_id)
        blockers = _window_fire_blockers(window)
        if blockers:
            _defer_window(row_id, blockers)
            continue
        _update_window(row_id, status='fired', fired_at=now)
        serve.log_action(row_id, 'dormancy_window_fired',
                         {'dormant': True}, authorized=True)
        fired.append(row_id)
    if fired or any(end_ts > now for _w, end_ts in holding):
        if not serve._dormant():
            serve._set_dormant(True)
            serve.log_action('dormancy_window_loop', 'dormancy_forced',
                             {'windows': fired + [w for w, _e in holding]},
                             authorized=True)
            print('[selfedit] dormancy window active -- tank set dormant', flush=True)
    return fired


def cancel_window(window_id, by='player'):
    """Operator-side undo before the slot fires."""
    serve = _s()
    with serve._db() as conn:
        _ensure_windows_table(conn)
        row = conn.execute(
            'SELECT status FROM dormancy_windows WHERE id = ?',
            (window_id,)).fetchone()
    if not row:
        return False, {'error': 'unknown window id'}
    status = row[0]
    if status not in ('pending', 'approved'):
        return False, {'error': f'window is {status!r}, nothing to cancel'}
    _update_window(window_id, status='cancelled',
                   note=f'cancelled by {by}')
    _unreserve_window_edits(window_id)
    serve.log_action(window_id, 'dormancy_window_cancelled',
                     {'by': by}, authorized=True)
    return True, {'ok': True, 'status': 'cancelled'}


def veto_window(window_id, by='player', reason=''):
    """Final operator veto for a change request.

    A veto before the slot fires cancels the window and releases the
    reservations. A veto after the slot fired but before the push rolls
    back every applied edit and prevents the push. A pushed window cannot
    be vetoed; it is already on main and needs a fresh change request.
    """
    serve = _s()
    reason = (reason or '').strip()[:MAX_PUSH_NOTE_LEN]
    if not reason:
        return False, {'error': 'a reason is required for a veto'}
    row = _window_row(window_id)
    if not row:
        return False, {'error': 'unknown window id'}
    status = row.get('status')
    if status == 'vetoed':
        return False, {'error': 'this change request is already vetoed'}
    if status in ('pending', 'approved'):
        _update_window(window_id, status='vetoed', note=f'vetoed by {by}: {reason}',
                       vetoed_by=by, vetoed_at=time.time(), veto_reason=reason)
        _unreserve_window_edits(window_id)
    elif status == 'deferred_conflict':
        _update_window(window_id, status='vetoed', note=f'vetoed by {by}: {reason}',
                       vetoed_by=by, vetoed_at=time.time(), veto_reason=reason)
    elif status == 'fired':
        if row.get('push_status') == 'pushed':
            return False, {'error': 'the change request was already pushed; '
                                    'veto cannot undo a push'}
        applied = _window_participants(window_id, 'applied')
        failed = _window_participants(window_id, 'failed')
        if applied:
            _rollback_rows(applied, f'change request vetoed by {by}: {reason}',
                           window_id)
        _update_window(window_id, status='vetoed',
                       note=f'vetoed by {by}: {reason}',
                       push_status='failed',
                       push_category='unsuccessful',
                       push_note=f'vetoed by {by}: {reason}',
                       vetoed_by=by, vetoed_at=time.time(),
                       veto_reason=reason)
        _unreserve_window_edits(window_id)
        if failed:
            _update_window(window_id, note=f'vetoed by {by}: {reason}; '
                                           f'{len(failed)} failed edit(s) kept '
                                           'for review')
    else:
        return False, {'error': f'window is {status!r}, nothing to veto'}
    if row.get('agent'):
        _notify_agent(row['agent'], (
            f'Change request {window_id} was VETOED by {by}. Reason: {reason}. '
            f'Nothing was pushed. Your edits remain armed unless they were '
            f'already applied inside the window.'),
            about=f'selfedit:{window_id}')
    validator = row.get('validator')
    if validator:
        _notify_agent(validator, (
            f'Change request {window_id} was VETOED by {by}. Reason: {reason}. '
            f'Do not approve or push it.'),
            about=f'selfedit:{window_id}')
    serve.log_action(window_id, 'dormancy_window_vetoed',
                     {'by': by, 'reason': reason, 'previousStatus': status},
                     authorized=True)
    _notify_player_telegram('[AI Think Tank] Change request vetoed',
                            f'Change request {window_id} was vetoed by {by}. '
                            f'Reason: {reason}')
    return True, {'ok': True, 'status': 'vetoed', 'windowId': window_id,
                  'vetoedBy': by, 'reason': reason}


def windows_snapshot(limit=25):
    """Bounded, newest-first view of the dormancy-window ledger."""
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT id, created, agent, date, start, duration_min, reason, '
            'status, note, escalation_id, fired_at, validator, '
            'validator_status, validator_note, push_status, push_category, '
            'push_note, push_sha, pushed_at, admin_status, admin_note, '
            'admin_agent, supervisor, title, description, change_type, '
            'risk_level, service_mode, affected_services, file_manifest, '
            'rollback_plan, rollback_owner, success_criteria, abort_criteria, '
            'post_apply_checks, safe_state_plan, restore_plan, on_call, '
            'acknowledgements, blackout_windows, pre_apply_result, '
            'pre_apply_tail, fire_recheck, deferred_reason, vetoed_by, '
            'vetoed_at, veto_reason '
            'FROM dormancy_windows ORDER BY created DESC LIMIT ?',
            (int(limit),)).fetchall()
    out = []
    for r in rows:
        item = {'id': r['id'], 'created': r['created'], 'agent': r['agent'],
                'date': r['date'], 'start': r['start'],
                'durationMin': r['duration_min'], 'reason': r['reason'],
                'status': r['status'], 'note': r['note'],
                'escalationId': r['escalation_id'],
                'firedAt': r['fired_at'], 'validator': r['validator'],
                'validatorStatus': r['validator_status'],
                'validatorNote': r['validator_note'],
                'pushStatus': r['push_status'],
                'pushCategory': r['push_category'],
                'pushNote': r['push_note'], 'pushSha': r['push_sha'],
                'pushedAt': r['pushed_at'],
                'adminStatus': r['admin_status'],
                'adminNote': r['admin_note'],
                'adminAgent': r['admin_agent'],
                'supervisor': r['supervisor']}
        json_lists = {'affectedServices', 'fileManifest',
                      'acknowledgements', 'blackoutWindows'}
        for key, col in (
                ('title', 'title'), ('description', 'description'),
                ('changeType', 'change_type'), ('riskLevel', 'risk_level'),
                ('serviceMode', 'service_mode'),
                ('affectedServices', 'affected_services'),
                ('fileManifest', 'file_manifest'),
                ('rollbackPlan', 'rollback_plan'),
                ('rollbackOwner', 'rollback_owner'),
                ('successCriteria', 'success_criteria'),
                ('abortCriteria', 'abort_criteria'),
                ('postApplyChecks', 'post_apply_checks'),
                ('safeStatePlan', 'safe_state_plan'),
                ('restorePlan', 'restore_plan'), ('onCall', 'on_call'),
                ('acknowledgements', 'acknowledgements'),
                ('blackoutWindows', 'blackout_windows'),
                ('preApplyResult', 'pre_apply_result'),
                ('preApplyTail', 'pre_apply_tail'),
                ('fireRecheck', 'fire_recheck'),
                ('deferredReason', 'deferred_reason'),
                ('vetoedBy', 'vetoed_by'), ('vetoedAt', 'vetoed_at'),
                ('vetoReason', 'veto_reason')):
            value = r[col]
            if key in json_lists:
                try:
                    value = json.loads(value or '[]')
                except (TypeError, ValueError):
                    value = []
            item[key] = value
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Two-agent change requests -> push to origin main (operator decision,
# 2026-10-18). The implementer requests the window and names a validator (a
# DIFFERENT agent). The window reserves the implementer's armed edits, applies
# exactly those inside the slot, and runs the repo gate on the live tree. Gate
# green holds the window for the validator's explicit approval; only an
# approved result is committed and pushed. Every final outcome is categorized
# (successful / successful_with_issues / unsuccessful), logged as repo_push,
# and pushed to the player's Telegram. Failures roll back like a failed gate:
# local main never sits ahead of origin with unpushed validated bytes.
# ---------------------------------------------------------------------------


def _git_run(args):
    """One git command against the repo root. Returns (rc, stdout, stderr);
    every failure is a plain return code -- the caller fails closed."""
    try:
        done = subprocess.run(['git', '-C', _repo_root(), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=GIT_STEP_TIMEOUT_S)
        return (done.returncode,
                (done.stdout or b'').decode('utf-8', errors='replace'),
                (done.stderr or b'').decode('utf-8', errors='replace'))
    except subprocess.TimeoutExpired:
        return 124, '', f'git {args[0]} timed out after {GIT_STEP_TIMEOUT_S}s'
    except Exception as exc:
        return 1, '', f'git {args[0]} could not start: {exc}'


def _notify_player_telegram(subject, body):
    """Best-effort player Telegram push for change-request outcomes; never
    raises (an unreachable Telegram must not break a finished push)."""
    try:
        from notify import send_player_telegram_sync
        return send_player_telegram_sync(subject, body)
    except Exception as exc:
        try:
            _s().log_action('selfedit', 'selfedit_telegram_failed',
                            {'error': str(exc)[:200]}, authorized=False)
        except Exception:
            pass
        return False


def _window_row(window_id):
    with _s()._db() as conn:
        conn.row_factory = sqlite3.Row
        _ensure_windows_table(conn)
        row = conn.execute(
            'SELECT * FROM dormancy_windows WHERE id = ?',
            (window_id,)).fetchone()
    return dict(row) if row else None


def _window_field(window_id, col):
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        row = conn.execute(
            f'SELECT {col} FROM dormancy_windows WHERE id = ?',
            (window_id,)).fetchone()
    return row[0] if row else None


def _unreserve_window_edits(window_id):
    """Release every edit still reserved for a window back to the idle pool
    (reservation only pays off if the window actually fires)."""
    with _s()._db() as conn:
        _ensure_table(conn)
        conn.execute(
            'UPDATE self_edit_log SET reserve_window = NULL '
            'WHERE reserve_window = ?', (window_id,))


def _window_applied_paths(window_id):
    with _s()._db() as conn:
        _ensure_table(conn)
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT path FROM self_edit_log WHERE window_id = ? "
            "AND status = 'applied'", (window_id,)).fetchall()]


def _window_edit_summaries(window_id):
    with _s()._db() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT id, agent, path, summary FROM self_edit_log "
            "WHERE window_id = ? AND status = 'applied' ORDER BY created",
            (window_id,)).fetchall()
    return [{'id': r[0], 'agent': r[1], 'path': r[2], 'summary': r[3]}
            for r in rows]


def _window_unlanded(window_id):
    """Reserved edits of this window that did NOT end applied (conflicts,
    apply failures). Each one is a change-request issue the validator must
    see -- and the note requirement exists for exactly this."""
    with _s()._db() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT id, status FROM self_edit_log WHERE reserve_window = ? "
            "AND status != 'applied'", (window_id,)).fetchall()
    return [{'id': r[0], 'status': r[1]} for r in rows]


def validation_window():
    """The fired window currently holding for its validator's push decision
    (still inside its slot). Drives serve's wake exemption so validator
    traffic reaches the server without waking the tank mid-window."""
    import math
    now = time.time()
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        row = conn.execute(
            "SELECT id FROM dormancy_windows WHERE status = 'fired' "
            "AND push_status = 'awaiting_validator' AND end_ts >= ? "
            'ORDER BY start_ts DESC LIMIT 1', (math.floor(now),)).fetchone()
    return row[0] if row else None


def window_validator(window_id):
    return _window_field(window_id, 'validator')


def _telegram_change_request_created(window_id):
    """Player Telegram message when a change request is created, with the
    details needed to judge it and a one-click veto link. The link is
    token-gated and needs no login. Telegram caps a message at 4096 chars."""
    row = _window_row(window_id) or {}
    token = row.get('veto_token') or ''
    base = _s().ESCALATION_BASE_URL.rstrip('/')
    veto_url = (f'{base}/api/selfedit/window/veto-link'
                f'?windowId={window_id}&token={token}')
    try:
        files = json.loads(row.get('file_manifest') or '[]')
    except (TypeError, ValueError):
        files = []
    title = row.get('title') or row.get('reason') or '(untitled change)'
    body = (
        f'Change request {window_id}: {title}\n'
        f'Status: {row.get("status") or "?"}\n'
        f'Implementer: {row.get("agent") or "?"}\n'
        f'Validator: {row.get("validator") or "?"}\n'
        f'Window: {row.get("date") or "?"} {row.get("start") or "?"} '
        f'({row.get("duration_min") or "?"} min)\n'
        f'Risk: {row.get("risk_level") or "low"}; '
        f'mode: {row.get("service_mode") or "idle_ok"}\n'
        f'Files: {", ".join(str(f) for f in files) or "(none)"}\n'
        f'Reason: {(row.get("reason") or "")[:500]}\n\n'
        f'Veto this change request: {veto_url}'
    )
    return _notify_player_telegram('[AI Think Tank] Change request created',
                                   body[:4096])


def veto_window_by_link(window_id, token, reason='Vetoed from Telegram link'):
    """Veto a change request from the tokenized Telegram link. The token is
    the authentication; no player session is needed. Fails closed on a
    missing/mismatched token and delegates every status rule to veto_window."""
    row = _window_row(window_id)
    if not row or not token:
        return False, {'error': 'invalid or expired veto link'}
    if not secrets.compare_digest(str(row.get('veto_token') or ''), str(token)):
        return False, {'error': 'invalid or expired veto link'}
    return veto_window(window_id, 'player',
                       (reason or 'Vetoed from Telegram link')[:300])


def _telegram_change_request(window_id, category, note, sha=None, n_edits=None):
    """One player Telegram message per final change-request outcome."""
    row = _window_row(window_id) or {}
    validator = row.get('validator') or '?'
    if category == 'successful':
        head = (f'Change request {window_id}: SUCCESSFUL -- {n_edits or "?"} '
                f'edit(s) passed the repo gate and validator {validator}, '
                'pushed to main' + (f' ({sha})' if sha else ''))
    elif category == 'successful_with_issues':
        head = (f'Change request {window_id}: SUCCESSFUL WITH ISSUES -- '
                f'pushed to main' + (f' ({sha})' if sha else '')
                + f' (validated by {validator})')
    else:
        head = (f'Change request {window_id}: UNSUCCESSFUL -- nothing was '
                'pushed to main')
    labels = {'successful': 'SUCCESSFUL',
              'successful_with_issues': 'SUCCESSFUL WITH ISSUES',
              'unsuccessful': 'UNSUCCESSFUL'}
    label = labels.get(category, str(category).upper())
    body = f'Outcome: {label}\nEnded: {time.strftime("%Y-%m-%d %H:%M:%S %Z")}\n\n{head}'
    if note:
        body += f'\n\nExplanation: {note}'
    if category == 'unsuccessful':
        body += ('\n\nNothing was pushed. The tank remains on the last good '
                 'main; review the explanation above before retrying.')
    return _notify_player_telegram(
        f'[AI Think Tank] Change request ended: {label}', body[:4096])


def _hold_for_validator(window_id, remediation_used, tail):
    """Gate-green handoff: the change request waits for the validator's
    explicit approval before anything is pushed. Idempotent per window."""
    serve = _s()
    row = _window_row(window_id)
    if not row or row['status'] != 'fired' or row.get('push_status'):
        return
    _update_window(window_id, push_status='awaiting_validator',
                   remediation_used=1 if remediation_used else 0,
                   gate_tail=(tail or '')[-POST_APPLY_TAIL_LEN:])
    edits = _window_edit_summaries(window_id)
    serve.log_action(window_id, 'selfedit_validation_requested',
                     {'edits': [e['id'] for e in edits],
                      'remediationUsed': bool(remediation_used)},
                     authorized=True)
    validator = row.get('validator')
    if not validator:
        return
    lines = [
        f'Change window {window_id} applied {len(edits)} edit(s) and the repo '
        'test gate PASSED on the live tree. You are the validator for this '
        'change request: review the applied result, then approve or deny the '
        'push with the selfedit_validate tool.',
        'Edits in this change request:',
    ]
    for e in edits:
        lines.append(f"- {e['path']} ({e['agent']}): {(e['summary'] or '')[:200]}")
    if remediation_used:
        lines.append('NOTE: in-window remediation fixes were needed before '
                     'the gate passed -- approving WITHOUT a note is refused; '
                     'your note becomes the change-request record.')
    lines.append('Approving pushes straight to main (a note is required when '
                 'there are issues). Denying rolls the window back like a '
                 'failed gate -- nothing is pushed.')
    deadline = time.strftime('%H:%M', time.localtime(row['end_ts']))
    lines.append(f'Window deadline: {deadline} tank time. If the slot ends '
                 'before you decide, the window rolls back automatically.')
    lines.append('Gate tail:\n' + (tail or '')[-POST_APPLY_TAIL_LEN:])
    _notify_agent(validator, '\n'.join(lines), about=f'selfedit:{window_id}')


def _window_diff(window_id):
    """Bounded pre-edit/current diff for the validator turn. The applied rows
    are exactly the change request's bytes, so a unified diff is the smallest
    honest review surface; a missing backup degrades to a named limitation."""
    where = _world()
    with _s()._db() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            'SELECT id, agent, path FROM self_edit_log WHERE window_id = ? '
            "AND status = 'applied' ORDER BY created", (window_id,)).fetchall()
    parts = []
    for row_id, agent, display in rows:
        target = _target_from_display(display)
        backup = os.path.join(where, '.selfedit_backups', row_id)
        try:
            with open(backup, 'rb') as fh:
                before = fh.read().decode('utf-8', errors='replace')
            with open(target, 'rb') as fh:
                after = fh.read().decode('utf-8', errors='replace')
        except OSError as exc:
            parts.append(f'{display} ({agent}): diff unavailable: {exc}')
            continue
        diff = ''.join(difflib.unified_diff(
            before.splitlines(True), after.splitlines(True),
            fromfile=f'{display} (before)', tofile=f'{display} (after)',
            n=3))
        parts.append(f'{display} ({agent}):\n'
                     + (diff or '(no line diff; binary or line-ending change)'))
    text = '\n\n'.join(parts) or '(no applied edits to diff)'
    if len(text) > VALIDATOR_TURN_MAX_DIFF_CHARS:
        text = text[:VALIDATOR_TURN_MAX_DIFF_CHARS] + '\n[diff truncated]'
    return text


def _validator_brief(window_id):
    row = _window_row(window_id) or {}
    return {
        'window': row,
        'edits': _window_edit_summaries(window_id),
        'unlanded': _window_unlanded(window_id),
        'diff': _window_diff(window_id),
        'tail': (row.get('gate_tail') or '')[-VALIDATOR_TURN_MAX_TAIL_CHARS:],
    }


SELFEDIT_VALIDATE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'selfedit_validate',
        'description': ('Record the validator decision for one change request. '
                        'approve=true pushes the applied bytes to origin main; '
                        'approve=false rolls the window back with nothing '
                        'pushed. A non-empty note is required when remediation '
                        'was used, any reserved edit failed to apply, or you '
                        'flag issues.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'windowId': {'type': 'string',
                             'description': 'The change window id under review.'},
                'approved': {'type': 'boolean',
                             'description': 'true to push, false to roll back.'},
                'note': {'type': 'string',
                         'description': 'Why, and what issues you found.'},
                'issues': {'type': 'boolean',
                           'description': 'true when you have concerns beyond '
                                          'the automatic issue checks.'},
            },
            'required': ['windowId', 'approved'],
        },
    },
}


def _default_validator_decider(window_id, validator, brief):
    """One model turn as the named validator. Forced to the decision tool so a
    prose answer cannot be mistaken for consent. Evidence is wrapped as
    external data; the validator identity is the window's own."""
    serve = _s()
    row = brief['window']
    state = serve.get_state_from_db() or {}
    agents = state.get('agents') or {}
    agent = agents.get(validator) if isinstance(agents, dict) else {}
    if not isinstance(agent, dict):
        agent = {}
    name = agent.get('name') or validator
    role = agent.get('role') or 'agent'
    diff_block, _n, _t, diff_inst = serve.wrap_external_content(
        brief['diff'], 'the applied change diff')
    tail_block, _n2, _t2, tail_inst = serve.wrap_external_content(
        brief['tail'] or '(no gate output recorded)', 'the repo test gate output')
    edit_lines = [f"- {e['path']} by {e['agent']}: {(e['summary'] or '')[:240]}"
                  for e in brief['edits']]
    unlanded = [f"- {u['id']}: {u['status']}" for u in brief['unlanded']]
    system = (
        f'You are {name}, a {role} in an AI think tank. You are the named '
        'validator for one change request. The tank is dormant while you '
        'decide, and your decision is the only gate before the applied bytes '
        'are pushed to the shared main branch. Review the diff, the applied '
        'edit summaries, and the repo test gate output. Approve only when the '
        'change matches the stated reason, is safe to ship, and the gate '
        'output actually supports it. Deny when the change is unrelated, '
        'incomplete, unsafe, or the evidence is insufficient. If in-window '
        'remediation was used, any reserved edit did not land, or you have '
        'any concern, set issues=true and write a non-empty note. Return '
        'exactly one selfedit_validate tool call. Do not return prose.'
    )
    user = ('## Change request\n'
            f"Window: {window_id}\n"
            f"Implementer: {row.get('agent') or '?'}\n"
            f"Reason: {row.get('reason') or '(none)'}\n"
            f"Remediation used: {'yes' if row.get('remediation_used') else 'no'}\n\n"
            '## Applied edits\n' + ('\n'.join(edit_lines) or '(none)') + '\n\n'
            '## Reserved edits that did not land\n'
            + ('\n'.join(unlanded) or '(none)') + '\n\n'
            '## Diff instruction\n' + diff_inst + '\n' + diff_block + '\n\n'
            '## Gate instruction\n' + tail_inst + '\n' + tail_block)
    model = serve._resolve_model_tier(
        f'Review applied self-edits for change request {window_id}',
        task_type='review')
    data = serve._post_openrouter_raw(
        model, [{'role': 'system', 'content': system},
                {'role': 'user', 'content': user}],
        tools=[SELFEDIT_VALIDATE_TOOL], max_tokens=800,
        tool_choice={'type': 'function',
                     'function': {'name': 'selfedit_validate'}})
    usage = (data or {}).get('usage') or {}
    cost = usage.get('cost') or 0
    if cost:
        serve._accrue_spend('selfedit_validation', cost, agent_id=validator)
    message = (((data or {}).get('choices') or [{}])[0].get('message') or {})
    calls = message.get('tool_calls') or []
    if not calls:
        raise RuntimeError('validator returned no selfedit_validate tool call')
    raw = ((calls[0].get('function') or {}).get('arguments')) or '{}'
    try:
        args = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'validator tool arguments were not JSON: {exc}')
    if (args.get('windowId') or '').strip() != window_id:
        raise RuntimeError('validator tool call named a different window')
    if 'approved' not in args:
        raise RuntimeError('validator tool call omitted approved')
    return {'approved': bool(args.get('approved')),
            'note': (args.get('note') or '').strip(),
            'issues': bool(args.get('issues')),
            'model': model, 'cost': cost}


def pending_validator_windows(now=None, limit=5):
    """Fired windows that still need a validator turn. `validator_status`
    gates retries: a failed transport wait VALIDATOR_TURN_RETRY_S, and a turn
    that has run out of attempts stops consuming model budget."""
    now = time.time() if now is None else now
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        rows = conn.execute(
            "SELECT id FROM dormancy_windows WHERE status = 'fired' "
            "AND push_status = 'awaiting_validator' AND end_ts >= ? "
            "AND COALESCE(validator_attempts, 0) < ? "
            "AND (validator_status IS NULL "
            "     OR ((validator_status = 'pending' "
            "          OR validator_status = 'deciding') "
            "         AND COALESCE(validator_attempt_at, 0) <= ?)) "
            'ORDER BY start_ts LIMIT ?',
            (now, VALIDATOR_TURN_MAX_ATTEMPTS,
             now - VALIDATOR_TURN_RETRY_S, limit)).fetchall()
    return [r[0] for r in rows]


def run_validator_review(window_id, decider=None, now=None):
    """Run ONE narrow validator decision turn while the tank is dormant.
    Returns (ok, payload). The decision still goes through validate_window(),
    so only the window's named validator can record it and every push/rollback
    path is unchanged."""
    serve = _s()
    now = time.time() if now is None else now
    row = _window_row(window_id)
    if not row:
        return False, {'error': 'unknown window id'}
    if row.get('status') != 'fired' or row.get('push_status') != 'awaiting_validator':
        return False, {'error': 'window is not awaiting validation'}
    if row['end_ts'] < now:
        return False, {'error': 'window slot already ended'}
    validator = (row.get('validator') or '').strip()
    if not validator:
        return False, {'error': 'window has no validator'}
    attempts = int(row.get('validator_attempts') or 0)
    if attempts >= VALIDATOR_TURN_MAX_ATTEMPTS:
        _update_window(window_id, validator_status='failed')
        return False, {'error': 'validator turn already used all attempts'}
    with serve._db() as conn:
        _ensure_windows_table(conn)
        cur = conn.execute(
            "UPDATE dormancy_windows SET validator_status = 'deciding', "
            'validator_attempt_at = ?, '
            'validator_attempts = COALESCE(validator_attempts, 0) + 1 '
            "WHERE id = ? AND status = 'fired' "
            "AND push_status = 'awaiting_validator' AND end_ts >= ? "
            "AND COALESCE(validator_attempts, 0) < ? "
            "AND (validator_status IS NULL "
            "     OR ((validator_status = 'pending' "
            "          OR validator_status = 'deciding') "
            "         AND COALESCE(validator_attempt_at, 0) <= ?))",
            (now, window_id, now, VALIDATOR_TURN_MAX_ATTEMPTS,
             now - VALIDATOR_TURN_RETRY_S))
    if cur.rowcount != 1:
        return False, {'error': 'another validator turn owns this window right now'}
    attempts += 1
    serve.log_action(window_id, 'selfedit_validator_turn_started',
                     {'validator': validator, 'attempt': attempts},
                     authorized=True)
    try:
        brief = _validator_brief(window_id)
        decider = decider or _default_validator_decider
        verdict = decider(window_id, validator, brief)
    except Exception as exc:  # transport/shape failure is not consent
        status = ('failed' if attempts >= VALIDATOR_TURN_MAX_ATTEMPTS
                  else 'pending')
        _update_window(window_id, validator_status=status)
        serve.log_action(window_id, 'selfedit_validator_turn_failed',
                         {'validator': validator, 'attempt': attempts,
                          'error': str(exc)[:300]}, authorized=False)
        return False, {'error': f'validator turn failed: {exc}'}
    approved = bool((verdict or {}).get('approved'))
    note = ((verdict or {}).get('note') or '').strip()
    issues = bool((verdict or {}).get('issues'))
    ok, payload = validate_window(validator, window_id, approved, note, issues)
    if ok:
        _update_window(window_id,
                       validator_status='approved' if approved else 'denied',
                       validated_at=time.time())
        serve.log_action(window_id, 'selfedit_validator_decided',
                         {'validator': validator, 'approved': approved,
                          'issues': issues,
                          'model': (verdict or {}).get('model'),
                          'attempt': attempts}, authorized=True)
    else:
        status = ('failed' if attempts >= VALIDATOR_TURN_MAX_ATTEMPTS
                  else 'pending')
        _update_window(window_id, validator_status=status)
        serve.log_action(window_id, 'selfedit_validator_decision_refused',
                         {'validator': validator, 'attempt': attempts,
                          'error': str((payload or {}).get('error'))[:300]},
                         authorized=False)
    return ok, payload


def _fail_window_push(window_id, reason, by=None):
    """Roll the window back like a failed gate and record an unsuccessful
    outcome: candidates saved under .selfedit_failed/<window>/, tree restored,
    Telegram + action log tell the player."""
    serve = _s()
    applied = _window_participants(window_id, 'applied')
    _rollback_rows(applied, reason, window_id)
    _update_window(window_id, push_status='failed',
                   push_category='unsuccessful',
                   push_note=reason[:MAX_PUSH_NOTE_LEN])
    serve.log_action(window_id, 'repo_push',
                     {'category': 'unsuccessful', 'reason': reason[:300],
                      'by': by or 'system', 'edits': applied},
                     authorized=True)
    _telegram_change_request(window_id, 'unsuccessful', reason)
    return True, {'ok': True, 'status': 'unsuccessful',
                  'message': f'{reason} -- the window rolled back; nothing '
                             'was pushed'}


def _agent_display_name(agent_id):
    """The human-facing name for an agent id, read from the live roster. Falls
    back to the id when the roster cannot be read, so a push never loses the
    attribution line (the id is still a correct, stable reference)."""
    if not agent_id:
        return '?'
    if agent_id == 'player':
        return 'player'
    try:
        state = _s().get_state_from_db() or {}
    except Exception:
        return str(agent_id)
    for row in state.get('agentRoster') or []:
        if isinstance(row, dict) and row.get('id') == agent_id:
            return row.get('name') or agent_id
    return str(agent_id)
