

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
        decision, confidence = decider(
            f'Pick the model tier this think tank call deserves. The work: {purpose or "routine task"}.',
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


DELEGATE_BUDGET_CAP_USD = 0.50
DELEGATE_MAX_OPEN = 3


def supervisor_delegate(agent_id, title, instructions, task_type='spike',
                        assignee=None, budget_usd=0.10, return_contract='', team=None,
                        parent_task_id=None):
    """Queue one scoped investigation for a worker on the caller's own team,
    with a written return contract. The delegation is async (a card): the
    supervisor reads the result on a later turn. Bounded to three open
    delegations and a small budget."""
    state = get_state_from_db() or {}
    if not _is_director_or_admin(state, agent_id):
        return False, {'error': 'only a director or the chief of staff may delegate'}
    is_admin = _is_admin(state, agent_id)
    wanted = str(team or '').strip()
    team_row = supervisor_team_for(state, agent_id)
    if wanted:
        team_row = next((t for t in (state.get('teams') or [])
                         if t.get('id') == wanted or t.get('directorId') == wanted), None)
    if not team_row:
        if is_admin:
            return False, {'error': 'the chief of staff must name a team to delegate to'}
        return False, {'error': 'you do not supervise a team'}
    title = (str(title or '')).strip()
    instructions = (str(instructions or '')).strip()
    if len(title) < 6 or len(instructions) < 12:
        return False, {'error': 'a title and instructions are required'}
    task_type = (task_type or 'spike').strip().lower()
    if task_type not in ('spike', 'research', 'code'):
        return False, {'error': 'taskType must be spike, research, or code'}
    team_id = (team_row or {}).get('id')
    members = list((team_row or {}).get('members') or [])
    if not assignee:
        try:
            import sim as _sim
            assignee = _sim.on_call_agent(state, team_id, None, int(time.time() * 1000))
        except Exception:
            assignee = members[0] if members else None
    if members and assignee not in members:
        return False, {'error': 'the assignee must be on your team'}
    try:
        budget = float(budget_usd)
    except (TypeError, ValueError):
        return False, {'error': 'budgetUsd must be a number'}
    if not (0 < budget <= DELEGATE_BUDGET_CAP_USD):
        return False, {'error': f'budgetUsd must be between 0 and {DELEGATE_BUDGET_CAP_USD:.2f}'}
    open_count = 0
    for item in (state.get('workQueue') or []):
        if item.get('teamId') == team_id and str(item.get('title') or '').startswith('[DELEGATE]'):
            open_count += 1
    for task in (state.get('tasks') or {}).values():
        if (task.get('teamId') == team_id and str(task.get('title') or '').startswith('[DELEGATE]')
                and task.get('status') not in ('done', 'cancelled', 'abandoned')):
            open_count += 1
    if open_count >= DELEGATE_MAX_OPEN:
        return False, {'error': f'you already have {open_count} open delegations; finish or cancel one first'}
    import sim as _sim
    contract = (str(return_contract or '')).strip() or (
        'Return: what you checked, what you found, the evidence, and the one next action.')
    _sim.queue_work(state, [{
        'title': f'[DELEGATE] {title}'[:140],
        'room': 'observatory' if task_type == 'research' else 'pressoffice',
        'taskType': task_type,
        'assignedTo': assignee,
        'teamId': team_id,
        'delegatedBy': agent_id,
        'parentTaskId': parent_task_id or None,
        'priority': 'high',
        'budgetUsd': budget,
        'sizeEstimate': 'S',
        'instructions': (f'Delegated by your supervisor ({agent_id}). Scope: {instructions} '
                         f'Return contract: {contract}'),
    }])
    save_state_to_db(state)
    log_action(agent_id, 'supervisor_delegate',
               {'title': title, 'assignee': assignee, 'taskType': task_type,
                'budgetUsd': budget, 'returnContract': contract[:300]}, authorized=True)
    return True, {'ok': True, 'title': title, 'assignee': assignee,
                  'taskType': task_type, 'budgetUsd': budget,
                  'parentTaskId': parent_task_id or None}


def supervisor_backlog_directive(agent_id, team, title, reason,
                                 task_type='task', feature=None):
    """Chief of staff only: instruct one team's director to put an immediate
    task in that team's backlog. Files a JIRA-style issue (the existing
    backlogRequests intake), marks the request HIGH priority so the groomed
    card is picked first, kicks the team's refinement so it is due on the very
    next pass, and mails the director the issue key. The director/scrum master
    still owns the accept/reject decision; this is a directive to act now, not
    a bypass of refinement."""
    state = _state_begin()
    try:
        if not state:
            return False, {'error': 'state unavailable'}
        if not _is_admin(state, agent_id):
            return False, {'error': 'only the chief of staff may direct the backlog'}
        wanted = str(team or '').strip()
        team_row = next((t for t in (state.get('teams') or [])
                         if t.get('id') == wanted or t.get('directorId') == wanted), None)
        if not team_row:
            return False, {'error': 'name a real team id or its director'}
        title = str(title or '').strip()
        reason = str(reason or '').strip()
        if len(title) < 6 or len(reason) < 12:
            return False, {'error': 'a title and a concrete reason are required'}
        issue_type = str(task_type or 'task').strip().lower()
        if issue_type not in ('task', 'story', 'spike', 'bug'):
            return False, {'error': 'taskType must be task, story, spike, or bug'}
        import sim as _sim
        team_id = team_row.get('id')
        issue = _sim.file_issue(
            state, team_id, issue_type, title,
            str(feature or 'chief-of-staff directive').strip(),
            reporter_id=agent_id,
            description=f'CHIEF OF STAFF DIRECTIVE (address immediately): {reason}',
            title=title)
        if not issue:
            return False, {'error': 'could not file the backlog issue'}
        req = next((r for r in (state.get('backlogRequests') or [])
                    if r.get('issueKey') == issue.get('key')), None)
        if req:
            req['priority'] = _sim.WORK_PRIORITY['high']
            req['origin'] = 'chief_of_staff'
            req['directiveBy'] = agent_id
            req['reason'] = ('CHIEF OF STAFF DIRECTIVE, address immediately: '
                             + reason)
        director_id = team_row.get('directorId') or team_row.get('id')
        director = (state.get('agents') or {}).get(director_id)
        if director is not None:
            _sim._append_mailbox(director, {
                'kind': 'chief_directive',
                'about': issue.get('key'),
                'title': f"Chief of staff directive: {title}",
                'text': (f'The chief of staff ({agent_id}) filed {issue.get("key")} '
                         f'for your team and wants it addressed immediately. '
                         f'Reason: {reason}'),
                'createdAt': int(time.time() * 1000),
            })
        save_state_to_db(state)
        log_action(agent_id, 'chief_backlog_directive', {
            'team': team_id, 'director': director_id,
            'issueKey': issue.get('key'), 'title': title[:120],
            'taskType': issue_type, 'priority': 'high',
            'reason': reason[:400],
        }, authorized=True)
        return True, {
            'ok': True, 'issueKey': issue.get('key'), 'team': team_id,
            'director': director_id, 'taskType': issue_type,
            'priority': 'high', 'refinementKicked': True,
        }
    finally:
        _state_abort()


DB_RECORDS_MAX_ROWS = 200
DB_RECORDS_MAX_CELL_CHARS = 4000
DB_RECORDS_DENY_TABLES = frozenset({
    'agent_keys', 'api_services', 'capability_handles', 'external_credentials',
    'temp_access_grants', 'jev_grants', 'sublow_grants', 'tank_account_grants',
    'tank_accounts', 'sessions', 'sms_messages', 'sms_read_grants',
    'kv_state', 'schema_migrations', 'sqlite_sequence',
})
DB_RECORDS_WHERE_OPS = frozenset({'=', '!=', '<', '<=', '>', '>=', 'LIKE'})


def _db_records_cell(value):
    if isinstance(value, str) and len(value) > DB_RECORDS_MAX_CELL_CHARS:
        return value[:DB_RECORDS_MAX_CELL_CHARS] + '…[truncated]'
    if isinstance(value, bytes):
        return f'<{len(value)} bytes>'
    return value


def _db_records_quote(name):
    return '"' + str(name).replace('"', '""') + '"'


def _db_records_columns(conn, table):
    rows = conn.execute(f'PRAGMA table_info({_db_records_quote(table)})').fetchall()
    return [{'name': row[1], 'type': row[2], 'notNull': bool(row[3]),
             'default': row[4], 'primaryKey': bool(row[5])} for row in rows]


def _db_records_table(conn, table):
    name = str(table or '').strip()
    if not name:
        raise ValueError('table is required')
    if name in DB_RECORDS_DENY_TABLES:
        raise ValueError('that table is not exposed to agents')
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,)).fetchone()
    if not row:
        raise ValueError('unknown table')
    return row[0]


def _db_records_where(conn, table, raw):
    columns = {c['name'] for c in _db_records_columns(conn, table)}
    items = []
    if isinstance(raw, dict):
        items = [(str(k), '=', v) for k, v in raw.items()]
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                raise ValueError('where entries must be objects')
            items.append((str(entry.get('column') or ''),
                          str(entry.get('op') or '=').upper(),
                          entry.get('value')))
    elif raw:
        raise ValueError('where must be an object or a list')
    clauses = []
    params = []
    for column, op, value in items:
        if column not in columns:
            raise ValueError(f'unknown column {column!r}')
        if op not in DB_RECORDS_WHERE_OPS:
            raise ValueError(f'unsupported operator {op!r}')
        clauses.append(f'{_db_records_quote(column)} {op} ?')
        params.append(value)
    return ' AND '.join(clauses), params


def supervisor_db_records(agent_id, args):
    """Chief of staff only: read or modify records in the think tank database.

    A controlled CRUD surface, not arbitrary SQL: table and column names are
    validated against the live schema, values are parameterized, update/delete
    require a non-empty filter, and a hard row cap bounds every read. Secret or
    identity-bearing tables are refused. Every operation is audit-logged. The
    whole-state blob (kv_state) is intentionally excluded; tasks/backlog records
    are changed through the normal state tools so the sim's single writer stays
    authoritative."""
    state = get_state_from_db() or {}
    if not _is_admin(state, agent_id):
        return {'error': 'only the chief of staff may read or modify database records'}
    op = str((args or {}).get('op') or 'tables').strip().lower()
    try:
        with _db() as conn:
            conn.row_factory = sqlite3.Row
            if op == 'tables':
                names = [row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "ORDER BY name").fetchall()]
                out = []
                for name in names:
                    if name in DB_RECORDS_DENY_TABLES:
                        continue
                    count = conn.execute(
                        f'SELECT COUNT(*) FROM {_db_records_quote(name)}').fetchone()[0]
                    out.append({'table': name, 'rows': count})
                return {'ok': True, 'tables': out}
            table = _db_records_table(conn, (args or {}).get('table'))
            columns = _db_records_columns(conn, table)
            column_names = {c['name'] for c in columns}
            if op == 'describe':
                return {'ok': True, 'table': table, 'columns': columns}
            if op == 'select':
                chosen = (args or {}).get('columns') or ['*']
                if not isinstance(chosen, list) or not chosen:
                    return {'error': 'columns must be a non-empty list'}
                for name in chosen:
                    if name != '*' and name not in column_names:
                        return {'error': f'unknown column {name!r}'}
                where_sql, params = _db_records_where(
                    conn, table, (args or {}).get('where'))
                order_by = (args or {}).get('orderBy')
                if order_by and order_by not in column_names:
                    return {'error': f'unknown orderBy column {order_by!r}'}
                try:
                    limit = int((args or {}).get('limit') or 50)
                except (TypeError, ValueError):
                    limit = 50
                limit = max(1, min(limit, DB_RECORDS_MAX_ROWS))
                sql = ('SELECT ' + ', '.join('*' if name == '*' else _db_records_quote(name)
                                             for name in chosen)
                       + f' FROM {_db_records_quote(table)}')
                if where_sql:
                    sql += ' WHERE ' + where_sql
                if order_by:
                    sql += ' ORDER BY ' + _db_records_quote(order_by)
                    if (args or {}).get('desc'):
                        sql += ' DESC'
                sql += ' LIMIT ?'
                rows = [{k: _db_records_cell(v) for k, v in dict(row).items()}
                        for row in conn.execute(sql, (*params, limit)).fetchall()]
                return {'ok': True, 'table': table, 'rows': rows,
                        'returned': len(rows), 'limit': limit}
            if op in ('insert', 'update', 'delete'):
                values = (args or {}).get('values')
                where_sql, where_params = _db_records_where(
                    conn, table, (args or {}).get('where'))
                if op == 'insert':
                    if not isinstance(values, dict) or not values:
                        return {'error': 'insert requires a non-empty values object'}
                    for name in values:
                        if name not in column_names:
                            return {'error': f'unknown column {name!r}'}
                    cols = list(values)
                    sql = (f'INSERT INTO {_db_records_quote(table)} ('
                           + ', '.join(_db_records_quote(c) for c in cols)
                           + ') VALUES (' + ', '.join('?' for _ in cols) + ')')
                    cur = conn.execute(sql, [values[c] for c in cols])
                    result = {'ok': True, 'table': table, 'inserted': cur.rowcount,
                              'lastRowId': cur.lastrowid}
                else:
                    if not where_sql:
                        return {'error': f'{op} requires a non-empty where filter'}
                    if op == 'delete':
                        cur = conn.execute(
                            f'DELETE FROM {_db_records_quote(table)} WHERE ' + where_sql,
                            where_params)
                        result = {'ok': True, 'table': table, 'deleted': cur.rowcount}
                    else:
                        if not isinstance(values, dict) or not values:
                            return {'error': 'update requires a non-empty values object'}
                        for name in values:
                            if name not in column_names:
                                return {'error': f'unknown column {name!r}'}
                        sets = ', '.join(f'{_db_records_quote(c)} = ?' for c in values)
                        cur = conn.execute(
                            f'UPDATE {_db_records_quote(table)} SET {sets} WHERE ' + where_sql,
                            [*values.values(), *where_params])
                        result = {'ok': True, 'table': table, 'updated': cur.rowcount}
                # Release the write lock before the audit row opens its own
                # connection (a second writer inside the open transaction would
                # block and then roll the whole change back on the error path).
                conn.commit()
                try:
                    log_action(agent_id, 'db_records_write', {
                        'op': op, 'table': table,
                        'columns': sorted((values or {}).keys()),
                        'rowcount': result.get('updated', result.get('deleted',
                                                                   result.get('inserted'))),
                    }, authorized=True)
                except Exception:
                    pass  # the write is already committed; audit is best-effort
                return result
            return {'error': f'unknown op {op!r}; use tables, describe, select, '
                             'insert, update, or delete'}
    except ValueError as exc:
        return {'error': str(exc)}
    except Exception as exc:
        return {'error': f'database records operation failed: {exc}'}


STATE_RECORDS_TOP_LEVEL = frozenset({
    'tasks', 'workQueue', 'backlogRequests', 'issues', 'teams', 'sprints',
    'products', 'features', 'backlog', 'researchTopics', 'retrospectives',
    'governance', 'directorSupervision', 'completionControl',
})
STATE_RECORDS_MAX_VALUE_CHARS = 200_000


def supervisor_state_records(agent_id, args):
    """Chief of staff only: read or modify live business records in the state
    blob (tasks, backlog requests, issues, teams, sprints, products, features,
    backlog, schedules, retrospectives, governance). A path-based set/get/delete
    under the state lock, so the sim's single writer stays authoritative. The
    roster, agent runtime state, settings, and every underscore-prefixed
    internal key are refused; the SQL tables have their own db_records tool."""
    state = _state_begin()
    try:
        if not state:
            return {'error': 'state unavailable'}
        if not _is_admin(state, agent_id):
            return {'error': 'only the chief of staff may read or modify state records'}
        op = str((args or {}).get('op') or 'collections').strip().lower()
        if op == 'collections':
            out = []
            for key in sorted(STATE_RECORDS_TOP_LEVEL):
                value = state.get(key)
                if isinstance(value, dict):
                    out.append({'collection': key, 'type': 'dict', 'count': len(value)})
                elif isinstance(value, list):
                    out.append({'collection': key, 'type': 'list', 'count': len(value)})
            return {'ok': True, 'collections': out}
        path = str((args or {}).get('path') or '').strip().strip('/')
        parts = [p for p in path.split('/') if p]
        if not parts or parts[0] not in STATE_RECORDS_TOP_LEVEL:
            return {'error': 'path must start with an allowed collection: '
                             + ', '.join(sorted(STATE_RECORDS_TOP_LEVEL))}
        if op in ('get', 'set', 'delete') and len(parts) < 2:
            return {'error': 'name a record inside the collection, e.g. tasks/task-1/status'}
        cur = state
        for part in parts[:-1]:
            if isinstance(cur, dict):
                if part not in cur:
                    return {'error': f'path not found at {part!r}'}
                cur = cur[part]
            elif isinstance(cur, list):
                try:
                    cur = cur[int(part)]
                except (ValueError, IndexError):
                    return {'error': f'list index not found at {part!r}'}
            else:
                return {'error': f'cannot traverse into {part!r}'}
        last = parts[-1]
        if op == 'get':
            if isinstance(cur, dict):
                if last not in cur:
                    return {'error': 'path not found'}
                value = cur[last]
            elif isinstance(cur, list):
                try:
                    value = cur[int(last)]
                except (ValueError, IndexError):
                    return {'error': 'list index not found'}
            else:
                return {'error': 'cannot read into a scalar'}
            try:
                encoded = json.dumps(value)
            except (TypeError, ValueError):
                encoded = ''
            if len(encoded) > STATE_RECORDS_MAX_VALUE_CHARS:
                return {'ok': True, 'path': path, 'value': None, 'truncated': True,
                        'note': f'value is {len(encoded)} chars; read a deeper path'}
            return {'ok': True, 'path': path, 'value': value}
        if op == 'set':
            if 'value' not in (args or {}):
                return {'error': 'set requires a value'}
            value = args.get('value')
            try:
                encoded = json.dumps(value)
            except (TypeError, ValueError):
                return {'error': 'value must be JSON-serializable'}
            if len(encoded) > STATE_RECORDS_MAX_VALUE_CHARS:
                return {'error': 'value is too large'}
            if isinstance(cur, dict):
                cur[last] = value
            elif isinstance(cur, list):
                try:
                    cur[int(last)] = value
                except (ValueError, IndexError):
                    return {'error': 'list index not found'}
            else:
                return {'error': 'cannot write into a scalar'}
            save_state_to_db(state)
            log_action(agent_id, 'state_records_set', {
                'path': path, 'valueType': type(value).__name__}, authorized=True)
            return {'ok': True, 'path': path, 'value': value}
        if op == 'delete':
            if isinstance(cur, dict):
                if last not in cur:
                    return {'error': 'path not found'}
                removed = cur.pop(last)
            elif isinstance(cur, list):
                try:
                    removed = cur.pop(int(last))
                except (ValueError, IndexError):
                    return {'error': 'list index not found'}
            else:
                return {'error': 'cannot delete from a scalar'}
            save_state_to_db(state)
            log_action(agent_id, 'state_records_delete', {
                'path': path, 'removedType': type(removed).__name__}, authorized=True)
            return {'ok': True, 'path': path, 'removed': removed}
        return {'error': f'unknown op {op!r}; use collections, get, set, or delete'}
    finally:
        _state_abort()


def supervisor_schedules(agent_id, args):
    """Team-agnostic schedule control for a director: list anything, create for
    themselves, and edit/delete only schedules they created (the chief of staff
    may edit any). Mutations take the state lock and mirror the watchlist."""
    op = (args.get('op') or 'list').strip().lower()
    if op == 'list':
        state = get_state_from_db() or {}
        return {'topics': [{'id': t.get('id'), 'topic': t.get('topic'),
                            'startUrl': t.get('startUrl'),
                            'cadenceMs': t.get('cadenceMs'),
                            'enabled': t.get('enabled', True) is not False,
                            'createdBy': t.get('createdBy'),
                            'lastRunAt': t.get('lastRunAt') or 0}
                           for t in (state.get('researchTopics') or [])]}
    state = _state_begin()
    try:
        if not _is_director_or_admin(state or {}, agent_id):
            return {'error': 'only a director or the chief of staff may change schedules'}
        if op == 'create':
            topic = (args.get('topic') or '').strip()
            url = (args.get('startUrl') or '').strip()
            parsed = urllib.parse.urlparse(url)
            if not topic or parsed.scheme not in ('http', 'https') or not parsed.netloc:
                return {'error': 'topic and a real absolute startUrl are required'}
            try:
                cadence = max(int(args.get('cadenceMs') or 0), 900000)
            except (TypeError, ValueError):
                return {'error': 'cadenceMs must be a number'}
            import sim as _sim
            record = _sim.add_research_topic(state, topic, url, cadence,
                                             created_by=agent_id)
            if not record:
                return {'error': 'could not create schedule'}
            config.note_watchlist_topic(record)
            save_state_to_db(state)
            log_action(agent_id, 'schedule_created', {'topicId': record['id'],
                                                      'topic': topic}, authorized=True)
            return {'ok': True, 'topic': record}
        topic_id = str(args.get('id') or '').strip()
        topics = state.get('researchTopics') or []
        record = next((t for t in topics if t.get('id') == topic_id), None)
        if not record:
            return {'error': 'unknown schedule'}
        if not _is_admin(state, agent_id) and record.get('createdBy') != agent_id:
            return {'error': 'you can only change schedules you created'}
        if op == 'delete':
            state['researchTopics'] = [t for t in topics if t.get('id') != topic_id]
            config.remove_watchlist_topic(record.get('topic'), record.get('startUrl'))
            save_state_to_db(state)
            log_action(agent_id, 'schedule_deleted', {'topicId': topic_id,
                                                      'topic': record.get('topic')}, authorized=True)
            return {'ok': True}
        if op == 'update':
            old = (record.get('topic'), record.get('startUrl'))
            if args.get('topic') is not None:
                record['topic'] = str(args['topic']).strip() or record.get('topic')
            if args.get('startUrl') is not None:
                url = str(args['startUrl']).strip()
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme not in ('http', 'https') or not parsed.netloc:
                    return {'error': 'startUrl must be a real absolute URL'}
                record['startUrl'] = url
            if args.get('cadenceMs') is not None:
                try:
                    record['cadenceMs'] = max(int(args['cadenceMs']), 900000)
                except (TypeError, ValueError):
                    return {'error': 'cadenceMs must be a number'}
            if args.get('enabled') is not None:
                record['enabled'] = args['enabled'] is not False
            config.update_watchlist_topic(old[0], old[1], record)
            save_state_to_db(state)
            log_action(agent_id, 'schedule_updated', {'topicId': topic_id,
                                                      'topic': record.get('topic')}, authorized=True)
            return {'ok': True, 'topic': record}
        return {'error': f'unknown op {op!r}; use list, create, update, or delete'}
    finally:
        _state_abort()


def supervisor_grant_budget(agent_id, task_id, budget_usd, reason=''):
    """The supervisor's ONE write: raise one live card's model-spend ceiling,
    or re-queue a card that closed on budget exhaustion with the higher ceiling.

    Enforced here, not in the prompt:
      - only a director/supervisor (not a worker) may call it;
      - only for a card on the caller's OWN team (the chief of staff may cover
        any team, matching his coordination role);
      - the new ceiling must exceed both the current ceiling and the spend so
        far, and stay under a hard cap;
      - the reason is required and recorded.
    Never touches the database tables, code, or another team's cards.
    """
    state = get_state_from_db() or {}
    actor = str(agent_id or '').strip()
    if not actor:
        return False, {'error': 'unknown supervisor'}
    if not _is_director(state, actor):
        return False, {'error': 'only a supervisor may grant budget'}
    task = (state.get('tasks') or {}).get(str(task_id or ''))
    if not task:
        return False, {'error': 'unknown card id'}
    team = supervisor_team_for(state, actor)
    is_admin = _is_admin(state, actor)
    if not is_admin:
        if not team:
            return False, {'error': 'you do not supervise a team'}
        members = set(team.get('members') or [])
        if task.get('teamId') != team.get('id') and task.get('assignedTo') not in members:
            return False, {'error': 'that card belongs to another team'}
    try:
        new_budget = float(budget_usd)
    except (TypeError, ValueError):
        return False, {'error': 'budgetUsd must be a number'}
    if not (0 < new_budget <= SUPERVISOR_BUDGET_CAP_USD):
        return False, {'error': f'budgetUsd must be between 0 and {SUPERVISOR_BUDGET_CAP_USD:.2f}'}
    note = str(reason or '').strip()
    if len(note) < 12:
        return False, {'error': 'a reason is required: say what the spend bought and why more is justified'}
    current = float(task.get('budgetUsd') or 0)
    spent = float(task.get('ownSpendUsd') or 0)
    if new_budget <= current:
        return False, {'error': f'new ceiling must exceed the current ${current:.2f}'}
    if new_budget <= spent:
        return False, {'error': f'new ceiling must exceed the ${spent:.4f} already spent'}
    status = task.get('status')
    if status in ('done', 'cancelled', 'abandoned'):
        return False, {'error': 'the card is already closed; nothing to fund'}
    grant = {'by': actor, 'at': int(time.time() * 1000),
             'from': current, 'to': new_budget, 'reason': note[:400]}
    if status == 'failed':
        # A card that closed on budget exhaustion is re-opened as a fresh card
        # with the higher ceiling; silently editing the dead task would never
        # put anyone back to work.
        if not (task.get('_budgetExhaustedNotified') or 'budget' in str(task.get('failNote') or '').lower()):
            return False, {'error': 'the card failed for a reason other than budget; file a new card instead'}
        import sim as _sim
        _sim.queue_work(state, [{
            'title': task.get('title') or f"Retry {task.get('id')}",
            'room': task.get('room') or 'pressoffice',
            'taskType': task.get('taskType') or 'code',
            'assignedTo': task.get('assignedTo'),
            'teamId': task.get('teamId'),
            'projectLabel': task.get('projectLabel'),
            'priority': 'high',
            'budgetUsd': new_budget,
            'sizeEstimate': 'S',
            'userStory': task.get('userStory'),
            'acceptanceCriteria': task.get('acceptanceCriteria'),
            'never': task.get('never'),
            'issueKey': task.get('issueKey'),
            'instructions': task.get('instructions'),
        }])
        task['reopenedAt'] = grant['at']
        task['reopenedBudgetUsd'] = new_budget
    task.setdefault('budgetGrants', []).append(grant)
    task['budgetUsd'] = new_budget
    task.pop('_budgetExhaustedNotified', None)
    save_state_to_db(state)
    log_action(actor, 'supervisor_budget_grant',
               {'taskId': task.get('id'), 'from': current, 'to': new_budget,
                'spent': spent, 'reopened': status == 'failed', 'reason': note[:300]},
               authorized=True)
    return True, {'ok': True, 'taskId': task.get('id'), 'budgetUsd': new_budget,
                  'spentUsd': spent, 'reopened': status == 'failed'}


def _senior_most_director_id(state):
    # "Senior-most director" = the director who stands in for the ADMIN to
    # approve/deny on the chief of staff's behalf. It is therefore the top of the
    # director chain EXCLUDING the admin(s): a director, not themselves
    # supervised by any other director (no `director` field), and not an admin.
    # In the current roster that is Nora (Theo is the chief of staff and also a director,
    # but she doesn't delegate to herself). Returns the agent id, or None if
    # there is no eligible non-chief-of-staff director to stand in (in which case
    # escalations just keep waiting for the human, as before).
    roster = state.get('agentRoster', []) if state else []
    for d in roster:
        if d.get('isDirector') and not d.get('isAdmin') and not d.get('director'):
            return d.get('id')
    return None
