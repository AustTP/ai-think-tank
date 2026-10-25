

def apply_pass():
    """Apply every armed edit while the tank is DORMANT, then TEST the live
    tree with the repo's own gate script (change-window semantics). Thread-
    safe from the serve loop (called through asyncio.to_thread).

    Returns the number of edits landed. On test failure the edits either
    enter the window's remediation session (fixable until the window ends,
    else automatic rollback) or, outside a window, roll back immediately;
    every proposer is mailed either way."""
    serve = _s()
    if not serve._dormant():
        return 0
    window_id = _active_window_id()
    with serve._db() as conn:
        _ensure_table(conn)
        if window_id:
            # inside a change window, ONLY the window's reserved edits apply
            # (two-agent contract: the validator reviews exactly this
            # change request's bytes, not bystander edits)
            rows = conn.execute(
                "SELECT id, path, sha_before FROM self_edit_log "
                "WHERE status = 'armed' AND reserve_window = ? "
                'ORDER BY created', (window_id,)).fetchall()
        else:
            # the idle path applies unreserved armed edits; it runs the same
            # post-apply tests but never pushes (no change request owns it)
            rows = conn.execute(
                "SELECT id, path, sha_before FROM self_edit_log "
                "WHERE status = 'armed' AND reserve_window IS NULL "
                "AND COALESCE(apply_policy, 'idle_or_window') != 'window_only' "
                'ORDER BY created').fetchall()
    if not rows:
        return 0
    kernel_rows = [r for r in rows if is_kernel(_target_from_display(r[1]))]
    if kernel_rows:
        # A kernel edit is applied alone: the restart replaces the process, so
        # mixing it with reload-based edits in one pass has no coherent order.
        row_id, display, sha_before = kernel_rows[0]
        return _apply_kernel(row_id, display, sha_before)
    applied = 0
    landed = []
    for row_id, display, sha_before in rows:
        try:
            if _apply_one(row_id, display, sha_before, window_id):
                applied += 1
                landed.append(row_id)
        except Exception as e:  # one bad edit must never stall the loop
            _update(row_id, status='failed', note=str(e)[:200])
            serve.log_action(row_id, 'self_edit_apply_failed',
                             {'error': str(e)[:200]}, authorized=False)
    if window_id and not landed:
        # every reserved edit conflicted or failed at apply time: the change
        # request produced nothing to validate or push
        note = 'no reserved edits applied (all conflicted or failed at apply time)'
        _update_window(window_id, push_status='failed',
                       push_category='unsuccessful', push_note=note)
        _unreserve_window_edits(window_id)
        serve.log_action(window_id, 'repo_push',
                         {'category': 'unsuccessful', 'reason': note},
                         authorized=True)
        _telegram_change_request(window_id, 'unsuccessful', note)
        return 0
    if not landed:
        return 0
    ok, tail = _run_post_apply_tests()
    serve.log_action('selfedit', 'self_edit_post_apply_tests',
                     {'result': 'pass' if ok else 'fail',
                      'edits': applied, 'window': window_id,
                      'tail': tail[:POST_APPLY_TAIL_LEN]}, authorized=True)
    if ok:
        if window_id:
            _hold_for_validator(window_id, remediation_used=False, tail=tail)
        return applied
    if window_id:
        _mark_remediation(window_id, landed, tail)
        return applied
    _rollback_rows(landed,
                   'post-apply tests failed and no change window was open',
                   'no-window')
    return applied


def _run_test_gate(cwd, label, env=None):
    """Run the repo's own gate script in cwd and return (ok, tail)."""
    serve = _s()
    # Tests must not call the live server. When the tank is dormant during a
    # change window, an unmocked SELF_BASE_URL call gets 503 and the gate flakes
    # on tests that are otherwise hermetic. Point the gate at a closed local
    # port and keep Telegram silent.
    run_env = dict(os.environ if env is None else env)
    # serve.py exports OPTIMIZATION_POLICY_MODE at boot. Inheriting it makes
    # the Jev tier-gate tests see the live routing policy and fail. Tests must
    # start from their own default policy, not the server's boot environment.
    run_env.pop('OPTIMIZATION_POLICY_MODE', None)
    run_env.setdefault('SELF_BASE_URL', 'http://127.0.0.1:1')
    run_env.setdefault('TELEGRAM_NOTIFICATIONS_ENABLED', 'false')
    run_env.setdefault('TELEGRAM_SUPPRESS_DURING_TESTS', 'true')
    try:
        done = subprocess.run(
            POST_APPLY_TEST_CMD, cwd=cwd, env=run_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=POST_APPLY_TEST_TIMEOUT_S)
        raw = (done.stdout or b'').decode('utf-8', errors='replace')
        ok = done.returncode == 0
    except subprocess.TimeoutExpired:
        raw = f'{label} test run timed out'
        ok = False
    except Exception as exc:  # a broken test harness fails closed
        raw = f'{label} test run could not start: {exc}'
        ok = False
    tail = raw.strip()[-POST_APPLY_TAIL_LEN:]
    if ok:
        return True, tail
    log_path = ''
    try:
        log_dir = os.path.join(_world(), '.selfedit_failed', 'gate-logs')
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(
            log_dir, f'{label}-{time.strftime("%Y%m%d-%H%M%S")}.log')
        with open(log_path, 'w', encoding='utf-8') as fh:
            fh.write(raw)
    except OSError:
        log_path = ''
    serve.log_action('selfedit', f'self_edit_{label}_tests_failed',
                     {'tail': tail[:POST_APPLY_TAIL_LEN],
                      'log': log_path}, authorized=True)
    return False, tail


def _run_post_apply_tests():
    """Test the edited tree after applying: run the repo's own gate script
    from the repo root and return (ok, tail). The command is a module
    constant so tests can substitute a fast fake."""
    repo_root = os.path.dirname(os.path.abspath(_world()))
    return _run_test_gate(repo_root, 'post_apply')


PRE_APPLY_IGNORE = (
    '.git', '.github', 'agents', 'sandboxes', 'library', 'datasets',
    'think-tank-db-backups', 'design-references', 'tinyprobe_test',
    '__pycache__', '.pytest_cache', '.mypy_cache', 'node_modules',
    '.selfedit_staging', '.selfedit_backups', '.selfedit_failed',
    'think_tank.db', '*.db', '*.db.bak*', '.env*', '.youtube-cookies.txt',
)

# The pre-apply copy must not carry real credentials. Keys whose names look
# secret get a placeholder value; everything else (numbers, booleans, URLs,
# roster configuration) keeps its real value so the gate sees the same
# feature switches as the live server.
PREAPPLY_SECRET_BITS = ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'COOKIE',
                        'ACCOUNT', 'REFRESH', 'HASH', 'SALT', 'NUMBER')
PREAPPLY_ENV_PLACEHOLDER = 'preapply'


def _write_preapply_env(repo_root, tmp):
    env_path = os.path.join(repo_root, '.env')
    if not os.path.isfile(env_path):
        return
    lines = []
    try:
        with open(env_path, encoding='utf-8') as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                if any(bit in key.upper() for bit in PREAPPLY_SECRET_BITS):
                    value = PREAPPLY_ENV_PLACEHOLDER
                lines.append(f'{key}={value}\n')
    except OSError:
        return
    # The gate is hermetic: never let the candidate tree call the live server,
    # and never let a test run push Telegram.
    lines = [line for line in lines
             if not line.startswith(('SELF_BASE_URL=', 'TELEGRAM_NOTIFICATIONS_ENABLED=',
                                     'TELEGRAM_SUPPRESS_DURING_TESTS='))]
    lines.extend([
        'SELF_BASE_URL=http://127.0.0.1:1\n',
        'TELEGRAM_NOTIFICATIONS_ENABLED=false\n',
        'TELEGRAM_SUPPRESS_DURING_TESTS=true\n',
    ])
    dest = os.path.join(tmp, '.env')
    with open(dest, 'w', encoding='utf-8') as fh:
        fh.writelines(sorted(lines))
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass


def _init_preapply_db(tmp):
    db_path = os.path.join(tmp, 'think_tank.db')
    env = {**os.environ, 'THINK_TANK_DB': db_path}
    try:
        done = subprocess.run(
            [sys.executable, '-c', 'import serve; serve.init_db()'],
            cwd=os.path.join(tmp, 'world'), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=180)
    except Exception as exc:
        return None, f'pre-apply database init failed: {exc}'
    if done.returncode != 0:
        tail = (done.stdout or b'').decode('utf-8', errors='replace')[-600:]
        return None, 'pre-apply database init failed: ' + tail
    return db_path, None


def _run_pre_apply_tests(rows):
    """Run the repo gate against a quarantine copy with every reserved
    candidate applied. This is the full pre-apply bar; it is used only for
    high-risk change requests because it copies and tests the whole tree.

    Returns (ok, tail). Any harness failure fails closed."""
    serve = _s()
    repo_root = os.path.dirname(os.path.abspath(_world()))
    staging_root = os.path.join(_world(), '.selfedit_staging')
    os.makedirs(staging_root, exist_ok=True)
    tmp = os.path.join(staging_root, f'pre-{secrets.token_hex(6)}')
    ok = False
    try:
        shutil.copytree(repo_root, tmp,
                        ignore=shutil.ignore_patterns(*PRE_APPLY_IGNORE),
                        dirs_exist_ok=True)
        _write_preapply_env(repo_root, tmp)
        db_path, err = _init_preapply_db(tmp)
        if err:
            return False, err
        root_real = os.path.realpath(tmp)
        for row in rows:
            candidate = _staging_candidate(row['id'])
            if not os.path.isfile(candidate):
                return False, f"candidate for {row.get('id')} is missing"
            dest = os.path.realpath(os.path.join(tmp, row['path']))
            if os.path.commonpath([root_real, dest]) != root_real:
                return False, f"candidate path escapes the pre-apply copy: {row['path']}"
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(candidate, 'rb') as src:
                data = src.read()
            with open(dest, 'wb') as out:
                out.write(data)
        ok, tail = _run_test_gate(
            tmp, 'pre_apply',
            env={**os.environ, 'THINK_TANK_DB': db_path})
        serve.log_action('selfedit', 'self_edit_pre_apply_tests',
                         {'result': 'pass' if ok else 'fail',
                          'edits': len(rows),
                          'tail': tail[:POST_APPLY_TAIL_LEN]},
                         authorized=True)
        return ok, tail
    except Exception as exc:
        return False, f'pre-apply gate could not run: {exc}'
    finally:
        if ok:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            serve.log_action('selfedit', 'self_edit_preapply_preserved',
                             {'path': tmp}, authorized=True)


def _active_window_id():
    """The fired change window covering NOW, or None (idle-path applies
    get no window linkage)."""
    import math
    now = time.time()
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        row = conn.execute(
            "SELECT id FROM dormancy_windows WHERE status = 'fired' "
            'AND start_ts <= ? AND end_ts >= ? '
            'ORDER BY start_ts DESC LIMIT 1',
            (now, math.floor(now))).fetchone()
    return row[0] if row else None


def remediation_window():
    """The open remediation session for serve's wake exemption: a fired
    window still inside its slot with failing edits left. None otherwise.
    Called per request by the middleware -- cheap by design (two small
    indexed queries)."""
    import math
    serve = _s()
    now = time.time()
    with serve._db() as conn:
        _ensure_windows_table(conn)
        window = conn.execute(
            "SELECT id FROM dormancy_windows WHERE status = 'fired' "
            'AND end_ts >= ? ORDER BY start_ts DESC LIMIT 1',
            (math.floor(now),)).fetchone()
        if not window:
            return None
        failing = conn.execute(
            "SELECT COUNT(*) FROM self_edit_log WHERE window_id = ? "
            "AND status = 'failed'", (window[0],)).fetchone()[0]
    return window[0] if failing else None


def _window_participants(window_id, status):
    with _s()._db() as conn:
        _ensure_table(conn)
        return [r[0] for r in conn.execute(
            'SELECT id FROM self_edit_log WHERE window_id = ? '
            'AND status = ?', (window_id, status)).fetchall()]


def _mark_remediation(window_id, landed_ids, tail):
    """Open the remediation session: failed rows keep the latest test tail,
    and every responsible proposer is mailed with what to do until the
    window ends."""
    serve = _s()
    for row_id in landed_ids:
        _update(row_id, status='failed',
                note=f'change window {window_id}: repo tests failed; '
                     f'latest tail: {tail[-200:]}')
    serve.log_action(window_id, 'selfedit_remediation_opened',
                     {'edits': landed_ids}, authorized=True)
    for row_id in landed_ids:
        row = _row_meta(row_id)
        if row:
            agent_id, display = row['agent'], row['path']
            _notify_agent(agent_id, (
                f'Your self-edit {row_id} ({display}) applied inside change '
                f'window {window_id}, but the repo test gate FAILED right '
                f'after. You have until the window ends to fix it: use the '
                f'selfedit_fix tool to splice a correction into the current '
                f'file, then selfedit_recheck to rerun the tests. If the '
                f'issues are not resolved by the end of the window the '
                f'window rolls back automatically, your candidate is '
                f'saved under .selfedit_failed/{window_id}/, and you will '
                f'need to fix the code off-window and schedule again.\n'
                f'Test tail:\n{tail[-POST_APPLY_TAIL_LEN:]}'),
                about=f'selfedit:{row_id}')


def _row_meta(row_id):
    with _s()._db() as conn:
        _ensure_table(conn)
        row = conn.execute(
            'SELECT agent, path FROM self_edit_log '
            'WHERE id = ?', (row_id,)).fetchone()
    return {'agent': row[0], 'path': row[1]} if row else None


def _rollback_rows(row_ids, reason, subfolder):
    """Revert each edit to its pre-edit bytes, save the (rejected)
    candidate under ANOTHER FILE NAME, and re-load the modules. The
    candidate copy is what the responsible agent fixes off-window."""
    serve = _s()
    saved = []
    for row_id in row_ids:
        meta = _row_meta(row_id)
        if not meta:
            continue
        display = meta['path']
        target = _target_from_display(display)
        backups = os.path.join(_world(), '.selfedit_backups')
        pre_bytes = os.path.join(backups, row_id)
        cand_path = os.path.join(backups, f'{row_id}.cand')
        try:
            failed_dir = os.path.join(_world(), '.selfedit_failed', subfolder)
            os.makedirs(failed_dir, exist_ok=True)
            module_name = os.path.splitext(os.path.basename(display))[0]
            new_name = f'{row_id}-{module_name}.py'
            if os.path.isfile(cand_path):
                shutil.copyfile(cand_path, os.path.join(failed_dir, new_name))
                saved.append(new_name)
            if os.path.isfile(pre_bytes):
                shutil.copyfile(pre_bytes, target)
                _reload(target)
            _update(row_id, status='rolled_back',
                    note=f'{reason}; candidate saved as '
                         f'.selfedit_failed/{subfolder}/{new_name}')
            serve.log_action(row_id, 'self_edit_rolled_back',
                             {'reason': reason[:200],
                              'savedAs': (new_name if os.path.isfile(cand_path)
                                          else '')[:160]},
                             authorized=True)
        except Exception as exc:
            _update(row_id, status='failed',
                    note=f'rollback itself failed: {str(exc)[:200]}')
            serve.log_action(row_id, 'self_edit_rollback_failed',
                             {'error': str(exc)[:200]}, authorized=False)
        if meta.get('agent'):
            _notify_agent(meta['agent'], (
                f'Your self-edit {row_id} ({display}) was REVERTED: '
                f'{reason}. The rejected candidate is saved as '
                f'.selfedit_failed/{subfolder}/{new_name} -- use it, '
                f'resolve the issues OFF-window, then file a fresh '
                f'self_edit proposal and schedule a new change window '
                f'with schedule_dormant_apply. nothing changed server-side '
                f'otherwise; the tank is back to the way it was.'),
                about=f'selfedit:{row_id}')
    return saved


def run_remediation_checks(agent_id):
    """Rerun the repo gate NOW during an open remediation session. On pass,
    failing rows recover to applied and the session closes."""
    serve = _s()
    window_id = remediation_window()
    if not window_id:
        return False, {'error': 'no remediation session is open right now '
                                '(the tests pass, or no window is firing)'}
    ok, tail = _run_post_apply_tests()
    serve.log_action(window_id, 'selfedit_remediation_checks',
                     {'by': agent_id or 'unknown',
                      'result': 'pass' if ok else 'fail',
                      'tail': tail[:POST_APPLY_TAIL_LEN]}, authorized=True)
    if ok:
        for row_id in _window_participants(window_id, 'failed'):
            meta = _row_meta(row_id)
            _update(row_id, status='applied',
                    note='tests pass after in-window fixes; recovered to '
                         'applied')
            if meta and meta.get('agent'):
                _notify_agent(meta['agent'], (
                    f'Change window {window_id} checks PASS after the '
                    f'in-window fixes. Your edit is back to applied and '
                    f'clean; the validator now reviews the result before '
                    f'any push.'), about=f'selfedit:{row_id}')
        _update_window(window_id, remediation_used=1)
        _hold_for_validator(window_id, remediation_used=True,
                            tail='tests pass after in-window fixes')
        serve.log_action(window_id, 'selfedit_remediation_cleared', {},
                         authorized=True)
        return True, {'ok': True, 'tests': 'pass',
                      'message': 'the repo gate passes now -- failing '
                                 'edits recovered to applied'}
    return True, {'ok': False, 'tests': 'fail',
                  'tail': tail[-POST_APPLY_TAIL_LEN:]}


def _target_from_display(display):
    """display is repo-root-relative ('world/sim.py'); self-edits only ever
    target world modules, so the real file is basename under world/."""
    return os.path.join(_world(), os.path.basename(display))


# ---------------------------------------------------------------------------
# Kernel apply: serve.py cannot be reloaded, so an edit is written, preflighted
# against the full repo gate, then applied by an in-place os.execv. A detached
# watchdog waits for the new process to answer a public endpoint; if it never
# does, it restores the pre-edit bytes and relaunches the previous kernel.
# ---------------------------------------------------------------------------
_KERNEL_WATCHDOG_SOURCE = """import json, os, shutil, subprocess, sys, time, urllib.request

backup, target, url, cwd, argv_json, timeout = sys.argv[1:7]
argv = json.loads(argv_json)
deadline = time.time() + float(timeout)
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            if resp.status == 200:
                sys.exit(0)
    except Exception:
        pass
    time.sleep(2)
try:
    shutil.copyfile(backup, target)
except Exception:
    pass
try:
    subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
except Exception:
    pass
"""


def _kernel_argv():
    """The exact command line this process was launched with, read from ps so
    the restart reproduces it (host, port, flags) with no guessing. None when
    the command cannot be read -- the caller then refuses to swap the file."""
    try:
        done = subprocess.run(['ps', '-o', 'command=', '-p', str(os.getpid())],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=10)
        command = (done.stdout or b'').decode('utf-8', errors='replace').strip()
    except Exception:
        return None
    if not command:
        return None
    argv = shlex.split(command)
    return argv or None


def _kernel_health_url(argv):
    """Public readiness URL for the relaunched process: the same host/port
    from the launch command, with wildcard binds mapped to loopback. /api/rooms
    is used because /api/health is session-gated and the watchdog has no key."""
    host, port = '127.0.0.1', '8000'
    for i, arg in enumerate(argv):
        if arg == '--host' and i + 1 < len(argv):
            host = argv[i + 1]
        elif arg.startswith('--host='):
            host = arg.split('=', 1)[1]
        elif arg == '--port' and i + 1 < len(argv):
            port = argv[i + 1]
        elif arg.startswith('--port='):
            port = arg.split('=', 1)[1]
    if host in ('0.0.0.0', '::', ''):
        host = '127.0.0.1'
    return f'http://{host}:{port}/api/rooms'


def _spawn_kernel_watchdog(argv, backup, target, health_url):
    path = os.path.join(_world(), KERNEL_WATCHDOG_NAME)
    try:
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(_KERNEL_WATCHDOG_SOURCE)
        subprocess.Popen(
            [sys.executable, path, backup, target, health_url, _world(),
             json.dumps(argv), str(KERNEL_HEALTH_TIMEOUT_S)],
            cwd=_world(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        return True
    except Exception:
        return False


def _run_kernel_preflight():
    """Run the repo gate against the on-disk candidate. The running process
    still holds the old code, so a failing candidate is restored and nothing
    restarts."""
    repo_root = os.path.dirname(os.path.abspath(_world()))
    try:
        done = subprocess.run(
            POST_APPLY_TEST_CMD, cwd=repo_root,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=POST_APPLY_TEST_TIMEOUT_S)
        raw = (done.stdout or b'').decode('utf-8', errors='replace')
        return done.returncode == 0, raw.strip()[-POST_APPLY_TAIL_LEN:]
    except subprocess.TimeoutExpired:
        return False, 'kernel preflight timed out'
    except Exception as exc:
        return False, f'kernel preflight could not start: {exc}'


def _apply_kernel(row_id, display, sha_before):
    """Apply one armed serve.py edit: verify bytes, write, preflight, restart.
    Returns 1 when the file was applied (restart disabled in tests) and 0 when
    the edit failed and the original bytes were restored."""
    serve = _s()
    target = _target_from_display(display)
    if not os.path.isfile(target):
        _update(row_id, status='conflict', note='target missing at apply time')
        return 0
    with open(target, 'r', encoding='utf-8') as fh:
        original = fh.read()
    if hashlib.sha256(original.encode('utf-8')).hexdigest() != sha_before:
        _update(row_id, status='conflict',
                note='target changed after arming -- re-propose against the '
                     'current bytes')
        return 0
    with open(_staging_candidate(row_id), 'r', encoding='utf-8') as fh:
        candidate = fh.read()
    backups = os.path.join(_world(), '.selfedit_backups')
    os.makedirs(backups, exist_ok=True)
    backup = os.path.join(backups, row_id)
    with open(backup, 'w', encoding='utf-8') as fh:
        fh.write(original)
    with open(target, 'w', encoding='utf-8') as fh:
        fh.write(candidate)

    ok, tail = _run_kernel_preflight()
    serve.log_action(row_id, 'self_edit_kernel_preflight',
                     {'path': display, 'result': 'pass' if ok else 'fail',
                      'tail': tail[:POST_APPLY_TAIL_LEN]}, authorized=True)
    argv = _kernel_argv() if ok else None
    if not ok or not argv:
        shutil.copyfile(backup, target)
        note = ('kernel preflight failed' if not ok
                else 'launch command unavailable; refusing to restart')
        _update(row_id, status='failed', note=note)
        serve.log_action(row_id, 'self_edit_kernel_apply_failed',
                         {'path': display, 'note': note}, authorized=False)
        _notify_agent(_row_agent(row_id),
                      f'Your kernel edit to {display} was not applied: {note}. '
                      f'The previous serve.py bytes were restored.\n'
                      f'Test tail:\n{tail[-POST_APPLY_TAIL_LEN:]}',
                      about='selfedit')
        return 0

    _update(row_id, status='applied', applied_at=time.time(),
            note='kernel edit: applied by process restart')
    serve.log_action(row_id, 'self_edit_kernel_applied',
                     {'path': display, 'restart': bool(KERNEL_RESTART_ENABLED)},
                     authorized=True)
    if not KERNEL_RESTART_ENABLED:
        return 1

    health_url = _kernel_health_url(argv)
    watchdog = _spawn_kernel_watchdog(argv, backup, target, health_url)
    if not watchdog:
        # Without the rollback net, do not swap the process.
        shutil.copyfile(backup, target)
        _update(row_id, status='failed', note='watchdog could not start')
        serve.log_action(row_id, 'self_edit_kernel_apply_failed',
                         {'path': display, 'note': 'watchdog could not start'},
                         authorized=False)
        return 0
    try:
        os.chdir(_world())
        os.execv(argv[0], argv)
    except Exception as exc:
        shutil.copyfile(backup, target)
        _update(row_id, status='failed', note=f'restart failed: {exc}'[:200])
        serve.log_action(row_id, 'self_edit_kernel_apply_failed',
                         {'path': display, 'note': f'restart failed: {exc}'[:200]},
                         authorized=False)
        return 0
    return 1


def _row_agent(row_id):
    try:
        with _s()._db() as conn:
            _ensure_table(conn)
            row = conn.execute('SELECT agent FROM self_edit_log WHERE id = ?',
                               (row_id,)).fetchone()
        return (row[0] if row else '') or 'unknown'
    except Exception:
        return 'unknown'


def _schedule_kernel_restart(delay=1.5):
    """Restart after the current response flushes (used by revert). A daemon
    thread is enough: exec replaces the process, and the thread dies with it."""
    import threading

    def _go():
        time.sleep(delay)
        argv = _kernel_argv()
        if not argv:
            return
        try:
            os.chdir(_world())
            os.execv(argv[0], argv)
        except Exception:
            pass

    threading.Thread(target=_go, daemon=True).start()


def _apply_one(row_id, display, sha_before, window_id=None):
    serve = _s()
    target = _target_from_display(display)
    if not os.path.isfile(target):
        _update(row_id, status='conflict', note='target missing at apply time')
        return 0
    with open(target, 'r', encoding='utf-8') as fh:
        original = fh.read()
    if hashlib.sha256(original.encode('utf-8')).hexdigest() != sha_before:
        _update(row_id, status='conflict',
                note='target changed after arming -- re-propose against the '
                     'current bytes')
        return 0
    with open(_staging_candidate(row_id), 'r', encoding='utf-8') as fh:
        candidate = fh.read()
    backups = os.path.join(_world(), '.selfedit_backups')
    os.makedirs(backups, exist_ok=True)
    with open(os.path.join(backups, row_id), 'w', encoding='utf-8') as fh:
        fh.write(original)
    shutil.copyfile(_staging_candidate(row_id),
                    os.path.join(backups, f'{row_id}.cand'))
    with open(target, 'w', encoding='utf-8') as fh:
        fh.write(candidate)
    _reload(target)
    _update(row_id, status='applied', applied_at=time.time(),
            window_id=window_id)
    serve.log_action(row_id, 'self_edit_applied',
                     {'path': display, 'dormant': True,
                      'window': window_id}, authorized=True)
    return 1


def submit_remediation_fix(agent_id, path, find, replace, summary):
    """One corrective splice DURING an open remediation session (inside a
    live change window). The player's window approval already covers
    in-window corrections, so there is no second JEV review: the gates here
    are the mechanical ones (real path, protected refusal, unique anchor,
    compiled splice) and the changes are TESTED by the next recheck. The
    fix itself is what the change window exists to enable."""
    serve = _s()
    agent_id = (agent_id or '').strip() or 'unknown'
    window_id = remediation_window()
    if not window_id:
        return False, {'error': 'no remediation session is open -- submit '
                                'a normal self_edit proposal instead, or '
                                'schedule a change window'}
    summary = (summary or '').strip()[:MAX_SUMMARY_LEN]
    find = find if isinstance(find, str) else ''
    replace = replace if isinstance(replace, str) else ''
    if not find or len(find) > MAX_FIND_LEN:
        return False, {'error': f'find text is required (max {MAX_FIND_LEN} chars)'}
    if len(replace) > MAX_REPLACE_LEN:
        return False, {'error': f'replacement too long (max {MAX_REPLACE_LEN} chars)'}
    if find == replace:
        return False, {'error': 'this fix changes nothing'}
    if not summary:
        return False, {'error': 'a summary is required -- say WHAT broke '
                                'and WHAT this correction changes'}
    resolved, display = _resolve_path(path)
    if resolved is None:
        return False, {'error': display}
    refusal = _path_refusal(resolved, display)
    if refusal:
        return False, {'error': refusal}
    if is_kernel(resolved):
        return False, {'error': 'serve.py is the kernel: an in-window fix '
                                'cannot reload it. Propose a normal kernel '
                                'edit (player signature, preflight, restart).'}
    try:
        with open(resolved, 'r', encoding='utf-8') as fh:
            original = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        return False, {'error': f'cannot read target: {e}'}
    occurrences = original.count(find)
    if occurrences != 1:
        where = 'appears 0 times' if occurrences == 0 else \
            f'appears {occurrences} times'
        return False, {'error': f'the find text must appear exactly once; it '
                                f'{where}. Include more surrounding context '
                                'to make the anchor unique'}
    candidate = original.replace(find, replace, 1)
    syntax_err = _compile_error(candidate)
    if syntax_err:
        return False, {'error': f'fix does not compile: {syntax_err} -- '
                                'correct the splice and resubmit'}
    fix_id = f'fix-{secrets.token_hex(6)}'
    backups = os.path.join(_world(), '.selfedit_backups')
    os.makedirs(backups, exist_ok=True)
    with open(os.path.join(backups, fix_id), 'w', encoding='utf-8') as fh:
        fh.write(original)
    with open(resolved, 'w', encoding='utf-8') as fh:
        fh.write(candidate)
    _reload(resolved)
    _record(fix_id, time.time(), agent_id, display, summary,
            hashlib.sha256(original.encode('utf-8')).hexdigest(),
            hashlib.sha256(candidate.encode('utf-8')).hexdigest(), 'applied')
    _update(fix_id, status='applied', window_id=window_id,
            note=f'in-window fix; pre-fix bytes under backup {fix_id}')
    serve.log_action(fix_id, 'selfedit_fix_applied',
                     {'agent': agent_id, 'path': display,
                      'window': window_id}, authorized=True)
    return True, {'id': fix_id, 'status': 'applied', 'window': window_id,
                  'message': 'fix applied in-window -- run selfedit_recheck '
                             'to test the tree now'}


def _revert_one_from_backup(row_id):
    """Restore one edit's pre-edit bytes from its backup and re-load the
    module. Used by the end-of-window rollback."""
    meta = _row_meta(row_id)
    if not meta:
        return False
    target = _target_from_display(meta['path'])
    backup = os.path.join(_world(), '.selfedit_backups', row_id)
    if not os.path.isfile(backup):
        return False
    shutil.copyfile(backup, target)
    if is_kernel(target):
        _schedule_kernel_restart()
        return True
    try:
        _reload(target)
    except Exception:
        pass
    return True


def _reload(target):
    import importlib
    modname = os.path.splitext(os.path.basename(target))[0]
    mod = sys.modules.get(modname)
    if mod:
        importlib.reload(mod)
    if modname == 'jev':
        _refresh_serve_jev_exports()
    if modname == 'config':
        _refresh_serve_config_exports()
    if modname == 'env_reload':
        _refresh_serve_env_reload_exports()
    _refresh_serve_imports(modname)
    serve = _s()
    for loader_name in POST_RELOAD_LOADERS.get(modname, ()):
        loader = getattr(serve, loader_name, None)
        if callable(loader):
            try:
                loader()
            except Exception:
                pass  # a loader failure leaves the registry empty, never a boot failure
    for extra in RELOAD_IMPORTERS_AFTER.get(modname, ()):
        extra_mod = sys.modules.get(extra)
        if extra_mod:
            importlib.reload(extra_mod)


def _refresh_serve_jev_exports():
    """serve.py's `from jev import (...)` names must point at the reloaded
    jev module's objects after a jev self-edit, or every serve-side call
    site would keep running pre-edit functions (the jev.py apply contract)."""
    import jev
    serve = _s()
    for name in ('JEV_SAFETY_CONFIDENCE', 'QUORUM_SAMPLE_SIZE',
                 'QUORUM_MIN_AGREEING', 'ESCALATION_KIND_RISK',
                 'ESCALATION_DEFAULT_FLOOR', 'ESCALATION_ERROR_PENALTY_STEP',
                 'ESCALATION_ERROR_PENALTY_CAP', '_jev_choice',
                 '_jev_quorum_choice_sync', '_jev_quorum_decision'):
        setattr(serve, name, getattr(jev, name))


_SERVE_IMPORT_NAMES: dict = {}


def _refresh_serve_imports(modname):
    """Rebind every name serve.py imports from modname after a reload.

    The name list is read from serve.py's own `from <mod> import (...)`
    statements with ast, so adding an export later needs no second edit here
    and cannot drift. Function-local imports are irrelevant (call-time
    resolution). A name the reloaded module no longer defines is skipped
    rather than left pointing at the pre-edit object by accident."""
    import ast
    mod = sys.modules.get(modname)
    if mod is None:
        return []
    names = _SERVE_IMPORT_NAMES.get(modname)
    if names is None:
        found = []
        try:
            tree = ast.parse(_read_source(_s().__file__))
        except (OSError, SyntaxError):
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == modname:
                    for alias in node.names:
                        found.append((alias.asname or alias.name, alias.name))
        names = tuple(found)
        if names:
            _SERVE_IMPORT_NAMES[modname] = names
    serve = _s()
    rebound = []
    for attr, source_name in names:
        if hasattr(mod, source_name):
            setattr(serve, attr, getattr(mod, source_name))
            rebound.append(attr)
    return rebound


def _refresh_serve_env_reload_exports():
    """serve.py binds the .env reload table and its flag helper by name, so
    a self-edit to env_reload.py must rebind both or the pre-edit table keeps
    running until a restart."""
    import env_reload
    serve = _s()
    serve._ENV_RELOAD_FIELDS = env_reload.ENV_RELOAD_FIELDS
    serve._env_flag = env_reload._env_flag


def _refresh_serve_config_exports():
    """serve.py's `from config import (...)` block (line ~222) must point at
    the reloaded config module after a config.py self-edit, and sim/content/
    sublow are reloaded by the RELOAD_IMPORTERS_AFTER chain -- their
    `from config import` bindings re-execute against the updated module.
    The reload only ever runs while the tank is dormant, so it lands between
    simulation ticks."""
    import config
    serve = _s()
    for name in ('ALLOW_WATCHLIST_WRITE', 'BROWSE_BLOCK_CATEGORIES',
                 'PLAIN_WRITING_BAN_LIST', 'RESEARCH_WATCHLIST', 'ROOT',
                 'bind_policy', 'get_grants_config', 'get_operator_policy',
                 'note_watchlist_topic', 'reload_policy_config'):
        setattr(serve, name, getattr(config, name))
