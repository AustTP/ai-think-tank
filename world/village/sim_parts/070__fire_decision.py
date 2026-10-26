

def _fire_decision(state, reviewers, candidate_def, now_ms, decider=None):
    """firing.js finishFiringReview's Jev verdict + deterministic fallback:
    'fire' or 'keep'. Pure decision; the consultation + idle guards are applied
    by the caller around it. `decider` injectable for tests (defaults to Jev)."""
    candidate = (state.get('agents') or {}).get(candidate_def.get('id'))
    morale = morale_for(state, candidate_def.get('id'), now_ms)
    reports = reports_about(state, candidate_def.get('id'))
    report_quotes = (' | '.join(f'"{r.get("quote")}" -- {r.get("note")} (severity: {r.get("severity") or "unclassified"})'
                                for r in reports)) or 'none filed'
    info = _firing_consultation(state, candidate_def.get('id'))
    consultant_line = (
        ' | '.join([*(f'{r.get("name")} (reported them: "{r.get("quote")}"'
                     f'{(" -- " + r.get("note")) if r.get("note") else ""})' for r in info['reporters']),
                    *(f'{c.get("name")} (currently working directly with {candidate.get("name")})'
                      for c in info['coworkers'])]) or 'no one reported them and no one is currently working directly with them')
    reviewer1, reviewer2 = reviewers
    strikes = _report_strikes(state, candidate_def.get('id'), now_ms)
    window_days = REPORT_STRIKE_WINDOW_MS // (24 * 3600 * 1000)
    instructions = (f'{reviewer1.get("name")} and {reviewer2.get("name")} are jointly reviewing '
                    f'{candidate.get("name")}\'s ({candidate.get("role")}) performance. '
                    f'{reviewer1.get("name")} is the admin; {reviewer2.get("name")} is the senior-most '
                    f'director standing in for the admin. Morale score: {morale}/100. Approved work: '
                    f'{candidate.get("approvedCount")}. Dropped work: {candidate.get("droppedCount")}. '
                    f'Reports filed against them: {report_quotes}. '
                    f'Firing-signal strikes inside the {window_days}-day grace window: {strikes["count"]} '
                    f'({strikes["severe"]} severe). '
                    f'{candidate.get("name")}\'s own manager has '
                    f'already been notified of these reports. People consulted who have worked with or reported '
                    f'{candidate.get("name")}: {consultant_line}. Decide whether to fire them or keep them on.')
    candidates = [
        {'id': 'fire', 'description': f"End {candidate.get('name')}'s role in the think tank -- performance does not justify keeping them on, and the people who work with them don't outweigh the evidence."},
        {'id': 'keep', 'description': f"Keep {candidate.get('name')} on -- performance is acceptable, improving, or the evidence or the people who work with them don't support firing."},
    ]
    decision = (decider(state, instructions, candidates) if decider
                else _governance_decider(state, instructions, candidates))
    if not decision:
        # Jev-outage fallback re-keyed off the firing signal, not
        # the raw morale score. Firing requires a real corroborated problem:
        # a negative report AND a genuine drop-off (handed work, dropped more
        # than a third of what they approved). Either alone stays 'keep' --
        # a report-only or drop-off-only agent is a help/hiring target, not a
        # firing one (see _has_firing_signal).
        strikes = _report_strikes(state, candidate_def.get('id'), now_ms)
        has_report = strikes['count'] >= 2 or strikes['severe'] >= 1
        dropped = candidate.get('droppedCount') or 0
        approved = candidate.get('approvedCount') or 0
        decision = ('fire' if has_report and dropped > approved * 0.3
                    else 'keep')
    return decision, morale


def _sim_direct_reports(state, director_id):
    """Direct reports of a director, from the `director` pointer already on
    every roster entry (purely structural -- no serve.py import needed)."""
    return [d.get('id') for d in (state.get('agentRoster') or [])
            if d.get('director') == director_id]


def _normalize_attendees(attendees):
    """Normalize a meeting card's attendee spec for storage: a list of
    roster ids kept in given order (non-strings dropped), a scope string
    ('team'/'all'/'pair') kept as-is for grooming-time resolution, or
    None (defaults to the team scope at grooming)."""
    if isinstance(attendees, (list, tuple)):
        return [a for a in attendees if isinstance(a, str) and a.strip()]
    if isinstance(attendees, str) and attendees.strip():
        return attendees.strip().lower()
    return None


def _meeting_team_members(state, team_id):
    """The owning team's people: its director, its scrum master, and the
    director's direct reports -- the same pool the team's own ceremonies
    convene. Stable order (director, scrum master, then roster order),
    deduped."""
    team = _team_row(state, team_id)
    if not team:
        return []
    director = team.get('directorId')
    scm = team.get('scrumMasterId')
    out = []
    seen = set()
    for m in [director, scm] + _sim_direct_reports(state, director):
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _resolve_meeting_attendees(state, attendees, team_id, facilitator_id=None):
    """Resolve a meeting card's attendee spec to concrete roster ids at
    GROOMING time (backlog refinement, before the sprint starts). A list
    of ids is validated against the roster (unknown ids dropped, order
    kept); a scope string expands: 'team' = the owning team's people
    (director + scrum master + direct reports), 'all' = the full think
    tank roster, 'pair' = the facilitator plus ONE teammate (the smallest
    useful meeting). None/unknown defaults to the team scope. Returns a
    stable, deduped list -- possibly empty (a meeting with nobody to
    convene still runs; the facilitator is always an attendee in spirit,
    but only appears in the list when the scope puts them there)."""
    roster_ids = {d.get('id') for d in (state.get('agentRoster') or [])
                  if d.get('id')}
    if isinstance(attendees, (list, tuple)):
        out = []
        seen = set()
        for a in attendees:
            if a in roster_ids and a not in seen:
                seen.add(a)
                out.append(a)
        return out
    scope = (attendees or 'team')
    scope = scope.strip().lower() if isinstance(scope, str) else 'team'
    if scope == 'all':
        return sorted(roster_ids)
    if scope == 'pair':
        team = _meeting_team_members(state, team_id)
        pair = []
        if facilitator_id in team:
            pair.append(facilitator_id)
        for m in team:
            if m != facilitator_id:
                pair.append(m)
                break
        if not pair and team:
            pair = team[:2]
        return pair
    return _meeting_team_members(state, team_id)


# Peer-approval gate (Phase E addendum): a deliverable-room task's story is not
# 'done' when its primary work finishes -- it moves to 'needs_review' and waits
# for TWO distinct same-team peers to approve it. Only rooms with real content
# executors gate; chores complete directly.
PEER_REVIEW_TIMEOUT_MS = 15 * 60 * 1000  # sim-ms: after one vote, request a fresh second reviewer
# Phase E3.1: stuck-at-review watchdog. A gated story's reviewer pool is locked at
# entry; the watchdog guarantees that pool stays capable of producing a vote (or
# widens it), so a story can't deadlock on an unreachable/inert locked pair.
STUCK_GATE_CADENCE_MS               = 20_000
STUCK_GATE_GRACE_MS                 = 5 * 60 * 1000   # no widening before this age
STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS = 30 * 60 * 1000  # min gap between re-picks
# State-blob pruning: the whole kv_state blob is autosaved every ~1s, so
# unbounded append-only structures make that write slower and slower. The
# prune pass bounds the historical TAIL of each (live/open/pending entries are
# always kept); `tasks` is intentionally left alone (it backs per-agent history).
_STATE_PRUNE_CADENCE_MS = 60 * 60 * 1000  # hourly
_TAIL_COMPLETED_DELIVERABLES = 500
_TAIL_BACKLOG_RESOLVED = 200
_TAIL_PLAYER_INBOX_ANSWERED = 100
_TAIL_ISSUES_CLOSED = 100
_TAIL_GROWTH_PLANS_APPLIED = 50
# Non-gated work lanes (Phase E2b/E2d): a SPIKE is a time-boxed investigation
# with no committed deliverable; a BUG is incident response on a live product
# (see the on-call phase); a MEETING is a deliberate conversation whose
# minutes land on the card itself (see _run_meeting_content). None ships a
# peer-reviewed story -- all three land a short artifact instead.
NON_GATED_LANES = frozenset({'spike', 'bug', 'research', 'meeting', 'supervise', 'report'})


def _peer_gated_lane(task):
    """True when a task's deliverable work should wait for peer approval -- i.e.
    it's in a deliverable room AND it's a normal (non-spike/bug) lane. A
    spike/bug in a deliverable room completes directly (its author is released
    but no gate is opened; nothing to vote on).

    skillReview/distill/research are standing housekeeping sweeps
    (_check_schedules), not authored stories -- there is no real "fix" for a
    reviewer to send back against "review whatever is in
    pending_review/skills/" or "crawl this site and update its skill file",
    so a reject just spawns a fix subtask whose completion unconditionally
    re-arms the SAME gate (_task_cycle's reviewOf/fix branch has no
    _peer_gated_lane check, unlike the initial-entry branch).
    One skill-review sweep, routed through this same gate as
    real coding/research work, looped reviewer-reject/re-fix for over 20
    minutes straight, flooding the workQueue with 50+ duplicate 'Review:
    Review pending skill files' entries and sending the same handful of
    agents back to the observatory door over and over -- exempted then, but
    a scheduled research crawl (task['research'], also queued by
    _check_schedules, also landing in the observatory -- a deliverable room)
    was missed. It recurred: a single "AI regulation
    news" schedule spiraled into 3,917 task assignments and 26,687
    escalations over one evening -- review found the crawl "actionable" (a
    scheduled crawl obviously has no passing flake8/mypy/pytest-cov suite to
    fail cleanly), queued a code fix for something that was never a coding
    deliverable, that fix's own review found it just as unfixable, forever.
    Exempting all three here (same shape as NON_GATED_LANES) stops them from
    ever entering the gate at all, so there is no vote to reject and nothing
    to re-arm."""
    if task.get('skillReview') or task.get('distill') or task.get('research'):
        return False
    return _deliverable_room(task.get('room')) and task.get('taskType') not in NON_GATED_LANES


def _pick_reviewer_ids(state, author_id, task_room=None, preferred=None):
    """Two reviewers for a gate. Under Phase E2 cross-assignment the author may
    pick up work outside her own team, so the ideal reviewer is one who shares
    the author's team OR has actually completed work in the story's target room
    (the known-craft signal, `completedRooms` -- see _note_completed_room).
    Candidate order = the author's team first, then every non-admin non-author
    agent. Prefer (0) on-team OR room-contributor over a stranger, (1) same-team,
    (2) room-contributor, (3) idle, (4) stable roster order. Same-team members
    still win the first two slots when the team can supply them. Never returns
    duplicate or author reviewers.

    `preferred`: a prior reviewer-pair (e.g. from a rejected gate). A rejection
    is a request to *verify the flagged problem was actually addressed*, which a
    stranger cold-reviewing cannot do -- so the rejecting reviewers are kept at
    the head of the candidate order. If they're gone/unreachable the pick falls
    back to the general widening below."""
    agents = state.get('agents') or {}
    roster = state.get('agentRoster') or []
    author_roster = next((d for d in roster if d.get('id') == author_id), None)
    team = []
    if author_roster and author_roster.get('director'):
        team = _sim_direct_reports(state, author_roster['director'])
    # Candidates = preferred pair first, then team (excl author), then widen to
    # all non-author.
    order = list(preferred or []) + team + [d.get('id') for d in roster if not d.get('isAdmin') and d.get('id') != author_id]
    seen = set()
    ordered_ids = []
    for cid in order:
        # A candidate whose agent record was deleted is a ghost -- never enqueue
        # a review to an id that can't actually review (this can otherwise leak
        # in from the team/widen portions too, not just `preferred`).
        if cid != author_id and cid not in seen and agents.get(cid):
            seen.add(cid)
            ordered_ids.append(cid)
    team_set = set(team)

    def _idle_key(cid):
        a = agents.get(cid) or {}
        cid_room_contrib = bool(task_room) and task_room in (a.get('completedRooms') or [])
        return (0 if preferred and cid in preferred else 1,  # prior reviewers stay at the head
                1 if cid not in team_set and not cid_room_contrib else 0,  # stranger loses
                0 if cid in team_set else 1,                               # same-team first
                0 if cid_room_contrib else 1,                              # known-craft next
                0 if a.get('offDuty') else 1,
                a.get('busy') is True,
                roster_order(cid, roster))
    ordered_ids.sort(key=_idle_key)
    if not preferred:
        # Bell-style cross-pollination (2026-10-06): when the tank can supply
        # both an on-team AND an off-team reviewer, force the pair to span
        # teams -- a reviewer who doesn't share the author's team's normalized
        # assumptions catches what the team itself has learned to ignore.
        # Off-team means a worker reporting to a DIFFERENT team director than
        # the author's (the author's own director standing in is not
        # cross-pollination, and a bare director with no team of their own is
        # not either). Falls back to the pure quality ordering when the pool
        # can't span (a tiny think tank) or when re-using a prior pair (a
        # rejection must be re-verified by the same reviewer who flagged it).
        team_directors = {t.get('directorId') for t in (state.get('teams') or []) if t.get('directorId')}
        author_director = (author_roster or {}).get('director')
        off_team = []
        for c in ordered_ids:
            if c in team_set:
                continue
            cd = next((d.get('director') for d in roster if d.get('id') == c), None)
            if cd and cd != author_director and cd in team_directors:
                off_team.append(c)
        same_team = [c for c in ordered_ids if c in team_set]
        if same_team and off_team:
            return [same_team[0], off_team[0]]
    return ordered_ids[:2]


def roster_order(cid, roster):
    for i, d in enumerate(roster):
        if d.get('id') == cid:
            return i
    return 0


def _fix_title_root(title):
    """Strip repeated 'Fix: ' prefixes and retry suffixes so 'Fix: Fix: Act on
    mail (retry 2)' and 'Act on mail' compare equal. Mirrors _task_fix_spirals'
    own reduction."""
    root = str(title or '').strip()
    while root.startswith('Fix: '):
        root = root[len('Fix: '):].strip()
    while True:
        reduced = re.sub(r'\s*\(retry\s+\d+\)\s*$', '', root).strip()
        if reduced == root:
            break
        root = reduced
    return root


def _open_fix_exists(state, title):
    """True when a live 'Fix: <root>' card already covers this title. Prevents
    the send-back path from minting the same fix over and over for one parent."""
    root = _fix_title_root(title)
    if not root:
        return False
    for q in state.get('workQueue') or []:
        if _fix_title_root(q.get('title')) != root:
            continue
        if q.get('status') in ('completed', 'failed', 'cancelled'):
            continue
        return True
    for t in (state.get('tasks') or {}).values():
        if _fix_title_root(t.get('title')) != root:
            continue
        if t.get('status') in ('completed', 'failed', 'cancelled'):
            continue
        return True
    return False


REPAIR_LOOP_WINDOW_MS = 6 * 60 * 60 * 1000
REPAIR_LOOP_MIN_FAILURES = 2

# Adaptive suppression: a card pattern that keeps failing does not get an
# infinite supply of fresh ids. The pattern is the issueKey (precise) with a
# normalized-title fallback for cards that have no issue. Three failures
# inside 24h freezes the pattern; a new issue with a new key is unaffected.
CARD_PATTERN_WINDOW_MS = 24 * 60 * 60 * 1000
CARD_PATTERN_FAILURE_LIMIT = 3


def _card_pattern_key(task):
    tid = (task or {}).get('issueKey')
    if tid:
        return 'issue:' + str(tid)
    root = _fix_title_root((task or {}).get('title'))
    return ('title:' + root) if root else None


def _card_pattern_failures(state, key, now_ms):
    if not key:
        return []
    recent = []
    for t in (state.get('tasks') or {}).values():
        if t.get('status') not in ('failed', 'blocked', 'abandoned'):
            continue
        if _card_pattern_key(t) != key:
            continue
        ts = t.get('failedAt') or t.get('blockedAt') or t.get('createdAt') or 0
        if now_ms - int(ts) <= CARD_PATTERN_WINDOW_MS:
            recent.append(t)
    return recent


def _card_pattern_suppressed(state, task, now_ms):
    key = _card_pattern_key(task)
    if not key:
        return None
    failures = _card_pattern_failures(state, key, now_ms)
    if len(failures) < CARD_PATTERN_FAILURE_LIMIT:
        return None
    return {'key': key, 'failures': len(failures),
            'taskIds': [t.get('id') for t in failures][:8]}


def _stop_card_pattern(state, task, info, author):
    now_ms = int(time.time() * 1000)
    task['status'] = 'blocked'
    task['blockedReason'] = 'pattern_suppressed'
    task['blockedAt'] = now_ms
    task['failNote'] = (f"Pattern suppressed after {info['failures']} failures in "
                        f"24h ({info['key']}). Fix the card contract before minting "
                        "another one.")[:300]
    task['nextAction'] = ('Do not mint another card for this issue/pattern. '
                          'Compare the lane output with the completion contract.')
    notified = state.setdefault('_patternSuppressedNotified', {})
    if now_ms - float(notified.get(info['key']) or 0) > CARD_PATTERN_WINDOW_MS:
        notified[info['key']] = now_ms
        director = _repair_loop_director(state, task)
        agent = (state.get('agents') or {}).get(director) if director else None
        if isinstance(agent, dict):
            _append_mailbox(agent, {
                'kind': 'card_pattern_suppressed', 'ts': now_ms, 'read': False,
                'taskId': task.get('id'), 'title': task.get('title'),
                'text': (f'Stopped minting cards for {info["key"]} after '
                         f'{info["failures"]} failures in 24h '
                         f'({", ".join(info["taskIds"])}). The card contract is '
                         'wrong; fix it before another card is filed.')})
        _log_governance(state, author, 'card_pattern_suppressed',
                        {'taskId': task.get('id'), 'pattern': info['key'],
                         'failures': info['failures'], 'taskIds': info['taskIds']})
        try:
            from serve import send_player_telegram_sync
            send_player_telegram_sync(
                '[AI Think Tank] Card pattern suppressed',
                (f'Stopped minting cards for {info["key"]} after '
                 f'{info["failures"]} failures in 24h.\n'
                 f'Task: {task.get("title") or task.get("id")}\n'
                 f'Tasks: {", ".join(info["taskIds"])}\n'
                 'The card contract is wrong. Fix it before another card is filed.'))
        except Exception:
            pass


def _repair_loop_should_stop(state, title, now_ms):
    """Same root failed twice inside the window. A third fix card would repeat
    the same contract, so stop and hand the mismatch to the director."""
    root = _fix_title_root(title)
    if not root:
        return None
    recent = []
    for t in (state.get('tasks') or {}).values():
        if t.get('status') not in ('failed', 'abandoned'):
            continue
        if _fix_title_root(t.get('title')) != root:
            continue
        ts = t.get('failedAt') or t.get('blockedAt') or t.get('createdAt') or 0
        if now_ms - int(ts) <= REPAIR_LOOP_WINDOW_MS:
            recent.append(t)
    if len(recent) < REPAIR_LOOP_MIN_FAILURES:
        return None
    return {'root': root, 'failures': len(recent),
            'taskIds': [t.get('id') for t in recent][:6]}


def _repair_loop_director(state, task):
    team_id = task.get('teamId')
    for team in (state.get('teams') or []):
        if team_id and team_id in (team.get('id'), team.get('directorId')):
            return team.get('directorId')
    author = task.get('assignedTo') or task.get('reviewAuthorId')
    for d in (state.get('agentRoster') or []):
        if d.get('id') == author:
            return d.get('director')
    return None


def _stop_repair_loop(state, task, info, author):
    now_ms = int(time.time() * 1000)
    task['status'] = 'blocked'
    task['blockedReason'] = 'repair_loop'
    task['blockedAt'] = now_ms
    task['failNote'] = (f"Repair loop stopped after {info['failures']} failures of "
                        f"the same card: the lane output and the completion gate "
                        f"disagree. Fix the contract before re-carding.")[:300]
    task['nextAction'] = ('Compare what the lane produces with what the '
                          'completion gate accepts; do not mint another Fix card.')
    key = f"repair_loop:{info['root']}"
    notified = state.setdefault('_repairLoopNotified', {})
    if key not in notified:
        notified[key] = now_ms
        director = _repair_loop_director(state, task)
        agent = (state.get('agents') or {}).get(director) if director else None
        if isinstance(agent, dict):
            _append_mailbox(agent, {
                'kind': 'card_repair_loop', 'ts': now_ms, 'read': False,
                'taskId': task.get('id'), 'title': task.get('title'),
                'text': (f'Repair loop stopped for "{task.get("title")}" after '
                         f'{info["failures"]} failures ({", ".join(info["taskIds"])}). '
                         'The lane output and the completion gate disagree. '
                         'Fix the contract before another card is minted.')})
    _log_governance(state, author, 'repair_loop_stopped',
                    {'taskId': task.get('id'), 'root': info['root'],
                     'failures': info['failures'], 'taskIds': info['taskIds'],
                     'note': (task.get('failNote') or '')[:200]})


def _queue_peer_repair(state, parent, rationale=None):
    """Queue one author-owned repair for a rejected deliverable."""
    if (parent.get('_peerGate') or {}).get('escalated'):
        return None
    author = parent.get('assignedTo')
    if not author or author not in (state.get('agents') or {}):
        return None
    tid = parent['id']
    existing = next((q for q in state.get('workQueue') or []
                     if q.get('reviewOf') == tid and q.get('taskType') not in ('review', 'qa')), None)
    active = next((t for t in (state.get('tasks') or {}).values()
                   if t.get('reviewOf') == tid and t.get('taskType') not in ('review', 'qa')
                   and t.get('status') in ('walking', 'working')), None)
    if existing or active:
        parent['status'] = 'repairing'
        parent['_peerRepairPending'] = True
        return existing or active
    now_ms = int(time.time() * 1000)
    pattern = _card_pattern_suppressed(state, parent, now_ms)
    if pattern:
        _stop_card_pattern(state, parent, pattern, author)
        return None
    loop = _repair_loop_should_stop(state, parent.get('title'), now_ms)
    if loop:
        _stop_repair_loop(state, parent, loop, author)
        return None
    findings = '\n'.join(
        f"{r.get('reviewer')}: {r.get('summary') or ''} "
        + '; '.join(str(check) for check in r.get('checks') or [])
        for r in (parent.get('peerReviews') or [])[-2:]
        if r.get('verdict') == 'actionable')
    feedback = rationale or 'Address the rejected peer-review findings.'
    instructions = (feedback + '\n' + findings
                    + f"\nOriginal deliverable: {parent.get('title')}"
                    + f"\nExisting Library document: {parent.get('libraryPath') or 'not yet saved'}."
                    + '\nSave the corrected deliverable and meet the original acceptance criteria before requesting review.')
    item = {
        'title': 'Fix: ' + str(parent.get('title') or tid),
        'room': parent.get('room') or 'pressoffice',
        'taskType': parent.get('taskType') or 'code',
        'deliverable': parent.get('deliverable'),
        # Lane markers must survive a repair. Distill/skill-review cards are
        # queued with taskType 'code', so without these a repair ran the
        # coding executor and could never satisfy the lane's completion rule.
        'distill': bool(parent.get('distill')),
        'distillSince': parent.get('distillSince') or 0,
        'skillReview': bool(parent.get('skillReview')),
        'kbClass': parent.get('kbClass'),
        'research': parent.get('research'),
        'assignedTo': author,
        'directRoute': True,
        'reviewOf': tid,
        'reviewAuthorId': author,
        'instructions': instructions,
        'reviewContract': parent.get('reviewContract'),
        'acceptanceCriteria': parent.get('acceptanceCriteria'),
        'userStory': parent.get('userStory'),
        'never': parent.get('never') or None,
        'sourcePaths': parent.get('sourcePaths'),
        'productId': parent.get('productId'),
        'issueKey': parent.get('issueKey'),
        'workOrderId': parent.get('workOrderId'),
        'workOrderStoryId': parent.get('workOrderStoryId'),
        'requestId': parent.get('requestId'),
        'projectLabel': parent.get('projectLabel'),
        'budgetUsd': parent.get('budgetUsd'),
        'priority': 'high',
    }
    parent['status'] = 'failed'
    parent['_peerRepairPending'] = True
    parent['_peerGate'] = dict(parent.get('_peerGate') or {})
    parent['_peerGate']['escalated'] = False
    queue_work(state, [item])
    return item


# Peer-review close rule (operator decision, 2026-10-22). A normal story
# needs two distinct clean approvals (the adversarial split). A sprint staffed
# with only two workers cannot produce a second reviewer besides the author
# without pulling a third agent into it, so one clean approval is the bar
# there. `requiredApprovals` is stamped on the gate at open time and carried
# across re-entries so the close rule never has to re-derive it.
PEER_APPROVALS_DEFAULT = 2
PEER_APPROVALS_LEAN_SPRINT = 1
LEAN_SPRINT_WORKERS = 2


def _peer_gate_required(state, task):
    """Distinct clean approvals this gate needs: 1 for a sprint staffed with
    at most two workers, else 2."""
    sprint_id = (task or {}).get('sprintId')
    if not sprint_id:
        return PEER_APPROVALS_DEFAULT
    try:
        pool = _sprint_worker_pool(state, sprint_id)
    except Exception:
        pool = None
    if pool is not None and len(pool) <= LEAN_SPRINT_WORKERS:
        return PEER_APPROVALS_LEAN_SPRINT
    return PEER_APPROVALS_DEFAULT


def _enter_peer_review(state, task, now_ms, preferred=None):
    """Move a deliverable task whose primary content work just finished into the
    peer-approval gate: status -> 'needs_review', pick two reviewers (see
    _pick_reviewer_ids -- one is forced cross-team when the tank can supply it),
    record the gate (approval counter + reviewer pair), and notify each reviewer
    (the 'tell your teammates' step, server-side at the trigger instant). Returns
    the _peerGate dict (or None if the task is misconfigured).

    Adversarial split (2026-10-06): the FIRST reviewer is designated the CRITIC
    -- instructed to prove the work wrong, not rubber-stamp it -- so a gate
    approval means the work survived hostile scrutiny, and a rubber-stamp pair
    can no longer pass a weak deliverable on 2 clean votes. The other reviewer
    keeps the standard skeptical-review framing. Same spend, stronger signal.

    `preferred` (prior reviewer pair): a re-open (reviewer rejection of the fix,
    or a player veto re-opening a closed story) should be re-verified by the
    reviewer(s) who already have the context -- the person who flagged a problem
    is the one who can confirm it was fixed. Fall back to a fresh pick if the
    prior pair is unavailable."""
    author = task.get('assignedTo')
    prior = task.get('_peerGate') or {}
    if preferred is None:
        preferred = prior.get('reviewerIds') or None
    required = _peer_gate_required(state, task)
    reviewers = _pick_reviewer_ids(state, author, task_room=task.get('room'), preferred=preferred)
    if not reviewers:
        return None
    reviewers = reviewers[:required]
    gate = {
        'room': task.get('room'),
        'approvals': 0,
        'requiredApprovals': required,
        'approvers': [],
        'reviewerIds': reviewers,
        'enteredMs': now_ms,
        # review subtask ids appended as they're enqueued (for board surfacing)
        'reviewedTaskIds': [],
        # Fail-closed gate: a story that has already cycled (a rejected review
        # or a failed-pipeline send-back) carries its prior cycle count + freeze
        # flag into the fresh gate, so the shared MAX_REVIEW_CYCLES cap is never
        # reset by a re-entry -- the total review/fix loops stay bounded no
        # matter WHICH mechanism keeps re-arming the gate. A frozen (escalated)
        # gate stays frozen: it re-arms here only as a fresh needs_review entry
        # for display, never to restart the loop (callers guard on `escalated`).
        'cycleCount': prior.get('cycleCount') or 0,
        'escalated': bool(prior.get('escalated')),
    }
    task['status'] = 'needs_review'
    task['_peerGate'] = gate
    # Peer review is agent-to-agent -- the two
    # reviewers picked below (mailbox 'peer_review_request') handle it, so the
    # player doesn't need an email for every routine story entering review,
    # only for things that actually need player input (agent_ask, card_blocked
    # below). This used to also email the player here; removed on request.
    review_items = []
    for i, rid in enumerate(reviewers):
        # Adversarial split: reviewer[0] is the CRITIC, briefed to find the
        # flaw; reviewer[1] keeps the standard skeptical frame.
        critic = (i == 0)
        _append_mailbox((state.get('agents') or {}).get(rid, {}), {
            'kind': 'peer_review_request',
            'about': task.get('id'),
            'title': task.get('title'),
            'text': (f'Work on "{task.get("title")}" is ready for your review. '
                     + ('As the CRITIC, your job is to find what is wrong with it -- assume it has a hidden flaw until you prove it does not, and send it back if you find a concrete, fixable problem.'
                        if critic else
                        'Please review it and approve or send it back.'))})
        review_items.append({
            'title': f'Review: {task.get("title")}',
            'room': task.get('room'),
            'instructions': (f'You are the CRITIC on this review. Review the work for "{task.get("title")}" '
                             'as an adversary: assume it has a hidden flaw until you have proven it does not. '
                             'Check the claims, the numbers, and the reasoning. Approve ONLY if the work survives '
                             'your scrutiny; if you find a concrete, fixable problem, send it back.'
                             if critic else
                             f'Review the work for "{task.get("title")}" and approve it only if it is genuinely solid '
                             'with no concrete, fixable problems. Be a skeptical reviewer, not a rubber stamp.'),
            'goal': task.get('projectLabel') or task.get('goal'),
            'taskType': 'review',
            # projectLabel + taskType=review is what routes pressoffice work to the
            # review executor; reviewOf links this vote back to the story.
            'projectLabel': task.get('projectLabel') or task.get('title'),
            'assignedTo': rid,
            'reviewOf': task.get('id'),
            # Phase E3: the ORIGINAL AUTHOR of the story (the parent's
            # assignedTo). A gate review that rejects must hand the fix back to
            # the worker who built the story -- NOT to the reviewer -- and the
            # review executor needs this id on the subtask to pin that fix.
            'reviewAuthorId': task.get('assignedTo'),
        })
    queue_work(state, review_items)
    return gate


def _cascade_rereview(state, reopened_task_id, now_ms=None):
    """Todo: ripple re-review via the dependency cascade. When a DELIVERED
    story is re-opened (player veto -- the only path a 'done' story can return
    to review), any story that DEPENDED on it (task['dependsOn'] = the reopened
    id, plumbed from the blocked card's issue) was built on the now-questioned
    output. A shipped dependent is sent back through the SAME peer gate --
    preferring the reviewers who already know it -- so the ripple gets
    re-verified too, not just the directly-flagged story. Fail-closed guards:
    only gated lanes, never an escalated (frozen) gate, and a dependent with no
    eligible reviewers right now stays 'done' rather than dropping to limbo.
    Returns the number of dependents re-opened."""
    if not reopened_task_id:
        return 0
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    reopened = 0
    for task in (state.get('tasks') or {}).values():
        if not isinstance(task, dict) or task.get('dependsOn') != reopened_task_id:
            continue
        if task.get('status') != 'done':
            continue
        if not _peer_gated_lane(task):
            continue
        prior = task.get('_peerGate') or {}
        if prior.get('escalated'):
            continue
        if _enter_peer_review(state, task, now_ms) is not None:
            reopened += 1
    return reopened


def _resolve_review_parent(state, review_task):
    """The parent task a gate-review subtask voted on (by its `reviewOf` id), or
    None. A parent already closed or no longer gated is skipped."""
    pid = review_task.get('reviewOf')
    if not pid:
        return None
    parent = (state.get('tasks') or {}).get(pid)
    if not parent or not parent.get('_peerGate'):
        return None
    return parent


def _sim_notify_author(state, parent, reviewer_id, rationale=None):
    """On an 'actionable' verdict the gate re-opens: notify the author that their
    work was sent back for a fix (and who rejected it). The actual fix task is
    queued by the executor's queueFix; here we just file the mailbox note. W6:
    the rejection RATIONALE also lands as a growth-plan note (kind
    'review_denial', repeat=True) routed to the author's NEXT task, so a peer
    denial changes the author's next execution -- not just a one-time mailbox
    message."""
    author = parent.get('assignedTo')
    if not author:
        return
    _gate = parent.get('_peerGate') or {}
    _cycle = _gate.get('cycleCount', 0)
    _max_cycles = team_policy(state, parent.get('teamId') or agent_team_ref(state, author), 'maxReviewCycles')
    _cycle_note = f' (rejection {_cycle} of {_max_cycles})' if _cycle else ''
    _upsert_rejection_mailbox((state.get('agents') or {}).get(author, {}), {
        'kind': 'peer_review_rejected',
        'about': parent.get('id'),
        'title': parent.get('title'),
        'text': f'A reviewer{(" (" + reviewer_id + ")") if reviewer_id else ""} sent your work on "{parent.get("title")}" back{_cycle_note} -- it needs a fix before it can close. Fix it and it will be reviewed again.'})
    # W6: a denial is actionable coaching, not just a message. The reviewer's
    # rationale (the review subtask's note) is carried onto the author's next
    # task so the fix starts from the actual problem raised.
    if rationale:
        _write_growth_plan(
            state, author, parent.get('room') or 'pressoffice',
            'review_denial', int(time.time() * 1000),
            f"Peer review of '{parent.get('title')}' was sent back for a fix. "
            f"What the reviewer said: {(rationale or '')[:400]}. Address this on "
            f"your next pass.", repeat=True)
    # Item 2: the rejection itself lands in the author's feedback buffer, so the
    # NEXT dispatch's perception block shows what came back at them (same W6
    # intent -- coaching that changes the next execution -- but visible in the
    # worker's situational awareness, not just the growth-plan loop).
    _append_feedback(state, author,
                     f"Peer review of '{parent.get('title')}' was sent back for "
                     f"a fix. What the reviewer said: {(rationale or 'no reason given')[:400]}",
                     source='peer_review')


def _sim_notify_author_failed(state, task, cycle=None, is_infra=False):
    """Fail-closed send-back signal: tell the author their deliverable was sent
    back because it FAILED the quality pipeline (a red flake8/mypy/bandit/
    pytest-cov run -- see content.py's coding executor), and a fix is queued
    back to them. Mirrors _sim_notify_author's mailbox shape.

    `cycle` (when given) stamps "rejection N of MAX_REVIEW_CYCLES" on the
    notice so the agent can tell a first rejection from a fifth without
    counting copies. `is_infra` switches the wording: an infrastructure
    failure is not the agent's quality, and the notice must say so -- pass
    cycle=None for infra send-backs, whose retries are not quality rejections."""
    author = task.get('assignedTo')
    if not author:
        return
    _max_cycles = team_policy(state, task.get('teamId') or agent_team_ref(state, author), 'maxReviewCycles')
    cycle_note = f' (rejection {cycle} of {_max_cycles})' if cycle else ''
    if is_infra:
        text = (f'Your work on "{task.get("title")}" hit an infrastructure '
                f'failure{cycle_note} -- not a quality problem with your code. '
                f'It will be retried automatically.')
    else:
        text = (f'Your work on "{task.get("title")}" failed the quality pipeline'
                f'{cycle_note} and was sent back -- it needs a fix before it can '
                f'be reviewed. Fix it and it will be reviewed again.')
    _upsert_rejection_mailbox((state.get('agents') or {}).get(author, {}), {
        'kind': 'peer_review_rejected',
        'about': task.get('id'),
        'title': task.get('title'),
        'text': text})
    # Item 2: the pipeline failure also lands in the author's feedback buffer, so
    # the next dispatch's perception block names the failing task.
    if not is_infra:
        _append_feedback(state, author,
                         f"Your deliverable '{task.get('title')}' failed the quality "
                         f"pipeline and was sent back. It will be reviewed again after "
                         f"you fix it.",
                         source='pipeline')


def _log_quality_gate_reject(state, task, note, escalated):
    """red_pipeline telemetry: a durable, queryable marker for every fail-closed
    quality-gate send-back (a red flake8/mypy/bandit/pytest-cov content result
    that was CAUGHT and never marked done). Mirrors the selfProposedRejected
    marker pattern (_resolve_refinement) so the health check can count rejects
    vs. escapes with a JSON-substring LIKE instead of a JSON query. The gate is
    fail-closed, so `redPipelineEscaped` is always false today -- the counter's
    job is to trip loudly if a regression ever lets a red result complete."""
    details = {'taskId': task.get('id'),
               'author': task.get('assignedTo'),
               'title': (task.get('title') or '')[:120],
               'note': (note or '')[:120],
               'redPipelineEscaped': False,
               'redPipelineEscalated': bool(escalated)}
    _log_governance(state, task.get('assignedTo'), 'quality_gate_reject', details)


INFRA_SEND_BACKS = 3  # same-work retries an infra-tagged failure gets before the gate freezes


def _send_back_after_failure(state, task, fail_note=None, is_infra=None):
    """Fail-closed quality-gate send-back. A deliverable (or a fix of one) whose
    content result failed the pipeline is marked `failed` -- deliberately NOT
    'needs_review', because _sweep_stuck_gates only processes needs_review and
    would otherwise re-pin fresh reviewers onto a card whose pipeline is red.
    The author is notified (mailbox) -- this is the one send-back path that
    MUST tell the worker, whether the story can be fixed or has escalated.
    Under the shared review-cycle cap a fix is queued back to the original
    author (pinned + high priority); once the cap is crossed the story
    escalates (create_escalation) and stays frozen -- no further fix cycling.
    Pure state mutation; called from _task_cycle's content-result branch. The
    prior gate (if any) is kept so _maybe_escalate_stuck_gate's shared cap and
    _enter_peer_review's carry-over both see it.

    Infra failures (a model-call/API/sandbox fault -- `result['infra']`, not
    the agent's quality) never enter that fix-cycle machinery: no coaching
    note (there is nothing the agent could have done differently), no
    review-cycle bump (a flaky outage must never auto-close a healthy story),
    and no 'Fix:' card. The SAME work is re-queued to the original author
    unchanged (reviewOf keeps the pin + the green-result re-arm path), at most
    INFRA_SEND_BACKS times; after that the gate is marked `escalated` (frozen)
    and the story stops ticking, so a persistent outage can neither ship an
    unverified story nor loop. `is_infra` overrides the task tag: a fix
    subtask's infra result lands on the fix, but the PARENT is the card being
    resent, and the parent tag alone misreads the cause."""
    author = task.get('assignedTo')
    note = (fail_note or task.get('note') or 'the pipeline did not pass').strip()
    now_ms = int(time.time() * 1000)
    if is_infra is None:
        is_infra = bool(task.get('_infraFailure'))
    if not is_infra:
        # Count this failure before the guard so the current card is part of
        # the same-root count. Two recent failures of one root is a contract
        # mismatch, not a retry.
        task['status'] = 'failed'
        task['failedAt'] = now_ms
        task['failNote'] = note[:300]
        pattern = _card_pattern_suppressed(state, task, now_ms)
        if pattern:
            _stop_card_pattern(state, task, pattern, author)
            return
        loop = _repair_loop_should_stop(state, task.get('title'), now_ms)
        if loop:
            _stop_repair_loop(state, task, loop, author)
            return
    _notify_card_failure(state, task, note, category='failed', now_ms=now_ms)
    if not task.get('_peerGate'):
        task['_peerGate'] = {'approvals': 0, 'approvers': [],
                             'reviewerIds': [], 'enteredMs': 0}
    gate = task['_peerGate']
    if is_infra and gate.get('infraRetries', 0) >= INFRA_SEND_BACKS:
        # Infra cap reached: freeze for manual attention. Deliberately NOT
        # _close_gated_story -- the pipeline never passed clean, so "ship what
        # exists" would be exactly the red-pipeline escape the gate exists to
        # prevent.
        task['status'] = 'failed'
        task['failedAt'] = int(time.time() * 1000)
        task['failNote'] = (f'frozen after {INFRA_SEND_BACKS} infrastructure-failure '
                            f'retries: {note}')[:300]
        gate['escalated'] = True
        _append_mailbox((state.get('agents') or {}).get(author, {}), {
            'kind': 'infrastructure_failure_frozen',
            'title': task.get('title'),
            'text': (f'Your work on "{task.get("title")}" kept hitting '
                     f'infrastructure failures across {INFRA_SEND_BACKS} automatic '
                     f'retries. The story is frozen pending a manual look -- no '
                     f'action is needed from you.')})
        _log_governance(state, author, 'infra_send_back',
                        {'taskId': task.get('id'), 'title': (task.get('title') or '')[:120],
                         'frozen': True, 'retries': gate.get('infraRetries', 0),
                         'note': note[:120]})
        return
    # Cycle counter for the mailbox notice: "rejection N of MAX" so the agent
    # can tell a first rejection from a fifth without counting copies. Infra
    # retries are NOT quality rejections -- no cycle stamp on their wording.
    _sim_notify_author_failed(state, task,
                              cycle=None if is_infra else gate.get('cycleCount', 0) + 1,
                              is_infra=is_infra)
    # Feedback injection (brunnfeld): a concrete failure reason must change the
    # author's NEXT execution, not just land as a mailbox message. Same W6
    # pattern as peer-review denial (_sim_notify_author): the pipeline failure
    # reason is routed through the growth-plan loop so it is injected once into
    # the author's next task via _coaching_note_for/_augment_task_instructions.
    # SKIPPED for infra failures: a model-call fault is not coaching material --
    # there is nothing the agent should change (leo's round-3 review).
    if author and not is_infra:
        _write_growth_plan(
            state, author, task.get('room') or 'pressoffice',
            'pipeline_feedback', int(time.time() * 1000),
            f"Your deliverable '{task.get('title')}' failed the quality pipeline. "
            f"What failed: {(note or '')[:400]}. Address this on your next pass.",
            repeat=True)
    task['status'] = 'failed'
    task['failedAt'] = int(time.time() * 1000)
    task['failNote'] = note[:300]
    if is_infra:
        instructions = (f"Your work on '{task.get('title')}' hit an infrastructure "
                        f"failure (not your code): {note}. The same task is being "
                        f"retried -- no change to your approach is needed.")
    else:
        instructions = (f"Your work on '{task.get('title')}' failed the quality pipeline: "
                        f"{note}. Fix it so the pipeline passes clean, then it will be re-reviewed.")
    if task.get('userStory'):
        instructions += f"\n\nUser story: {task['userStory']}"
    if task.get('acceptanceCriteria'):
        instructions += f"\n\nAcceptance criteria:\n{task['acceptanceCriteria']}"
    if task.get('never'):
        instructions += f"\n\nNEVER:\n{task['never']}"
    if is_infra:
        # Bounded same-work retry: count separately from the review cycle, log
        # with its own governance kind (the red-pipeline telemetry stays about
        # quality), and requeue the identical work. No _maybe_escalate_stuck_gate
        # here -- the cycle cap must stay a QUALITY verdict.
        gate['infraRetries'] = gate.get('infraRetries', 0) + 1
        _log_governance(state, author, 'infra_send_back',
                        {'taskId': task.get('id'), 'title': (task.get('title') or '')[:120],
                         'retry': gate['infraRetries'], 'limit': INFRA_SEND_BACKS,
                         'note': note[:120]})
        queue_work(state, [{
            'title': task.get('title'),
            'room': task.get('room'),
            'instructions': instructions,
            'goal': task.get('projectLabel') or task.get('goal'),
            'projectLabel': task.get('projectLabel') or task.get('title'),
            'taskType': 'code',
            'deliverable': task.get('deliverable'),
            'reviewOf': task.get('id'),
            'reviewAuthorId': author,
            'assignedTo': author,
            'productId': task.get('productId'),
            'userStory': task.get('userStory'),
            'acceptanceCriteria': task.get('acceptanceCriteria'),
            'never': task.get('never') or None,
            'sourcePaths': task.get('sourcePaths'),
            'issueKey': task.get('issueKey'),
            'requestId': task.get('requestId'),
            'priority': WORK_PRIORITY['high'],
        }])
        return
    escalated = _maybe_escalate_stuck_gate(state, task, gate,
                                           'quality pipeline keeps failing')
    _log_quality_gate_reject(state, task, note, escalated=escalated)
    if escalated:
        return  # auto-closed after cycle cap -- no further fix cycling
    if _open_fix_exists(state, task.get('title')):
        _log_governance(state, author, 'fix_send_back_deduped',
                        {'taskId': task.get('id'), 'title': (task.get('title') or '')[:120]})
        return
    alternatives = []
    agents = state.get('agents') or {}
    team_id = task.get('teamId')
    if team_id:
        for aid in _sim_direct_reports(state, team_id):
            agent = agents.get(aid)
            if (aid != author and agent and not agent.get('busy')
                    and not agent.get('task') and not agent.get('pairWith')
                    and not agent.get('offDuty')
                    and governance.may_work_code(state, aid)):
                alternatives.append(aid)
    decision = policies.decide_retry(task, gate, note, is_infra=False,
                                     alternatives=alternatives)
    assignee = author
    if (policies.apply_enabled() and decision.get('action') == 'reassign'
            and decision.get('target') in alternatives):
        assignee = decision['target']
    policies.record_shadow('retry', decision, {
        'taskId': task.get('id'),
        'title': (task.get('title') or '')[:120],
        'applied': assignee != author,
    })
    if assignee != author:
        _log_governance(state, author, 'retry_reassigned',
                        {'taskId': task.get('id'), 'from': author,
                         'to': assignee, 'cycleCount': gate.get('cycleCount', 0)})
    queue_work(state, [{
        'title': f'Fix: {task.get("title")}',
        'room': task.get('room'),
        'instructions': instructions,
        'goal': task.get('projectLabel') or task.get('goal'),
        'projectLabel': task.get('projectLabel') or task.get('title'),
        'taskType': task.get('taskType') or 'code',
        'deliverable': task.get('deliverable'),
        # Same lane-marker rule as the peer-repair card above.
        'distill': bool(task.get('distill')),
        'distillSince': task.get('distillSince') or 0,
        'skillReview': bool(task.get('skillReview')),
        'kbClass': task.get('kbClass'),
        'research': task.get('research'),
        'reviewOf': task.get('id'),
        'reviewAuthorId': assignee,
        'assignedTo': assignee,
        'productId': task.get('productId'),
        'userStory': task.get('userStory'),
        'acceptanceCriteria': task.get('acceptanceCriteria'),
        'never': task.get('never') or None,
        'sourcePaths': task.get('sourcePaths'),
        'issueKey': task.get('issueKey'),
        'requestId': task.get('requestId'),
        'priority': WORK_PRIORITY['high'],
    }])


def _peer_gate_should_close(gate, now_ms, entered_ms):
    """Close rule: the gate's required distinct clean approvals, normally two.
    A two-worker sprint stamps one (see _peer_gate_required). The old
    single-vote timeout is gone. A quiet tank gets a fresh second reviewer from
    the stuck-gate sweep instead of lowering the bar. A gate below its bar
    stays open until the vote lands or the gate escalates."""
    if gate.get('escalated'):
        return False
    required = int(gate.get('requiredApprovals') or PEER_APPROVALS_DEFAULT)
    return gate['approvals'] >= required


def _note_completed_room(state, agent_id, task):
    """Record a room an agent has actually completed work in (deliverable work
    bumps it, whether the story closes via finish_task or the peer gate's
    _release_agent_gated). Drives two Phase E2 surfaces: the reviewer-widening
    signal ("knows the room's craft") and the cross-learning affordance
    ('roomsLearned'). Idempotent -- a room is tracked once. Cut 2 also fires
    the fire-and-forget deliverable grader + incident runbook writer here, so
    both completion paths grade/learn exactly once."""
    if not task or not task.get('room'):
        return
    a = (state.get('agents') or {}).get(agent_id)
    if not a:
        return
    rooms = a.setdefault('completedRooms', [])
    if task.get('room') not in rooms:
        rooms.append(task.get('room'))
    now_ms = int(time.time() * 1000)
    _grade_completed_task(state, agent_id, task, now_ms)
    _runbook_task(state, task, now_ms)


def _release_agent_gated(state, agent_id, grid):
    """Release an agent whose deliverable work just finished but whose STORY stays
    open in 'needs_review' (as opposed to finish_task, which marks it 'done').
    Clears the agent's task/busy/inRoom and sends the agent off duty (vanishes
    in place). The approval count and the deliverable grade land at GATE CLOSE,
    not here: work that reviewers may still reject must not count as shipped.
    """
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return
    completed = (state.get('tasks') or {}).get(a.get('task'))  # capture before clearing
    a['task'] = None
    a['busy'] = False
    a['inRoom'] = None
    a['visible'] = True
    _maybe_file_followup(state, agent_id, completed, int(time.time() * 1000))


def _release_agent_after_failure(state, agent_id, task):
    """Release an agent whose deliverable work FAILED the quality pipeline (or
    whose fix subtask failed it). Unlike _release_agent_gated / finish_task this
    does NOT bump approvals, record a completed room, grade the deliverable, or
    file a follow-up -- a red-pipeline result is not shipped work and must not
    count as one (a fabricated 0.0 trailing grade would otherwise pollute the
    roadmap signal and a follow-up would propose more work off a failure). The
    completed task is marked 'done' so _reclaim_orphaned_walking_tasks never
    re-issues it. Pure state mutation."""
    agents = state.get('agents') or {}
    a = agents.get(agent_id)
    if not a:
        return
    if task and task.get('status') not in ('blocked', 'failed', 'cancelled', 'abandoned'):
        task['status'] = 'done'
        # This is NOT a completion. Marking the task done only stops the
        # orphan sweep from re-issuing it. Log it as an auto-close so
        # throughput telemetry can tell "never finished because the pipeline
        # rejected it" from "still in flight" -- without this, 290 failed
        # attempts read as a slow colony.
        task['_pipelineFailedAt'] = int(time.time() * 1000)
        try:
            from serve import log_action
            log_action(agent_id, 'task_auto_closed',
                       {'taskId': task.get('id'), 'title': task.get('title'),
                        'reason': (task.get('note') or 'quality pipeline failed')[:300],
                        'stage': 'content_pipeline'}, authorized=True)
        except Exception:
            pass
        _notify_card_failure(
            state, task, task.get('note') or 'quality pipeline failed',
            category='auto-closed', now_ms=int(time.time() * 1000))
    a['task'] = None
    a['busy'] = False
    a['inRoom'] = None
    a['visible'] = True
