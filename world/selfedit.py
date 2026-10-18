"""Self-edit pipeline: agents edit self-owned code through review gates.

The self-modification contract (operator decisions, locked 2026-10-18):
  * Editable surface: everything under world/ EXCEPT serve.py and
    BUSINESS-PROCESSES.md. serve.py is the HTTP kernel and the enforcement
    surface itself -- self-edits to it would be self-reviewing. sim.py is
    fully editable (operator decision).
  * Reload binding contract (why consumer scanning exists): after a file is
    replaced and its module reloaded, only code that reaches that module
    through its OWN globals or module attributes at CALL time sees new
    objects. config.POLICY_BINDINGS and serve's jev.py re-exports already
    follow that pattern; jev reloads are followed by a serve-name refresh,
    and sublow reloads are followed by reloading its one importer
    (jev_grants.py). Any OTHER module holding a top-level `from <target>
    import ...` binding would go stale, so a target with outside importers
    beyond the registered cases is REFUSED in v1.
  * config.py is NOT protected but is refused in v1 for the same reason: it
    has direct importers (content.py, sublow.py, serve.py) and a topological
    reload is not yet built. The operator edits config.py directly; Phase 2
    opens it.
  * Review bar: one JEV decisions review (the quorum sampler), then a
    subprocess quarantine validation (compileall over the whole world tree
    with the candidate in place + flake8 on the changed file + an import
    smoke of the changed module). No human approval step in v1; every gate
    outcome is logged in the self_edit_log table for audit and revert.
  * Apply window: DORMANT ONLY. apply_pass() returns without doing anything
    unless serve._dormant() is true; a single always-running serve loop
    (_dormancy_window_loop) owns applies on every poll while dormant. The
    tank reaches dormancy through TWO paths: the idle watcher (operator's
    --max-idle-minutes) and approved one-shot DORMANCY WINDOWS (below).
  * Change-request process for scheduled applies (operator decision,
    2026-10-18): an agent may request a one-shot dormancy window -- a date +
    start time + duration -- to sleep the tank and apply armed edits. The
    chain: (1) conflict scan against the standing-task schedule and every
    other pending/approved/fired window; any overlap REFUSES with the
    colliding items listed; (2) the agent's supervisor (serve._supervisor_of)
    passes JEV judgment on the request; (3) the request escalates as a
    floor-1.0 kind ('dormancy window request' in jev.Escalation floors) whose
    email/telegram link is the PLAYER's mandatory final signature. A window
    applies whatever armed edits exist when it fires (each edit already
    passed its own quorum + validator), then consumes itself; the tank
    normally re-awakes on the next request, exactly like the idle path.
  * CHANGE WINDOW semantics (operator decision, 2026-10-18): edits landed in
    a window are TESTED after they apply (the repo's own gate script runs
    inside the window). If tests fail, an in-window remediation session
    opens: every proposer is mailed the failing output and, until the
    window's end, may submit corrective fixes and rerun the checks through
    remediation traffic that does NOT wake the tank (selfedit_* request
    paths only). If the failures are not resolved BY THE END of the window,
    the window ROLLS BACK automatically: each candidate that did not
    survive is copied to world/.selfedit_failed/<window>/<new-name>, every
    edit applied in that window is reverted to its pre-edit bytes, modules
    re-load, and each responsible agent is mailed to fix the issues
    OFF-window and schedule a fresh window. Applies outside any window (the
    idle path) get the same post-apply tests; with no window to remediate
    in, a failure there rolls back immediately and mails the agents.
  * Failure posture: every gate fails closed. A refused, rejected, or
    unverifiable proposal never touches the file tree.
"""

import hashlib
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time

# NEVER self-editable, by operator decision (2026-10-18): the HTTP kernel
# and the enforcement documents themselves.
PROTECTED_BASENAMES = ('serve.py', 'BUSINESS-PROCESSES.md')

# Loading contract, target module -> importer basenames that ARE tolerated,
# because apply-time refresh handles their specific binding shape:
#   jev    -> serve's `from jev import (...)` names are refreshed explicitly.
#   sublow -> jev_grants.py imports sublow at top level and is reloaded after.
# Everything else tolerates ZERO outside importers in v1.
TOLERATED_IMPORTERS = {
    'jev': {'serve.py'},
    'sublow': {'jev_grants.py'},
}
RELOAD_IMPORTERS_AFTER = {
    'sublow': ('jev_grants',),
}

MAX_TARGET_FILE_BYTES = 4 * 1024 * 1024
MAX_FIND_LEN = 80_000
MAX_REPLACE_LEN = 200_000
MAX_SUMMARY_LEN = 500

# JEV review contract for code edits. The choice space maps onto the existing
# deciders: approve / reject / escalate / None(fail-closed).
_REVIEW_INSTRUCTIONS = (
    'You are reviewing a proposed code edit to an AI think tank codebase. '
    'The proposing agent must state WHAT it is changing and WHY this improves '
    'the tank. Reject when: the summary is missing, vague, or conflicts with '
    'the edit; the replacement looks destructive (wide deletions, removing '
    'fail-closed checks, hardcoding credentials, disabling gates, raising '
    'the proposing agent\'s own privileges); or the change exceeds what the '
    'summary describes. Approve only when purpose and scope are coherent '
    'and safe. Escalate when you are unsure.'
)
_REVIEW_CRITERIA = (
    'A short summary describing intent (given).\n'
    'The exact find/replace splice being made (given).\n'
    'Reject destructive, self-privileging, or unexplained edits.\n'
    'serve.py and BUSINESS-PROCESSES.md are never editable (already '
    'enforced upstream -- treat violations as reject).'
)


def _s():
    import serve
    return serve


def _world():
    return _s().ROOT


def _repo_root():
    return os.path.dirname(_world())


def is_protected(resolved_path):
    """True for the paths self-edits may never touch: serve.py (the actual
    kernel file) and every BUSINESS-PROCESSES.md enforcement document in
    the repo."""
    name = os.path.basename(resolved_path)
    if name == 'serve.py':
        return os.path.realpath(resolved_path) == os.path.realpath(
            os.path.join(_world(), 'serve.py'))
    return name in PROTECTED_BASENAMES


def _resolve_path(path):
    """Resolve a proposal's target to (realpath, display_path) where display
    is repo-root-relative (e.g. 'world/sim.py'). Accepted forms: a bare
    world-module file name ('sim.py'), a repo-root-relative path
    ('world/sim.py'), or an absolute path inside the repo. Everything else
    -- and every nonexistent file or symlink escape -- errors out."""
    raw = (path or '').strip().replace('\\', '/')
    if not raw:
        return None, 'path is required'
    if os.path.isabs(raw):
        real = os.path.realpath(raw)
    elif raw.startswith('world/'):
        real = os.path.realpath(os.path.join(_repo_root(), raw))
    elif '/' not in raw and raw.endswith('.py'):
        real = os.path.realpath(os.path.join(_world(), raw))
    else:
        return None, ('path must be a world module file (sim.py), a '
                      "repo-root relative path (world/sim.py), or an "
                      'absolute path inside this repository')
    if not os.path.isfile(real):
        return None, 'no such file'
    root_real = os.path.realpath(_repo_root())
    if os.path.commonpath([root_real, real]) != root_real:
        return None, 'path escapes the repository'
    return real, os.path.relpath(real, root_real)


def _importers_of(modname):
    """World modules whose TOP-LEVEL code does `import <modname>` or
    `from <modname> import ...` -- bindings that would go stale across a
    reload. Function-local `import <modname>` does not count (call-time
    resolution is reload-safe); only column-0 statements are read."""
    importers = []
    for fn in sorted(os.listdir(_world())):
        if not fn.endswith('.py') or fn == f'{modname}.py':
            continue
        try:
            with open(os.path.join(_world(), fn), 'r', encoding='utf-8') as fh:
                src = fh.read()
        except OSError:
            continue
        for line in src.splitlines():
            if not line or line[0] in (' ', '\t', '#'):
                continue
            stripped = line.strip()
            if stripped.startswith('import '):
                if stripped.split()[1].split('.')[0] == modname:
                    importers.append(fn)
                    break
            elif stripped.startswith('from '):
                module = stripped.split()[1].split('.')[0]
                if module == modname:
                    importers.append(fn)
                    break
    return importers


def _ensure_table(conn):
    conn.execute(
        'CREATE TABLE IF NOT EXISTS self_edit_log ('
        'id TEXT PRIMARY KEY, created REAL NOT NULL, agent TEXT NOT NULL, '
        'path TEXT NOT NULL, summary TEXT NOT NULL, '
        'sha_before TEXT NOT NULL, sha_after TEXT NOT NULL, '
        'status TEXT NOT NULL, note TEXT, jev_confidence REAL, jev_cost REAL, '
        'validator_note TEXT, armed_at REAL, applied_at REAL, reverted_at REAL)')
    try:  # change-window linkage landed after the v1 table
        conn.execute('ALTER TABLE self_edit_log ADD COLUMN window_id TEXT')
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute('CREATE INDEX IF NOT EXISTS idx_self_edit_status '
                 'ON self_edit_log (status, created)')


def _record(edit_id, created, agent, display, summary, sha_before, sha_after,
            status):
    with _s()._db() as conn:
        _ensure_table(conn)
        conn.execute(
            'INSERT INTO self_edit_log (id, created, agent, path, summary, '
            'sha_before, sha_after, status) VALUES (?,?,?,?,?,?,?,?)',
            (edit_id, created, agent, display, summary, sha_before, sha_after,
             status))


def _update(edit_id, **fields):
    if not fields:
        return
    cols = ', '.join(f'{k} = ?' for k in fields)
    with _s()._db() as conn:
        _ensure_table(conn)
        conn.execute(f'UPDATE self_edit_log SET {cols} WHERE id = ?',
                     (*fields.values(), edit_id))


def _get_row(edit_id):
    with _s()._db() as conn:
        _ensure_table(conn)
        return conn.execute(
            'SELECT path, status FROM self_edit_log WHERE id = ?',
            (edit_id,)).fetchone()


def _staging_candidate(edit_id):
    return os.path.join(_world(), '.selfedit_staging', f'{edit_id}.cand')


def _compile_error(text):
    try:
        compile(text, '<selfedit-candidate>', 'exec')
    except SyntaxError as e:
        return str(e)
    return None


def propose(agent_id, path, find, replace, summary):
    """Run the full gate chain for one splice edit. Returns (ok, payload);
    ok=False means nothing was armed and payload carries an 'error' for the
    proposing agent."""
    agent_id = (agent_id or '').strip() or 'unknown'
    summary = (summary or '').strip()[:MAX_SUMMARY_LEN]
    find = find if isinstance(find, str) else ''
    replace = replace if isinstance(replace, str) else ''
    if not find or len(find) > MAX_FIND_LEN:
        return False, {'error': f'find text is required (max {MAX_FIND_LEN} chars)'}
    if len(replace) > MAX_REPLACE_LEN:
        return False, {'error': f'replacement too long (max {MAX_REPLACE_LEN} chars)'}
    if find == replace:
        return False, {'error': 'this edit changes nothing'}
    if not summary:
        return False, {'error': 'a summary is required -- say WHAT you are '
                                'changing and WHY it helps the tank'}
    resolved, display = _resolve_path(path)
    if resolved is None:
        return False, {'error': display}
    if is_protected(resolved):
        return False, {'error': f'{display} is PROTECTED: self-edits may '
                                'never touch serve.py or '
                                'BUSINESS-PROCESSES.md, even with approval'}
    try:
        with open(resolved, 'r', encoding='utf-8') as fh:
            original = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        return False, {'error': f'cannot read target: {e}'}
    if len(original.encode('utf-8')) > MAX_TARGET_FILE_BYTES:
        return False, {'error': 'target file too large for a self-edit'}
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
        return False, {'error': f'candidate does not compile: {syntax_err} -- '
                                'fix the splice before proposing'}
    modname = os.path.splitext(os.path.basename(resolved))[0]
    # No config exception here: config.py has top-level importers
    # (content.py, sublow.py, serve.py) with no tolerated/refresh contract, so
    # the scan below refuses it NATURALLY in v1 -- exactly the docstring's
    # contract (Phase 2 topological reload opens it).
    importers = _importers_of(modname)
    tolerated = TOLERATED_IMPORTERS.get(modname, set())
    hostile = [i for i in importers if i not in tolerated]
    if hostile:
        return False, {'error': f'{display} is top-level imported by '
                                f'{", ".join(hostile)}; those bindings '
                                'would go stale after the post-apply '
                                'reload. Propose edits to the consumer '
                                'module instead (v1 binding contract).'}
    sha_before = hashlib.sha256(original.encode('utf-8')).hexdigest()
    sha_after = hashlib.sha256(candidate.encode('utf-8')).hexdigest()
    edit_id = f'ed-{secrets.token_hex(6)}'
    _record(edit_id, time.time(), agent_id, display, summary, sha_before,
            sha_after, 'proposed')
    _s().log_action(agent_id, 'self_edit_proposed',
                    {'id': edit_id, 'path': display,
                     'delta_bytes': len(candidate) - len(original)},
                    authorized=True)
    ok, payload = _review(edit_id, display, find, replace, summary)
    if not ok:
        return False, payload
    ok, payload = _validate(edit_id, resolved, candidate)
    if not ok:
        return False, payload
    os.makedirs(os.path.dirname(_staging_candidate(edit_id)), exist_ok=True)
    with open(_staging_candidate(edit_id), 'w', encoding='utf-8') as fh:
        fh.write(candidate)
    _update(edit_id, status='armed', armed_at=time.time())
    _s().log_action(edit_id, 'self_edit_armed',
                    {'path': display, 'apply': 'dormant-only via idle loop'},
                    authorized=True)
    return True, {'id': edit_id, 'status': 'armed',
                  'message': 'edit armed -- it applies the next time the tank '
                             'goes dormant; check self_edit_status'}


def _review(edit_id, display, find, replace, summary):
    """One quorum-sampled JEV decisions review; every outcome fails closed
    unless it is a confident enough approval."""
    serve = _s()
    question = (f'Proposed self-edit of {display} by agent.\n'
                f'Summary: {summary}\n'
                f'FIND (exact splice anchor):\n{find[:4000]}\n'
                f'REPLACE WITH:\n{replace[:8000]}')
    try:
        decision, confidence, cost = serve._jev_quorum_choice_sync(
            _REVIEW_INSTRUCTIONS, _REVIEW_CRITERIA + '\n\n' + question)
    except Exception as e:  # transport/shape failure is not consent
        decision, confidence, cost = None, 1.0, 0.0
        serve.log_action(edit_id, 'self_edit_review_failed',
                         {'error': str(e)[:200]}, authorized=False)
    _update(edit_id, jev_confidence=round(float(confidence or 0.0), 4),
            jev_cost=round(float(cost or 0.0), 6),
            note=f'jev: {decision!r} @ {round(float(confidence or 0.0), 2)}')
    if decision == 'approve':
        return True, {'review': 'approved', 'confidence': confidence}
    _update(edit_id, status='rejected' if decision == 'reject' else 'refused')
    serve.log_action(edit_id,
                     'self_edit_rejected' if decision == 'reject'
                     else 'self_edit_refused',
                     {'decision': decision, 'confidence': confidence},
                     authorized=True)
    return False, {
        'error': (f'the edits reviewer said {decision!r} at '
                  f'{round(float(confidence or 0.0), 2)} confidence -- '
                  'nothing was changed. Re-propose with a clearer summary, '
                  'a smaller edit, or a rewritten splice.'),
        'status': 'rejected' if decision == 'reject' else 'refused'}


def _validate(edit_id, resolved, candidate):
    """Subprocess quarantine: copy the whole world tree, drop the candidate
    in, compileall, flake8 the changed file, import-smoke the changed
    module. Any failure refuses the edit."""
    serve = _s()
    staging_root = os.path.join(_world(), '.selfedit_staging')
    os.makedirs(staging_root, exist_ok=True)
    tmp = os.path.join(staging_root, f'q-{secrets.token_hex(6)}')
    checks, errors = [], []
    try:
        shutil.copytree(_world(), tmp,
                        ignore=shutil.ignore_patterns(
                            '__pycache__', '.selfedit_staging',
                            '.selfedit_backups'),
                        dirs_exist_ok=True)
        with open(os.path.join(tmp, os.path.basename(resolved)), 'w',
                  encoding='utf-8') as fh:
            fh.write(candidate)
        py = sys.executable
        step = subprocess.run([py, '-m', 'compileall', '-q', tmp],
                              capture_output=True, text=True, timeout=180)
        if step.returncode != 0:
            errors.append('compileall: ' + (step.stdout or step.stderr)[-400:])
        else:
            checks.append('compileall ok')
        flake = subprocess.run(
            [py, '-m', 'flake8',
             os.path.join(tmp, os.path.basename(resolved)),
             '--max-line-length=100'],
            capture_output=True, text=True, timeout=180)
        if flake.returncode != 0:
            errors.append('flake8: ' + (flake.stdout or flake.stderr)[-400:])
        else:
            checks.append('flake8 ok')
        modname = os.path.splitext(os.path.basename(resolved))[0]
        smoke = subprocess.run(
            [py, '-c', f'import sys; sys.path.insert(0, {tmp!r}); '
                        f'import {modname}'],
            capture_output=True, text=True, timeout=180, cwd=tmp,
            env={**os.environ, 'PYTHONPATH': tmp})
        if smoke.returncode != 0:
            errors.append('import smoke: ' + (smoke.stderr or smoke.stdout)[-600:])
        else:
            checks.append('import smoke ok')
    except subprocess.TimeoutExpired:
        errors.append('validator timed out')
    except Exception as e:
        errors.append(f'validator failure: {e}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    note = (' | '.join(checks) if not errors
            else 'FAIL: ' + ' | '.join(errors))
    _update(edit_id, validator_note=note[:400])
    if errors:
        serve.log_action(edit_id, 'self_edit_validator_rejected',
                         {'note': note[:200]}, authorized=True)
        return False, {'error': 'quarantine validation failed -- the '
                                'candidate never left staging',
                       'detail': note[:400], 'status': 'validator-rejected'}
    return True, {'validator': note[:400]}


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
    with serve._db() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT id, path, sha_before FROM self_edit_log "
            "WHERE status = 'armed' ORDER BY created").fetchall()
    if not rows:
        return 0
    window_id = _active_window_id()
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
    if not landed:
        return 0
    ok, tail = _run_post_apply_tests()
    serve.log_action('selfedit', 'self_edit_post_apply_tests',
                     {'result': 'pass' if ok else 'fail',
                      'edits': applied, 'window': window_id,
                      'tail': tail[:POST_APPLY_TAIL_LEN]}, authorized=True)
    if ok:
        return applied
    if window_id:
        _mark_remediation(window_id, landed, tail)
        return applied
    _rollback_rows(landed,
                   'post-apply tests failed and no change window was open',
                   'no-window')
    return applied


def _run_post_apply_tests():
    """Test the edited tree after applying: run the repo's own gate script
    from the repo root and return (ok, tail). The command is a module
    constant so tests can substitute a fast fake."""
    serve = _s()
    repo_root = os.path.dirname(os.path.abspath(_world()))
    try:
        done = subprocess.run(
            POST_APPLY_TEST_CMD, cwd=repo_root,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=POST_APPLY_TEST_TIMEOUT_S)
        raw = (done.stdout or b'').decode('utf-8', errors='replace')
        ok = done.returncode == 0
    except subprocess.TimeoutExpired:
        raw = 'post-apply test run timed out'
        ok = False
    except Exception as exc:  # a broken test harness fails closed
        raw = f'post-apply test run could not start: {exc}'
        ok = False
    tail = raw.strip()[-POST_APPLY_TAIL_LEN:]
    if ok:
        return True, tail
    serve.log_action('selfedit', 'self_edit_post_apply_tests_failed',
                     {'tail': tail[:POST_APPLY_TAIL_LEN]}, authorized=True)
    return False, tail


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
                    f'clean; nothing else to do.'), about=f'selfedit:{row_id}')
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
    if is_protected(resolved):
        return False, {'error': f'{display} is PROTECTED: self-edits may '
                                'never touch serve.py or '
                                'BUSINESS-PROCESSES.md, even with approval'}
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
            'jev_confidence, validator_note, applied_at, window_id '
            'FROM self_edit_log '
            'ORDER BY created DESC LIMIT ?', (int(limit),)).fetchall()
    return [{'id': r[0], 'created': r[1], 'agent': r[2], 'path': r[3],
             'summary': r[4], 'status': r[5], 'note': r[6],
             'jevConfidence': r[7], 'validator': r[8], 'appliedAt': r[9],
             'windowId': r[10]}
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
WINDOW_MIN_LEAD_S = 5 * 60
WINDOW_MAX_LEAD_S = 30 * 24 * 60 * 60
MAX_WINDOW_REASON_LEN = 500

# The post-apply gate: the change window tests the LIVE tree after edits
# land, using the repo's own test gate (the same bar every contributors'
# change must meet). A long window is not needed for tests to finish, but a
# window shorter than the test script would roll back half-seen work -- keep
# the default 15 minutes and the gate capped so a stuck runner cannot eat a
# whole window.
POST_APPLY_TEST_CMD = ['bash', 'tests/run_all.sh']
POST_APPLY_TEST_TIMEOUT_S = 900
POST_APPLY_TAIL_LEN = 2000

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
_WINDOW_CRITERIA = (
    'Date, start time and duration for the sleep window (given).\n'
    'A stated maintenance reason (given).\n'
    'Reject vague reasons, suspicious slots (prime working hours without '
    'justification), and oversized durations.\n'
    'The player approves or denies the final link separately.'
)


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


def _occupied_window_conflicts(start_ts, end_ts):
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
             'agent': r[4]} for r in rows]


def _standing_task_conflicts(start_ts, end_ts):
    """Standing-task schedule windows covering any minute of the slot.

    Mirrors the declaration -> schedule.StandingTask conversion the sim
    sweep uses, and reads the scheduler's live runtime state so hours-mode
    anchors are honored instead of treated as always-on. One sample per
    minute: a 4-hour window is 240 samples of a pure predicate."""
    import datetime as _dt
    import schedule as _schedule
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


def _window_conflicts(start_ts, end_ts):
    return (_standing_task_conflicts(start_ts, end_ts)
            + _occupied_window_conflicts(start_ts, end_ts))


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


def request_window(agent_id, date, start, duration_min, reason):
    """Run the change-request chain for one dormancy slot.

    Returns (ok, payload); ok=False carries the refusal (with 'conflicts'
    when the slot was blocked by the schedule). ok=True means the supervisor
    judgment passed and the player-only escalation link is out."""
    serve = _s()
    agent_id = (agent_id or '').strip() or 'unknown'
    reason = (reason or '').strip()[:MAX_WINDOW_REASON_LEN]
    if not reason:
        return False, {'error': 'a reason is required -- say WHY the tank '
                                'should sleep then (emergency fix, risky '
                                'reload, quiet hours maintenance)'}
    try:
        duration_min = int(duration_min or WINDOW_DEFAULT_DURATION_MIN)
    except (TypeError, ValueError):
        duration_min = WINDOW_DEFAULT_DURATION_MIN
    duration_min = max(WINDOW_MIN_DURATION_MIN,
                       min(WINDOW_MAX_DURATION_MIN, duration_min))
    now = time.time()
    start_ts, _day_end, err = _parse_window_target(date, start, now)
    if err:
        return False, {'error': err}
    if start_ts < now + WINDOW_MIN_LEAD_S:
        return False, {'error': 'the window must start at least 5 minutes '
                                'from now -- pick a future slot'}
    if start_ts > now + WINDOW_MAX_LEAD_S:
        return False, {'error': 'the window may not be more than 30 days out'}
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
    question = (f'Agent {agent_id} requests a dormancy window to apply '
                f'reviewed code self-edits:\n'
                f'Date: {date}  Start: {start} (tank local time)  '
                f'Duration: {duration_min} min\n'
                f'Reason: {reason}\n\n'
                f'Approving sets the tank dormant for that slot so armed '
                f'self-edits apply. The supervisor pre-cleared this request '
                f'through the JEV quorum.')
    decision, confidence, cost = serve._jev_quorum_choice_sync(
        _WINDOW_INSTRUCTIONS, _WINDOW_CRITERIA + '\n\n' + question)
    if decision != 'approve':
        serve.log_action(agent_id, 'dormancy_window_refused',
                         {'decision': decision, 'confidence': confidence},
                         authorized=True)
        return False, {'error': f'the supervisor review said {decision!r} '
                                f'at {round(float(confidence or 0.0), 2)} '
                                'confidence -- nothing was scheduled',
                       'status': 'refused'}
    window_id = f'win-{secrets.token_hex(5)}'
    esc_id = serve.create_escalation(_WINDOW_JEV_KIND, question,
                                     on_approve_note=window_id)
    _window_record(window_id, now, agent_id, date, start, start_ts, end_ts,
                   duration_min, reason, 'pending')
    _update_window(window_id, escalation_id=esc_id)
    serve.log_action(window_id, 'dormancy_window_requested',
                     {'agent': agent_id, 'date': date, 'start': start,
                      'durationMin': duration_min, 'escalationId': esc_id,
                      'supervisorDecision': 'approved'},
                     authorized=True)
    return True, {'id': window_id, 'status': 'pending', 'escalationId': esc_id,
                  'durationMin': duration_min,
                  'message': 'window requested -- the supervisor review '
                             'approved it; it now waits on the player '
                             'approval link and then applies on schedule. '
                             'Check dormancy_window_status.'}


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
    _update_window(window_id, status='approved' if approved else 'refused',
                   note=f'player {"approved" if approved else "denied"}')
    serve.log_action(window_id, 'dormancy_window_resolved',
                     {'by': 'player', 'approved': approved},
                     authorized=True)
    return True


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
                                'candidates saved and edits reverted')
            serve.log_action(row_id, 'dormancy_window_rolled_back',
                             {'edits': failing}, authorized=True)
        else:
            _update_window(row_id, status='done')
            serve.log_action(row_id, 'dormancy_window_done', {},
                             authorized=True)
    fired = []
    for (row_id,) in due:
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
    serve.log_action(window_id, 'dormancy_window_cancelled',
                     {'by': by}, authorized=True)
    return True, {'ok': True, 'status': 'cancelled'}


def windows_snapshot(limit=25):
    """Bounded, newest-first view of the dormancy-window ledger."""
    with _s()._db() as conn:
        _ensure_windows_table(conn)
        rows = conn.execute(
            'SELECT id, created, agent, date, start, duration_min, reason, '
            'status, note, escalation_id, fired_at FROM dormancy_windows '
            'ORDER BY created DESC LIMIT ?', (int(limit),)).fetchall()
    return [{'id': r[0], 'created': r[1], 'agent': r[2], 'date': r[3],
             'start': r[4], 'durationMin': r[5], 'reason': r[6],
             'status': r[7], 'note': r[8], 'escalationId': r[9],
             'firedAt': r[10]} for r in rows]