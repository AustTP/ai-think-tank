

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
    _update_window(window_id, push_status='pushed', push_category=category,
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
