

def project_board(args):
    args = args or {}
    status = (args.get('status') or 'open').strip().lower()
    team = (args.get('team') or '').strip()
    project = (args.get('project') or '').strip().lower()
    limit = max(1, min(int(args.get('limit') or 30), 100))
    state = _serve.get_state_from_db() or {}
    tasks = state.get('tasks') or {}
    rows = []
    counts = {'open': 0, 'done': 0, 'other': 0}
    for task_id, task in tasks.items():
        if not isinstance(task, dict):
            continue
        st = str(task.get('status') or '')
        if st in ('done',):
            counts['done'] += 1
        elif st in ('cancelled', 'abandoned'):
            counts['other'] += 1
        else:
            counts['open'] += 1
        if status == 'open' and st in ('done', 'cancelled', 'abandoned'):
            continue
        if status == 'done' and st != 'done':
            continue
        if team and team not in (str(task.get('teamId') or ''), str(task.get('assignedTo') or '')):
            continue
        label = str(task.get('projectLabel') or '')
        if project and project not in label.lower() and project not in str(task.get('title') or '').lower():
            continue
        report_path = str(task.get('reportPath') or '')
        artifact = False
        if report_path:
            try:
                full = os.path.normpath(os.path.join(_serve.LIBRARY_DIR, report_path))
                root = os.path.normpath(_serve.LIBRARY_DIR)
                artifact = (full == root or full.startswith(root + os.sep)) and os.path.isfile(full)
            except Exception:
                artifact = False
        rows.append({
            'taskId': task_id,
            'title': task.get('title') or '',
            'status': st,
            'assignedTo': task.get('assignedTo') or '',
            'teamId': task.get('teamId') or '',
            'projectLabel': label,
            'priority': task.get('priority') or '',
            'budgetUsd': task.get('budgetUsd') or 0,
            'spentUsd': task.get('spentUsd') or task.get('spendUsd') or 0,
            'acceptanceCriteria': task.get('acceptanceCriteria') or '',
            'reportPath': report_path,
            'artifactExists': artifact,
            'parentTaskId': task.get('parentTaskId') or '',
            'updatedAt': task.get('updatedAt') or task.get('createdAt') or 0,
        })
    rows.sort(key=lambda r: (r['status'] == 'done', -(float(r['updatedAt']) if r['updatedAt'] else 0)))
    # Team rollup (operator, 2026-11-23, the Meta Factory steal): each team
    # gets one header line on the pulse board with its in-flight spend and a
    # health dot. The dot is 'sync' in the video's sense (is this team's
    # pipeline healthy right now) but computed from OUR ground truth: green
    # when open cards and none blocked, amber when cards are blocked,
    # red when a repair loop stopped for the team in the last 24h -- never a
    # CI fraction the server cannot see.
    import time as _time
    _cutoff = _time.time() - 24 * 3600
    _taskTeam = {str(tid): str((t or {}).get('teamId') or '')
                 for tid, t in (state.get('tasks') or {}).items()}
    _stoppedTeams = set()
    try:
        with _serve._db() as _conn:
            for (det,) in _conn.execute(
                    "SELECT details FROM action_log WHERE action = 'repair_loop_stopped' "
                    "AND ts >= ?", (_cutoff,)).fetchall():
                try:
                    _tid = str(json.loads(det or '{}').get('taskId') or '')
                except Exception:
                    _tid = ''
                if _tid and _taskTeam.get(_tid):
                    _stoppedTeams.add(_taskTeam[_tid])
    except Exception:
        pass
    teams = {}
    for r in rows:
        tid = r.get('teamId') or 'unassigned'
        g = teams.setdefault(tid, {
            'teamId': tid, 'open': 0, 'done': 0, 'blocked': 0,
            'spendOpenUsd': 0.0, 'spendTotalUsd': 0.0, 'sync': 'green',
        })
        st = r.get('status') or ''
        spent = float(r.get('spentUsd') or 0)
        g['spendTotalUsd'] += spent
        if st == 'done':
            g['done'] += 1
        elif st in ('cancelled', 'abandoned'):
            pass
        else:
            g['open'] += 1
            g['spendOpenUsd'] += spent
            if st in ('blocked', 'repairing', 'failed'):
                g['blocked'] += 1
    for k, g in teams.items():
        if k in _stoppedTeams or g['blocked'] >= 3:
            g['sync'] = 'red'
        elif g['blocked']:
            g['sync'] = 'amber'
    return json.dumps({'ok': True, 'counts': counts, 'tasks': rows[:limit],
                       'teams': sorted(teams.values(),
                                       key=lambda g: -g['spendTotalUsd'])},
                      sort_keys=True)
