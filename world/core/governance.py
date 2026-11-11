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


def is_change_request_validator(state, agent_id):
    """A change request's validator must be a supervisor (director), the
    chief of staff, or a regional VP. THE chief of staff (the admin) is
    the second, independent push approver and so is excluded from the
    validator slot by the request-time check -- the regional VP is the
    explicit authority tier even when she has no direct reports yet
    (a brand-new village with no senior directors). Operators direction
    (2026-11-12): the determination that approval-chain modifications are
    necessary may come from any of the three authority layers."""
    if not agent_id:
        return False
    return is_director(state, agent_id) or is_village_chief(state, agent_id)


def is_scrum_master(state, agent_id):
    if not agent_id:
        return False
    for team in (state or {}).get('teams') or []:
        if team.get('scrumMasterId') == agent_id:
            return True
    row = _entry(state, agent_id) or {}
    return bool(row.get('scrumMaster') or row.get('isScrumMaster'))


# --- Subdirector layer (operator direction, 2026-10-31) --------------------
#
# A subdirector is middle management: an agent reporting to a director who
# themselves has reports. The role is EMERGENT from the reporting graph
# (roster `director` fields), not a stored flag -- promoting someone means
# giving them reports, which the promote endpoint does by handing over a
# share of the director's workers.
#
# The 3+ rule: once a director leads SUBDIRECTOR_LAYER_MIN or more
# subdirectors, they stop directing workers in addition to the layer -- the
# remaining direct workers are distributed across the subdirectors, and the
# director's direct reports become exactly the subdirector layer. Below the
# threshold the director may still supervise workers directly.

SUBDIRECTOR_LAYER_MIN = 3
# The ceiling mirrors the house's working span (six desks to a room, sprint
# pools capped at maxTeamMembers 6): a director supervising more than six
# subdirectors has rebuilt the original over-wide span one level up. Past
# the ceiling the answer is a NEW TEAM (spawn_new_team_for_request), not a
# wider layer.
SUBDIRECTOR_LAYER_MAX = 6


def team_director_ids(state):
    """Ids that hold a teams row -- the senior directors, whatever tier they
    report to (the admin today, a village regional VP on the sharded
    org). Holding a teams row is the STABLE marker of the senior tier: the
    reporting pointer above them may change as chiefs of staff are added,
    but a teams row means "this agent owns a team" at every tier."""
    return {t.get('directorId') for t in (state.get('teams') or [])
            if isinstance(t, dict) and t.get('directorId')}


def village_chief_ids(state):
    """Village chiefs of staff (operator direction, 2026-11-01): the
    per-village regional VPs sitting between the one global chief of staff
    (the admin) and that village's senior directors. An explicit roster
    tier -- `villageChief: True` + `chiefOfVillage: <villageId>` -- not an
    emergent one: unlike subdirectors (middle management grown from the
    reporting graph), a chief is an AUTHORITY layer the operator appoints,
    and chiefs never count against the senior-director cap."""
    return [r.get('id') for r in _roster(state)
            if isinstance(r, dict) and r.get('villageChief') and r.get('id')]


def is_village_chief(state, agent_id):
    return bool(agent_id) and any(
        isinstance(r, dict) and r.get('id') == agent_id and r.get('villageChief')
        for r in _roster(state))


def villages_without_chief(state):
    """Village ids with no appointed regional manager. Operator invariant
    (2026-11-01): every village gets one at creation, so a chief-less
    village means the creation hook skipped (no eligible candidate yet) or
    a chief was lost -- a health alert, not a silent gap."""
    have = {r.get('chiefOfVillage') for r in _roster(state)
            if isinstance(r, dict) and r.get('villageChief')}
    return [v.get('id') for v in ((state or {}).get('villages') or [])
            if isinstance(v, dict) and v.get('id') and v.get('id') not in have]


def chief_of_village(state, village_id):
    """The village regional VP appointed for `village_id`, or None."""
    for r in _roster(state):
        if isinstance(r, dict) and r.get('villageChief') \
                and (r.get('chiefOfVillage') or None) == (village_id or None):
            return r.get('id')
    return None


def subdirectors_of(state, director_id):
    """Direct reports of director_id who themselves lead people -- EXCLUDING
    team directors. A village chief's team-leading children are that
    village's SENIOR directors (each holds a teams row), never the chief's
    subdirectors, and the admin's team-leading children are senior
    directors too. Subdirector is the middle-management tier BELOW a team
    director; the teams row is what separates the tiers, so the emergent
    classification stays correct as chiefs of staff are added above the
    senior layer."""
    team_dirs = team_director_ids(state)
    chiefs = set(village_chief_ids(state))
    return [rid for rid in direct_reports(state, director_id)
            if rid not in team_dirs and rid not in chiefs
            and direct_reports(state, rid)]


def has_subdirector_layer(state, director_id, minimum=None):
    minimum = SUBDIRECTOR_LAYER_MIN if minimum is None else minimum
    return len(subdirectors_of(state, director_id)) >= minimum


def _roster_index(state):
    return {r.get('id'): r for r in _roster(state)
            if isinstance(r, dict) and r.get('id')}


def team_director_of(state, agent_id):
    """The TOP-LEVEL director owning the reporting chain that `agent_id`
    sits in: the first ancestor whose own parent is absent, self-referencing,
    or the chief of staff. Team directors report to the admin in this
    codebase, so "walk to the root" would land on the admin for everyone --
    the stop anchor is the admin itself, and an admin's child who leads
    people IS a top-level team director while a childless admin-aide belongs
    to the admin's own team (the pre-layer one-hop behavior). This keeps
    every reader that treats the roster `director` field as "the team"
    (spend attribution, sprint staffing, status views) correct across the
    subdirector layer. Cycle-safe: a corrupted multi-node cycle resolves to
    None rather than looping forever."""
    if not agent_id:
        return None
    index = _roster_index(state)
    report_counts = {}
    for r in _roster(state):
        if isinstance(r, dict):
            d = r.get('director')
            if isinstance(d, str):
                report_counts[d] = report_counts.get(d, 0) + 1
    current = agent_id
    seen = set()
    while True:
        if current in seen:
            return None
        seen.add(current)
        row = index.get(current)
        if row is None:
            return None
        parent = row.get('director')
        if not parent or parent == current:
            return current
        if parent not in index:
            # The named director has no roster row (trimmed or synthetic):
            # the name IS the team, exactly the pre-layer one-hop read.
            return parent
        prow = index[parent]
        if prow.get('isAdmin'):
            # `current` sits directly under the chief of staff: with reports
            # it is a top-level team director; childless, it is the admin's
            # own aide and belongs to the admin's team (one hop, pre-layer).
            return current if report_counts.get(current) else parent
        current = parent


def team_director_map(state):
    """{agent_id: top-level director id} for every roster entry, one pass
    (same semantics as team_director_of, computed in bulk for attribution
    code that used to read the `director` field directly)."""
    index = _roster_index(state)
    report_counts = {}
    for r in _roster(state):
        if isinstance(r, dict):
            d = r.get('director')
            if isinstance(d, str):
                report_counts[d] = report_counts.get(d, 0) + 1

    def _root(aid):
        seen = set()
        current = aid
        while current in index:
            if current in seen:
                return None
            seen.add(current)
            parent = index[current].get('director')
            if not parent or parent == current:
                return current
            if parent not in index:
                return parent
            if index[parent].get('isAdmin'):
                return current if report_counts.get(current) else parent
            current = parent
        return current if current in index else None

    return {aid: _root(aid) for aid in index}


def team_members(state, director_id):
    """Every roster member whose ROOT director is director_id: direct reports
    plus everyone under their subdirectors, in roster order."""
    roots = team_director_map(state)
    return [r.get('id') for r in _roster(state)
            if isinstance(r, dict) and r.get('id')
            and roots.get(r.get('id')) == director_id]


def team_workers(state, director_id):
    """The working members of a team: team_members minus the director
    themself and the subdirector layer (supervisors facilitate and coach;
    they are not counted as worker hands). Roster order, deterministic."""
    subs = set(subdirectors_of(state, director_id))
    return [aid for aid in team_members(state, director_id)
            if aid != director_id and aid not in subs]


def rebalance_under_subdirectors(state, director_id):
    """The 3+ rule, applied: move the director's remaining direct workers
    under the subdirectors, least-loaded subdirector first (roster-order
    tiebreak), so the director's direct reports become exactly the layer.
    No-op below the threshold or with no workers to move. Returns the number
    of workers moved."""
    subs = subdirectors_of(state, director_id)
    if len(subs) < SUBDIRECTOR_LAYER_MIN:
        return 0
    workers = [rid for rid in direct_reports(state, director_id)
               if rid not in subs]
    if not workers:
        return 0
    index = {r.get('id'): r for r in _roster(state) if isinstance(r, dict)}
    loads = {sid: len(direct_reports(state, sid)) for sid in subs}
    order = {sid: pos for pos, sid in enumerate(subs)}
    moved = 0
    for wid in workers:
        row = index.get(wid)
        if row is None:
            continue
        target = min(subs, key=lambda sid: (loads[sid], order[sid]))
        row['director'] = target
        loads[target] += 1
        moved += 1
    return moved


def supervisor_for_new_hire(state, director_id):
    """Which existing agent a NEW hire reports to. Below the 3+ threshold the
    hire reports to the team director as always; with an active layer the
    hire lands under the least-loaded subdirector (deterministic roster-order
    tiebreak) -- a director with a layer directs subdirectors, not workers."""
    if not has_subdirector_layer(state, director_id):
        return director_id
    subs = subdirectors_of(state, director_id)
    if not subs:
        return director_id
    loads = {sid: len(direct_reports(state, sid)) for sid in subs}
    order = {sid: pos for pos, sid in enumerate(subs)}
    return min(subs, key=lambda sid: (loads[sid], order[sid]))


def enforce_subdirector_layers(state):
    """Defense in depth for the 3+ rule: sweep every director whose layer is
    active and whose direct reports still include plain workers (an organic
    crossing -- a worker quietly gained their own reports -- leaves exactly
    that state behind), and rebalance them. Idempotent and cheap; called
    after roster-graph-changing events. Returns the total workers moved."""
    moved = 0
    for r in _roster(state):
        if not isinstance(r, dict) or not r.get('id'):
            continue
        aid = r.get('id')
        if r.get('isAdmin'):
            continue
        reports = direct_reports(state, aid)
        if not reports or not has_subdirector_layer(state, aid):
            continue
        if set(reports) - set(subdirectors_of(state, aid)):
            moved += rebalance_under_subdirectors(state, aid)
    return moved


def may_work_code(state, agent_id):
    """Directors and the chief of staff delegate code; they do not write it.
    Subdirectors lead people, so the same policy reaches them through
    is_director's emergent definition."""
    return not is_director(state, agent_id)


def scale_worker_row(state, agent_id):
    """The roster row of a SCALE-TIER worker, or None. Structural only --
    callers combine it with the operator's `scaleTierWorkers` switch. A
    scale-tier worker is a plain worker: not the admin, leads nobody (so no
    director and no subdirector), and not the designated scrum master (the
    SM's coordination calls need judgment, not cheap volume)."""
    if not agent_id:
        return None
    row = _entry(state, agent_id)
    if not row or row.get('isAdmin'):
        return None
    if direct_reports(state, agent_id):
        return None
    if is_scrum_master(state, agent_id):
        return None
    return row


def scale_worker_ids(state):
    """{ids of scale-tier workers} in one roster pass -- the membership test
    model-tier resolution uses behind a short cache (never a per-call state
    read). Same semantics as scale_worker_row."""
    rows = [r for r in _roster(state) if isinstance(r, dict) and r.get('id')]
    counts = {}
    for r in rows:
        d = r.get('director')
        if isinstance(d, str):
            counts[d] = counts.get(d, 0) + 1
    sm_ids = {t.get('scrumMasterId') for t in (state or {}).get('teams') or []
              if t.get('scrumMasterId')}
    sm_ids |= {r.get('id') for r in rows
               if r.get('scrumMaster') or r.get('isScrumMaster')}
    out = set()
    for r in rows:
        aid = r.get('id')
        if r.get('isAdmin') or aid in sm_ids:
            continue
        if counts.get(aid):
            continue
        out.add(aid)
    return out


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
    mode = str(((state or {}).get('automation') or {}).get('mode') or '').lower()
    if mode in ('drain', 'idle', 'paused', 'off', 'manual'):
        return 0
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


def village_team_count(state, admin_id=None):
    """SENIOR team records across the WHOLE ORG (operator direction,
    2026-11-01: 12 senior directors all together -- the cap is global, not
    per village). A team counts when its director sits at the top of the
    non-admin hierarchy: reporting to the admin, to their village's chief
    of staff, or having no director at all (the legacy senior tier). The
    admin's own aide cluster is not a team, and subdirector sub-teams
    (director reporting to a director) do not count. Growth beyond the cap
    is a new VILLAGE with its own chief of staff above its senior
    directors, or subdirectors below them -- never a 13th senior
    director."""
    state = state or {}
    admin_id = admin_id or next(
        (d.get('id') for d in (state.get('agentRoster') or []) if d.get('isAdmin')),
        None)
    if not admin_id:
        return 0
    rows = {d.get('id'): d for d in (state.get('agentRoster') or [])}
    chiefs = set(village_chief_ids(state))
    count = 0
    for t in (state.get('teams') or []):
        did = t.get('directorId')
        if not did or did == admin_id:
            continue
        parent = (rows.get(did) or {}).get('director')
        if parent in (admin_id, None) or parent in chiefs:
            count += 1
    return count
