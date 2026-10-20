"""Roles and approval policy for the think tank.

Pure state helpers, so sim/selfedit/serve can share one definition without
import cycles. The policy is intentionally small:

- directors and the chief of staff never write code or arm code self-edits;
- they may still file stories, spikes, and bugs from sprint findings;
- a scrum master may grant file/app entitlements when a director is tied up;
- a code change request needs one director and the chief of staff, not the player.
"""

from __future__ import annotations

CODE_TASK_TYPES = frozenset({'code', 'bug', 'qa'})


def _roster(state):
    return (state or {}).get('agentRoster') or []


def _entry(state, agent_id):
    for row in _roster(state):
        if row.get('id') == agent_id:
            return row
    return None


def is_admin(state, agent_id):
    row = _entry(state, agent_id)
    return bool(row and row.get('isAdmin'))


def direct_reports(state, agent_id):
    return [r.get('id') for r in _roster(state)
            if r.get('director') == agent_id]


def is_director(state, agent_id):
    """A director is the chief of staff or anyone with at least one direct report."""
    if not agent_id:
        return False
    return is_admin(state, agent_id) or bool(direct_reports(state, agent_id))


def is_scrum_master(state, agent_id):
    if not agent_id:
        return False
    for team in (state or {}).get('teams') or []:
        if team.get('scrumMasterId') == agent_id:
            return True
    row = _entry(state, agent_id) or {}
    return bool(row.get('scrumMaster') or row.get('isScrumMaster'))


def may_work_code(state, agent_id):
    """Directors and the chief of staff delegate code; they do not write it."""
    return not is_director(state, agent_id)


def may_self_edit(state, agent_id):
    return may_work_code(state, agent_id)


def may_grant_entitlements(state, agent_id):
    return is_scrum_master(state, agent_id) or is_director(state, agent_id)


def admin_id(state):
    for row in _roster(state):
        if row.get('isAdmin'):
            return row.get('id')
    return None


def change_request_approvers(state, implementer):
    """Return (director_id, admin_id) for a change request.

    The director is the implementer's own director when one exists; otherwise
    the first non-chief-of-staff director. The chief of staff is always the roster admin. They
    must be different people; if the only director is the chief of staff, the request
    is not approvable under the two-person rule.
    """
    admin = admin_id(state)
    row = _entry(state, implementer) or {}
    director = row.get('director')
    if not director or director == admin:
        director = next((r.get('id') for r in _roster(state)
                         if r.get('id') != admin and is_director(state, r.get('id'))), None)
    if not director or director == admin:
        return None, admin
    return director, admin


SUPERVISION_MIN = 0
SUPERVISION_MAX = 5
SUPERVISION_DEFAULT = 3
SUPERVISION_THRESHOLDS = {1: 5, 2: 3}


def project_key(task):
    """Stable per-project key: explicit project id, else the project label,
    else the team. A missing key is the director's default policy."""
    if not isinstance(task, dict):
        return '_default'
    for key in ('projectId', 'projectLabel', 'teamId'):
        value = task.get(key)
        if value:
            return str(value)
    return '_default'


def supervision_level(state, director_id, project=None):
    raw = ((state or {}).get('directorSupervision') or {}).get(director_id) or {}
    value = raw.get(project) if project else None
    if value is None:
        value = raw.get('_default', SUPERVISION_DEFAULT)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = SUPERVISION_DEFAULT
    return max(SUPERVISION_MIN, min(SUPERVISION_MAX, value))


def set_supervision_level(state, director_id, level, project=None):
    """Set one director's 0-5 supervision level, for one project or their
    default. Returns the stored entry."""
    try:
        level = int(level)
    except (TypeError, ValueError):
        raise ValueError('level must be an integer 0-5')
    if level < SUPERVISION_MIN or level > SUPERVISION_MAX:
        raise ValueError('level must be between 0 and 5')
    key = project or '_default'
    levels = state.setdefault('directorSupervision', {})
    entry = levels.setdefault(director_id, {})
    entry[key] = level
    return entry


def _task_map(state):
    raw = (state or {}).get('tasks')
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {t.get('id'): t for t in raw if isinstance(t, dict) and t.get('id')}
    return {}


def _blocking_task_ids(state):
    """Map each failed/blocked task id to the number of live cards that
    depend on it."""
    tasks = _task_map(state)
    blocked = {tid for tid, t in tasks.items()
               if t.get('status') in ('blocked', 'failed')}
    blocking = {}
    for t in tasks.values():
        if t.get('status') in ('done', 'cancelled', 'abandoned'):
            continue
        dep = t.get('dependsOn')
        if isinstance(dep, str):
            dep = [dep]
        for d in dep or []:
            if d in blocked:
                blocking[d] = blocking.get(d, 0) + 1
    return blocking


def supervisor_tick(state, now_ms=None):
    """File an immediate blocker bug for a failed/blocked task that gates
    other work, and leave the dependent work waiting on it.

    Runs at a 60s cadence inside the sim tick. The bug is a high-priority
    incident pinned to the owning team's on-call; the blocked cards keep
    their `dependsOn` edge, so they cannot resume until it lands.
    """
    import time
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    state.setdefault('governance', {}).setdefault(
        'changeRequestAdminApproval', True)
    last = int((state or {}).get('lastSupervisorScanAt') or 0)
    if now_ms - last < 60000:
        return 0
    state['lastSupervisorScanAt'] = now_ms
    tasks = _task_map(state)
    blocking = _blocking_task_ids(state)
    recent = {tid for tid, task in tasks.items()
              if (task.get('status') in ('blocked', 'failed')
                  and now_ms - int(task.get('updatedAt') or task.get('lastProgressAt') or 0)
                  <= 10 * 60 * 1000)}
    try:
        import sim
    except Exception:
        return 0
    filed = 0
    for tid in sorted(set(blocking) | recent):
        task = tasks.get(tid) or {}
        if task.get('taskType') == 'bug' or task.get('reviewOf'):
            continue
        if task.get('blockerBugId'):
            continue
        team_id = task.get('teamId')
        if not team_id:
            team_id = sim._task_owner_director(state, task)
        level = supervision_level(state, team_id, project_key(task))
        dependents = blocking.get(tid, 0)
        if level == 0:
            continue
        if level < 3 and dependents < SUPERVISION_THRESHOLDS.get(level, 1):
            continue
        if level < 4 and dependents == 0:
            continue
        title = f"Blocker: {(task.get('title') or tid)[:90]}"
        already = any((t.get('title') or '') == title and
                      t.get('status') not in ('done', 'cancelled', 'abandoned')
                      for t in list(tasks.values()) + list(state.get('workQueue') or []))
        if already:
            continue
        if not team_id:
            continue
        oc = sim.on_call_agent(state, team_id, task.get('sprintId'), now_ms)
        if not oc:
            continue
        sim.queue_work(state, [{
            'title': title,
            'room': task.get('room') or 'pressoffice',
            'taskType': 'bug',
            'assignedTo': oc,
            'directRoute': True,
            'incident': True,
            'priority': 'high',
            'teamId': team_id,
            'instructions': (f"BLOCKER BUG for {tid}: {task.get('title')}. "
                             "Work this before the blocked area resumes. "
                             "Fix the underlying failure, run the scoped "
                             "checks, and report the exact result. Do not "
                             "close it without a landed fix."),
        }])
        task['blockerBugId'] = title
        filed += 1
    return filed


def admin_change_request_enabled(state):
    """Production switch for the director+admin change-request chain.

    Off by default so unit fixtures keep the historical player-signature
    flow; flip it in live state's `governance.changeRequestAdminApproval`.
    """
    return bool(((state or {}).get('governance') or {})
                .get('changeRequestAdminApproval'))
