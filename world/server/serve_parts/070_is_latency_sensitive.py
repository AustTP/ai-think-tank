

def _is_latency_sensitive(purpose, task_type=None):
    """True for work where freshness beats depth: pulse triage, live alerts,
    monitoring, and short status checks."""
    task = str(task_type or '').lower()
    text = str(purpose or '').lower()
    if task in ('pulse', 'triage', 'monitor', 'monitoring'):
        return True
    return any(token in text for token in ('pulse', 'triage', 'live alert',
                                           'monitor', 'freshness check'))


def _slug_for_band(band):
    if band in ('3d', '3d-modeling'):
        return _3d_tier_slug() or _coding_tier_slug() or _mid_tier_slug() or _low_tier_slug()
    if band == 'coding':
        return _coding_tier_slug() or _mid_tier_slug() or _low_tier_slug()
    if band == 'high':
        return _high_tier_slug() or _mid_tier_slug() or _low_tier_slug()
    if band == 'mid':
        return _mid_tier_slug() or _low_tier_slug()
    return _low_tier_slug() or _mid_tier_slug()


def _resolve_model_tier(purpose, task_type=None, allow_high=False, decider=None,
                        best_effort=False):
    """Pick which model tier a call deserves, under the JEV-gated escalation
    policy. Returns a resolved model slug (never None).

    `task_type` 'code'/'review'/'qa' -> deterministic CODING tier (no JEV).
    `allow_high` -> JEV may pick HIGH; otherwise the decision is low-or-mid.
    `purpose` -> a one-line description of what the call is for, so JEV's
    judgment is grounded in the actual work rather than a generic "should I
    spend more?" prompt.

    Fails CLOSED to low: a JEV outage, a non-binary answer, or a low-confidence
    escalation all keep the cheap tier."""
    # A best-effort call is a request for JEV to consider the high band, not
    # a bypass around it. The deterministic coding and latency lanes still
    # win below; every other best-effort band choice is JEV-gated.
    wants_best = bool(best_effort)
    if wants_best:
        allow_high = True
    route = policies.choose_route(purpose, task_type, allow_high, False)
    if not wants_best and policies.apply_enabled():
        band = route['band']
        if band == 'high' and _high_tier_budget_exceeded():
            band = 'mid'
        slug = _slug_for_band(band)
        if slug:
            return slug
    elif not wants_best:
        policies.record_shadow('routing', route,
                               {'purpose': (purpose or '')[:120],
                                'task_type': task_type})
    if task_type in ('3d', '3d-modeling'):
        return _3d_tier_slug() or _coding_tier_slug() or _mid_tier_slug() or _low_tier_slug()
    if task_type in ('code', 'review', 'qa'):
        return _coding_tier_slug() or _mid_tier_slug() or _low_tier_slug()
    if _is_latency_sensitive(purpose, task_type):
        # Pulse triage and live monitoring are latency work: use the cheap,
        # fast tier and never wait on a JEV escalation call.
        return _low_tier_slug() or _mid_tier_slug()
    decider = decider or _tier_gate_decider
    try:
        # The old prompt was the purpose line alone: JEV judged a one-liner and
        # came back under the 0.55 floor 97% of the time, so the gate failed
        # closed to the cheap tier almost always. Two additions: the task_type
        # axis (code/review/qa never even reach here, but research/agent
        # intent does) and an explicit calibration line naming what each band
        # is FOR, so borderline one-step work stops reading as "needs judgment".
        try:
            decision, confidence = decider(
                f'Pick the model tier this think tank call deserves. '
                f'Work type: {task_type or "unspecified"}. The work: {purpose or "routine task"}. '
                f'A short single-step reply, lookup, or extraction stays low even when '
                f'it matters; mid is for real synthesis or multi-step judgment; high is '
                f'only for one-shot planning where a mistake wastes downstream work.',
                _TIER_GATE_CRITERIA,
                kind='tier_gate',
            )
        except TypeError:
            # Injectable test stubs keep the older 2-arg shape; the real
            # decider swallows its own exceptions, so a TypeError escaping
            # here can only be the stub's arity.
            decision, confidence = decider(
                f'Pick the model tier this think tank call deserves. '
                f'Work type: {task_type or "unspecified"}. The work: {purpose or "routine task"}. '
                f'A short single-step reply, lookup, or extraction stays low even when '
                f'it matters; mid is for real synthesis or multi-step judgment; high is '
                f'only for one-shot planning where a mistake wastes downstream work.',
                _TIER_GATE_CRITERIA,
            )
    except Exception:
        decision, confidence = None, 0.0
    if decision == 'high' and allow_high and confidence >= TIER_GATE_MIN_CONFIDENCE:
        # High tier is expensive and designed for RARE use -- a dollar budget,
        # not just a confidence gate. If the month's high-tier allowance is
        # spent, fail CLOSED to mid rather than overrunning the expensive tier.
        if _high_tier_budget_exceeded():
            return _mid_tier_slug() or _low_tier_slug()
        return _high_tier_slug() or _mid_tier_slug() or _low_tier_slug()
    if decision == 'mid' and confidence >= TIER_GATE_MIN_CONFIDENCE:
        return _mid_tier_slug() or _low_tier_slug()
    # Everything else -- low decision, low confidence, outage, or a high pick
    # on a non-allow_high call -- stays on the cheap tier.
    return _low_tier_slug() or _mid_tier_slug()


# A chief of staff decision
# should not always have to wait for the human to tap the email link -- the
# senior-most director (a director no other director supervises) stands in,
# resolving pending escalations with the same real Jev judgment every other
# gate in this file uses. The human is still the ultimate authority: any
# escalation the user already resolved via the email link has status != pending
# and is skipped; and the director's own decision is recorded on the escalation
# record (who decided, what was asked) so the delegation is auditable, not
# invisible. Runs on its own cadence, server-side, like the health/mail loops.
DIRECTOR_APPROVAL_INTERVAL_S = 30

# Re-ask policy for the director's delegated approvals (added after the
# Escalation storm -- ~204k Jev calls in 24h, 161k of them
# "escalation_unsure" dead-ends): a pending escalation Jev can't resolve
# confidently is left for the human, but it must NOT be re-queried every
# tick forever. Back off between attempts, and stop entirely after a cap
# so an unresolved escalation stops costing Jev calls. Kinds whose per-kind
# floor is 1.0 ("never auto-approve") are skipped before any Jev call --
# they're human-only by design.
DIRECTOR_ESCALATION_REASK_COOLDOWN_S = 15 * 60
DIRECTOR_ESCALATION_MAX_ASKS = 6


SUPERVISOR_BUDGET_CAP_USD = 5.0


def supervisor_team_for(state, agent_id):
    """The team a supervisor leads, or None. Team id and directorId may differ
    in older records, so match either. The chief of staff leads no team here
    (he coordinates), which keeps team-scoped reads honest."""
    if not state or not agent_id:
        return None
    for team in (state.get('teams') or []):
        if team.get('directorId') == agent_id:
            return team
    return None


LIVE_READ_MAX_FILES = 200
LIVE_READ_MAX_LINES = 400
LIVE_READ_MAX_BYTES = 8_000_000
LIVE_READ_DENY = {'.git', '.secret_keys', 'node_modules', '__pycache__', '.venv', '.pytest_cache'}


def _safe_live_path(rel):
    """Resolve a repo-relative path for a read-only live inspection. Refuses
    traversal, symlink escapes, secrets, and VCS internals."""
    rel = (rel or '').strip().lstrip('/').replace('\\', '/')
    base = os.path.realpath(THINK_TANK_DIR)
    full = os.path.realpath(os.path.join(base, rel)) if rel else base
    if full != base and not full.startswith(base + os.sep):
        raise ValueError('path escapes the project')
    parts = [p for p in os.path.relpath(full, base).split(os.sep) if p not in ('', '.')]
    if any(p in LIVE_READ_DENY for p in parts):
        raise ValueError('that path is not readable')
    if parts and parts[-1] == '.env':
        raise ValueError('that path is not readable')
    return full


def _run_live_git(args):
    """Run one fixed, read-only git command in the project root. No shell, no
    user-supplied flags, bounded output. Returns {'text', 'truncated'} or
    {'error'}."""
    import subprocess
    try:
        result = subprocess.run(['git', '-C', THINK_TANK_DIR, *args],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return {'error': f'git is unavailable: {exc}'}
    if result.returncode != 0:
        return {'error': (result.stderr or 'git failed').strip()[:500]}
    text = result.stdout or ''
    return {'text': text[:20000], 'truncated': len(text) > 20000}


def _live_git_read(op, args):
    if op == 'git_status':
        out = _run_live_git(['status', '--short', '--untracked-files=normal'])
        if 'error' in out:
            return out
        lines = [line for line in out['text'].splitlines()
                 if '.env' not in line and 'secret' not in line.lower()]
        return {'op': op, 'entries': lines[:200], 'clean': not lines,
                'truncated': out['truncated'] or len(lines) > 200}
    if op == 'git_diff':
        try:
            full = _safe_live_path(args.get('path') or '')
        except ValueError as exc:
            return {'error': str(exc)}
        rel = os.path.relpath(full, THINK_TANK_DIR)
        if rel == '.' or not os.path.exists(full):
            return {'error': 'a changed file or directory path is required'}
        out = _run_live_git(['diff', '--no-ext-diff', '--unified=3', '--', rel])
        if 'error' in out:
            return out
        return {'op': op, 'path': rel, **out}
    limit = max(1, min(int(args.get('limit') or 20), 50))
    out = _run_live_git(['log', '-n', str(limit), '--oneline', '--decorate'])
    if 'error' in out:
        return out
    return {'op': op, **out}


def _live_task_trace(state, agent_id, args):
    """Read-only, team-scoped trace for one card: state, children, queue, and
    matching action-log rows. A director sees only cards owned by their team;
    the chief of staff sees any card."""
    task_id = str(args.get('taskId') or '').strip()
    if not task_id or len(task_id) > 80:
        return {'error': 'taskId is required'}
    task = (state.get('tasks') or {}).get(task_id)
    if not task:
        return {'error': f'unknown task {task_id!r}'}
    is_admin = _is_admin(state, agent_id)
    team = supervisor_team_for(state, agent_id)
    if not is_admin:
        direct = set(_direct_reports(state, (team or {}).get('directorId') or ''))
        if not team or (task.get('teamId') != team.get('id')
                        and task.get('assignedTo') not in direct):
            return {'error': 'this card is outside your team'}
    fields = ('id', 'title', 'status', 'room', 'assignedTo', 'teamId',
              'reviewOf', 'rootStoryId', 'deliveryState', 'failNote', 'note',
              'nextAction', 'budgetUsd', 'storySpendUsd', 'libraryPath',
              'reportPath', 'issueKey', 'requestId')
    summary = {k: task.get(k) for k in fields if k in task}
    if task.get('acceptanceCriteria'):
        summary['acceptanceCriteria'] = str(task['acceptanceCriteria'])[:2000]
    if task.get('instructions'):
        summary['instructions'] = str(task['instructions'])[:2000]
    children = [t.get('id') for t in (state.get('tasks') or {}).values()
                if t.get('reviewOf') == task_id or t.get('investigationOf') == task_id]
    queued = [q.get('id') or q.get('title') for q in (state.get('workQueue') or [])
              if q.get('reviewOf') == task_id or q.get('investigationOf') == task_id]
    limit = max(1, min(int(args.get('limit') or 40), 100))
    rows = []
    with _db() as conn:
        rows = conn.execute(
            'SELECT ts, agent_id, action, details FROM action_log '
            'WHERE details LIKE ? ORDER BY id DESC LIMIT ?',
            (f'%{task_id}%', limit)).fetchall()
    return {'task': summary, 'children': children, 'queued': queued,
            'actionLog': [{'at': r[0], 'agent': r[1], 'action': r[2],
                           'details': (r[3] or '')[:500]} for r in rows]}


def _live_find_artifact(state, args):
    """Chief-of-staff recovery lookup: search the Library, archive, DB backups,
    and archived task records for a title/filename fragment. Read-only."""
    if not _is_admin(state, args.get('agentId')):
        return {'error': 'only the chief of staff may search recovery stores'}
    query = str(args.get('query') or '').strip()
    if len(query) < 2 or len(query) > 120:
        return {'error': 'query must be 2-120 characters'}
    needle = query.lower()
    roots = [os.path.join(THINK_TANK_DIR, 'library'),
             os.path.join(THINK_TANK_DIR, 'archive'),
             os.path.join(THINK_TANK_DIR, 'think-tank-db-backups')]
    files = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for base, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in LIVE_READ_DENY and not d.startswith('.')]
            for name in names:
                path = os.path.join(base, name)
                if os.path.islink(path) or os.path.getsize(path) > LIVE_READ_MAX_BYTES:
                    continue
                matched = needle in name.lower()
                if not matched:
                    try:
                        with open(path, encoding='utf-8', errors='replace') as stream:
                            matched = needle in stream.read(200_000).lower()
                    except OSError:
                        continue
                if matched:
                    files.append({'path': os.path.relpath(path, THINK_TANK_DIR),
                                  'bytes': os.path.getsize(path),
                                  'modified': os.path.getmtime(path)})
                    if len(files) >= 25:
                        break
            if len(files) >= 25:
                break
    archived = []
    with _db() as conn:
        for tid, blob in conn.execute('SELECT id, blob FROM task_archive'):
            if needle in (blob or '').lower():
                try:
                    task = json.loads(blob)
                except (TypeError, ValueError):
                    task = {}
                archived.append({'id': tid, 'title': task.get('title'),
                                 'status': task.get('status'),
                                 'note': str(task.get('note') or '')[:300]})
                if len(archived) >= 15:
                    break
    return {'query': query, 'files': files, 'archivedTasks': archived}


def supervisor_live_read(agent_id, args):
    """Read-only live inspection for a director: list, read, grep the project,
    or read the team's recent action log. Never writes, never leaves an
    allowlisted project path, and never exposes secrets or .git."""
    state = get_state_from_db() or {}
    if not _is_director_or_admin(state, agent_id):
        return {'error': 'only a director or the chief of staff may read the live project'}
    op = (args.get('op') or 'ls').strip().lower()
    if op in ('git_status', 'git_diff', 'git_log'):
        return _live_git_read(op, args)
    if op == 'task_trace':
        return _live_task_trace(state, agent_id, args)
    if op == 'find_artifact':
        args = dict(args or {})
        args['agentId'] = agent_id
        return _live_find_artifact(state, args)
    if op == 'ls':
        try:
            full = _safe_live_path(args.get('path') or '')
        except ValueError as exc:
            return {'error': str(exc)}
        if os.path.isfile(full):
            entries = [os.path.basename(full)]
        else:
            entries = [name for name in sorted(os.listdir(full))
                       if name not in LIVE_READ_DENY and name != '.env'][:LIVE_READ_MAX_FILES]
        return {'path': os.path.relpath(full, THINK_TANK_DIR), 'entries': entries}
    if op == 'read':
        try:
            full = _safe_live_path(args.get('path') or '')
        except ValueError as exc:
            return {'error': str(exc)}
        if not os.path.isfile(full):
            return {'error': 'not a file'}
        if os.path.getsize(full) > LIVE_READ_MAX_BYTES:
            return {'error': 'file is too large to read here'}
        start = max(1, int(args.get('start') or 1))
        end = min(start + LIVE_READ_MAX_LINES - 1, start + LIVE_READ_MAX_LINES - 1)
        if args.get('end'):
            end = min(int(args['end']), start + LIVE_READ_MAX_LINES - 1)
        with open(full, encoding='utf-8', errors='replace') as stream:
            lines = stream.readlines()
        chunk = lines[start - 1:end]
        return {'path': os.path.relpath(full, THINK_TANK_DIR), 'start': start,
                'end': start + len(chunk) - 1, 'totalLines': len(lines),
                'text': ''.join(chunk)}
    if op == 'grep':
        pattern = (args.get('pattern') or '').strip()
        if not pattern or len(pattern) > 120:
            return {'error': 'a pattern (max 120 chars) is required'}
        try:
            root = _safe_live_path(args.get('path') or 'world')
        except ValueError as exc:
            return {'error': str(exc)}
        want = pattern.lower()
        matches = []
        files_scanned = 0
        for base_dir, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in LIVE_READ_DENY]
            for name in files:
                path = os.path.join(base_dir, name)
                if os.path.islink(path) or os.path.getsize(path) > LIVE_READ_MAX_BYTES:
                    continue
                files_scanned += 1
                try:
                    with open(path, encoding='utf-8', errors='replace') as stream:
                        for lineno, line in enumerate(stream, 1):
                            if want in line.lower():
                                matches.append(f"{os.path.relpath(path, THINK_TANK_DIR)}:{lineno}: {line.strip()[:200]}")
                                if len(matches) >= 50:
                                    return {'pattern': pattern, 'filesScanned': files_scanned,
                                            'matches': matches, 'truncated': True}
                except OSError:
                    continue
        return {'pattern': pattern, 'filesScanned': files_scanned,
                'matches': matches, 'truncated': False}
    if op in ('team_log', 'log'):
        team = supervisor_team_for(state, agent_id)
        if not team and not _is_admin(state, agent_id):
            return {'error': 'you do not supervise a team'}
        director = (team or {}).get('directorId') or agent_id
        team_ids = set(_direct_reports(state, director)) if team else set()
        team_ids.add(director)
        limit = min(int(args.get('limit') or 40), 100)
        rows = []
        if team_ids:
            placeholders = ','.join('?' for _ in team_ids)
            with _db() as conn:
                rows = conn.execute(
                    f'SELECT ts, agent_id, action, details FROM action_log '
                    f'WHERE agent_id IN ({placeholders}) ORDER BY id DESC LIMIT ?',
                    (*sorted(team_ids), limit)).fetchall()
        return {'entries': [
            {'at': r[0], 'agent': r[1], 'action': r[2], 'details': (r[3] or '')[:400]}
            for r in rows]}
    return {'error': f'unknown op {op!r}; use ls, read, grep, team_log, git_status, '
                     'git_diff, git_log, task_trace, or find_artifact'}

