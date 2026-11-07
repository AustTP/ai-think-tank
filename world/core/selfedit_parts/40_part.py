

def _commit_message(window_id, row, edits):
    """Commit text for a validated change request. Attribution is part of the
    contract: the approving supervisor, the chief of staff, the implementer,
    and the independent validator are all named, so `git log` answers who
    approved and who checked the work without reading the tank's database."""
    implementer = _agent_display_name(row.get('agent'))
    validator = _agent_display_name(row.get('validator'))
    supervisor = _agent_display_name(row.get('supervisor'))
    chief = _agent_display_name(row.get('admin_agent'))
    lines = [f'selfedit: change window {window_id}', '',
             (row.get('reason') or '').strip() or '(no reason recorded)', '',
             f'Implemented by {implementer}; validated by {validator}.', '',
             'Change request:',
             f'Approving supervisor: {supervisor}',
             f'Chief of staff: {chief}',
             f'Implementer: {implementer}',
             f'Validator: {validator}', '']
    for e in edits[:20]:
        lines.append(f"- {e['path']}: {(e['summary'] or '')[:160]}")
    return '\n'.join(lines)


def _push_window(window_id, category, note):
    """Commit exactly this window's applied files and push origin main.
    The gate already ran on these exact bytes while the tank was dormant;
    the status check guards against unrelated tracked changes sneaking in."""
    serve = _s()
    row = _window_row(window_id)
    paths = _window_applied_paths(window_id)
    if not row or not paths:
        return _fail_window_push(window_id,
                                 'no applied edits to push (unexpected state)')
    rc, out, err = _git_run(['status', '--porcelain'])
    if rc != 0:
        return _fail_window_push(window_id,
                                 f'git status failed: {(err or out)[-300:]}')
    expected = set(paths)
    for line in out.splitlines():
        if not line.strip():
            continue
        st, path = line[:2], line[3:].strip()
        if st.strip() in ('??', '!!'):
            continue  # untracked/ignored noise is not part of the push
        if ' -> ' in path:
            path = path.split(' -> ', 1)[1]
        if path not in expected:
            return _fail_window_push(
                window_id,
                f'unexpected tracked change in the tree ({path}) -- only '
                "this change request's files may land")
    edits = _window_edit_summaries(window_id)
    message = _commit_message(window_id, row, edits)
    for args in (['add', *paths], ['commit', '-m', message],
                 ['push', 'origin', 'main']):
        rc, out, err = _git_run(args)
        if rc != 0:
            return _fail_window_push(
                window_id, f'git {args[0]} failed: {(err or out)[-300:]}')
    rc, sha_out, _err = _git_run(['rev-parse', 'HEAD'])
    sha = sha_out.strip()[:12] if rc == 0 and sha_out.strip() else ''
    ci_checkable = shutil.which('gh') is not None
    _update_window(window_id, push_status='awaiting_ci' if ci_checkable
                   else 'pushed', push_category=category,
                   push_note=(note or None), push_sha=sha,
                   pushed_at=time.time())
    serve.log_action(window_id, 'repo_push',
                     {'category': category, 'sha': sha, 'edits': len(edits),
                      'agent': row.get('agent'),
                      'validator': row.get('validator'),
                      'note': (note or '')[:300]},
                     authorized=True)
    _telegram_change_request(window_id, category, note, sha=sha,
                             n_edits=len(edits))
    _notify_agent(row.get('agent'), (
        f'Change window {window_id} was validated by {row.get("validator")} '
        f'and PUSHED to main ({sha or "sha unavailable"}), category '
        f'{category}.' + (f' Note: {note}' if note else '')),
        about=f'selfedit:{window_id}')
    return True, {'ok': True, 'status': category, 'sha': sha,
                  'edits': len(edits),
                  'message': f'pushed to origin main ({sha or "sha unavailable"}), '
                             f'category {category}'}


def admin_approve_window(agent_id, window_id, approved, note=''):
    """The admin's push approval for a change request. The director validator
    approves the applied bytes; this is the second, independent approval."""
    serve = _s()
    state = serve.get_state_from_db() or {}
    if not serve._is_admin(state, agent_id):
        return False, {'error': 'only the admin may approve a change request push'}
    row = _window_row(window_id)
    if not row:
        return False, {'error': 'unknown window id'}
    if row.get('status') != 'fired' or row.get('push_status') != 'awaiting_validator':
        return False, {'error': 'nothing is waiting for admin approval right now'}
    note = (note or '').strip()[:MAX_PUSH_NOTE_LEN]
    if not approved:
        if not note:
            return False, {'error': 'a note is REQUIRED for an unsuccessful '
                                    'change request -- say why the push is '
                                    'being denied, then deny again'}
        _update_window(window_id, admin_status='denied', admin_note=note,
                       admin_approved_at=time.time(), admin_agent=agent_id)
        _fail_window_push(
            window_id, f'admin {agent_id} denied the push: {note}', by=agent_id)
        return True, {'ok': True, 'status': 'unsuccessful',
                      'message': 'admin denied; the window rolled back'}
    _update_window(window_id, admin_status='approved', admin_note=note,
                   admin_approved_at=time.time(), admin_agent=agent_id)
    if row.get('validator_status') != 'approved':
        return True, {'ok': True, 'status': 'awaiting_director',
                      'message': 'admin approved; the director validator must '
                                 'still approve the applied bytes'}
    has_issues = bool(row.get('remediation_used') or _window_unlanded(window_id)
                      or row.get('validator_note'))
    return _push_window(window_id,
                        'successful_with_issues' if has_issues else 'successful',
                        note)


def validate_window(agent_id, window_id, approved, note, issues=False):
    """The validator's push decision for a held change request. Only the
    window's validator may call this, only while the slot is open and the
    window holds. Deny = rollback (nothing pushed). A note is REQUIRED for
    every unsuccessful outcome and for successful outcomes with issues
    (remediation was used, reserved edits failed to apply, or the validator
    flags concerns)."""
    with _VALIDATION_LOCK:
        return _validate_window_unlocked(agent_id, window_id, approved, note,
                                         issues)


def _validate_window_unlocked(agent_id, window_id, approved, note,
                              issues=False):
    agent_id = (agent_id or '').strip()
    note = (note or '').strip()[:MAX_PUSH_NOTE_LEN]
    row = _window_row(window_id)
    if not row:
        return False, {'error': 'unknown window id'}
    if row.get('validator') != agent_id:
        state = _s().get_state_from_db() or {}
        if (governance.admin_change_request_enabled(state)
                and _s()._is_admin(state, agent_id)):
            return admin_approve_window(agent_id, window_id, approved, note)
        return False, {'error': 'only the change request\'s validator may '
                                'validate it (you are not on record as the '
                                'validator)'}
    if row['status'] != 'fired' or row.get('push_status') != 'awaiting_validator':
        return False, {'error': f"window {window_id} is {row['status']!r} / "
                                f"push {row.get('push_status')!r} -- nothing "
                                'is waiting for validation right now'}
    # A validator turn that started before the slot ended may finish after it:
    # the alternative is losing a real decision to a slow model call. The
    # end-of-slot sweep still rolls back an unanswered window on the next tick.
    grace = bool(row.get('validator_status') == 'deciding'
                 and row.get('validator_attempt_at')
                 and float(row['validator_attempt_at']) <= float(row['end_ts']))
    if row['end_ts'] < time.time() and not grace:
        return False, {'error': 'the window slot has ended -- the sweep will '
                                'roll the edits back; schedule a fresh window'}
    if not approved:
        if not note:
            return False, {'error': 'a note is REQUIRED for an unsuccessful '
                                    'change request -- say why the push is '
                                    'being denied, then deny again'}
        _fail_window_push(window_id,
                          f'validator {agent_id} denied the push: {note}',
                          by=agent_id)
        _update_window(window_id, validator_status='denied',
                       validator_note=note, validated_at=time.time())
        return True, {'ok': True, 'status': 'unsuccessful',
                      'message': 'denial recorded -- the window rolled back '
                                 'and nothing was pushed'}
    remediation_used = bool(row.get('remediation_used'))
    unlanded = _window_unlanded(window_id)
    has_issues = bool(remediation_used or unlanded or issues)
    if has_issues and not note:
        why = []
        if remediation_used:
            why.append('in-window remediation was needed')
        if unlanded:
            why.append(f'{len(unlanded)} reserved edit(s) did not apply')
        if issues:
            why.append('you flagged issues')
        return False, {'error': 'a note is REQUIRED for a change request '
                                'with issues (' + '; '.join(why) + ') -- '
                                'describe them for the record, then approve '
                                'again'}
    _update_window(window_id, validator_status='approved',
                   validator_note=note, validated_at=time.time())
    state = _s().get_state_from_db() or {}
    if (governance.admin_change_request_enabled(state)
            and row.get('admin_status') != 'approved'):
        return True, {'ok': True, 'status': 'awaiting_admin',
                      'message': 'director approved; the admin must approve '
                                 'the push before anything lands'}
    category = 'successful_with_issues' if has_issues else 'successful'
    return _push_window(window_id, category, note)


# ---------------------------------------------------------------------------
# CI gate for change requests (2026-11-07 operator direction): a pushed
# window is NOT successful until the repo's CI run on the pushed sha is
# green. The push marks the window awaiting_ci; window_tick sweeps it. A
# red (or timed-out) CI reverts the push -- main never sits red, the same
# contract as the local rollback rules. Missing gh tooling cannot verify,
# which is recorded honestly instead of claimed green.
# ---------------------------------------------------------------------------
_CI_WAIT_TIMEOUT_S = 900
_CI_POLL_S = 30


def _gh_run_json(sha):
    """The CI runs for a pushed sha via the gh CLI, or None when gh is
    unavailable or the query fails (unverifiable, never guessed green)."""
    gh = shutil.which('gh')
    if not gh:
        return None
    try:
        proc = subprocess.run(
            [gh, 'run', 'list', '--commit', sha, '--limit', '5',
             '--json', 'databaseId,status,conclusion'],
            cwd=_repo_root(), capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout or '[]')
    except Exception:
        return None


def _ci_sweep_pushed_windows():
    """Resolve every awaiting_ci window against its CI run. Runs from
    window_tick, so nothing here blocks the validator's request."""
    serve = _s()
    now = time.time()
    with serve._db() as conn:
        rows = conn.execute(
            "SELECT id, push_sha, pushed_at FROM dormancy_windows "
            "WHERE push_status = 'awaiting_ci'").fetchall()
    for window_id, sha, pushed_at in rows:
        if not sha:
            _update_window(window_id, push_status='pushed',
                           push_note=(_self_note(window_id) or '') +
                           ' | CI unverified (no sha recorded)')
            continue
        runs = _gh_run_json(sha)
        if runs is None or not runs:
            if now - (pushed_at or now) > _CI_WAIT_TIMEOUT_S:
                _update_window(window_id, push_status='pushed',
                               push_note='CI unverified: no CI run found for '
                                         + sha + ' within the wait window')
                serve.log_action(window_id, 'repo_push_ci_unverified',
                                 {'sha': sha}, authorized=True)
                _notify_player_telegram(
                    'AI Think Tank: change request CI unverified',
                    'Window ' + window_id + ' pushed ' + sha
                    + ' but no CI run was found within '
                    + str(_CI_WAIT_TIMEOUT_S // 60) + ' minutes; the change '
                    'request stands, verify CI manually.')
            continue  # within the wait window: keep polling
        if any(r.get('conclusion') == 'failure' for r in runs):
            rc, out, err = _git_run(['revert', '--no-edit', sha])
            if rc == 0:
                rc, out, err = _git_run(['push', 'origin', 'main'])
            note = ('CI failed on the pushed sha ' + sha
                    + ('; auto-reverted' if rc == 0
                       else '; AUTO-REVERT CONFLICTED: ' + (err or out)[-160:]))
            _update_window(window_id, push_status='failed',
                           push_category='unsuccessful', push_note=note)
            serve.log_action(window_id, 'repo_push_ci_failed',
                             {'sha': sha, 'reverted': rc == 0,
                              'note': note}, authorized=True)
            _notify_player_telegram(
                'AI Think Tank: change request CI FAILED',
                'Window ' + window_id + ' pushed ' + sha + ' and CI is red. '
                + note)
            _notify_agent(row_agent(window_id), (
                'Change window ' + window_id + ' was pushed but CI FAILED on '
                + sha + '. ' + note), about='selfedit:' + window_id)
            continue
        if all(r.get('conclusion') == 'success' for r in runs):
            _update_window(window_id, push_status='pushed',
                           push_note='CI green on ' + sha)
            serve.log_action(window_id, 'repo_push_ci_passed',
                             {'sha': sha}, authorized=True)
            _notify_player_telegram(
                'AI Think Tank: change request CI green',
                'Window ' + window_id + ' pushed ' + sha
                + ' and CI passed -- the change request is successful.')
            _notify_agent(row_agent(window_id), (
                'Change window ' + window_id + ' CI is GREEN on ' + sha
                + ' -- the change request is successful.'),
                about='selfedit:' + window_id)
            continue
        # Still running: keep waiting until the timeout sweeps it.


def _self_note(window_id):
    row = _window_row(window_id)
    return (row or {}).get('push_note') or ''


def row_agent(window_id):
    row = _window_row(window_id)
    return (row or {}).get('agent')
