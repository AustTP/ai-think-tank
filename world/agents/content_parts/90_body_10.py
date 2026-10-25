

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
    return json.dumps({'ok': True, 'counts': counts, 'tasks': rows[:limit]}, sort_keys=True)
