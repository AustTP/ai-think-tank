import re


def _start_breakdown(state, team_id, req_ids, scrum_master_id, now_ms, pos_offset=0):
    """Convene the breakdown ceremony at the Command Center: snapshot each
    attendee's prior state into `pending['people']`, mark them busy at a spread,
    and schedule the resolve BREAKDOWN_MEET_MS later. Returns True if convened
    (attendees healthy), False to defer and retry next pass."""
    ids = _breakdown_attendees(state, req_ids, scrum_master_id, team_id)
    if not ids:
        return False
    agents = state.get('agents') or {}
    snap = {}
    base_x = 315 + (pos_offset * 460)
    for i, aid in enumerate(ids):
        a = agents.get(aid)
        snap[aid] = {k: a.get(k) for k in ('offDuty', 'visible', 'x', 'y', 'dir',
                                           'task', 'busy', 'inRoom', 'pairWith', 'handoff', 'workUntil')}
        a['busy'] = True
        a['visible'] = True
        a['offDuty'] = False
        a['task'] = None
        a['inRoom'] = 'commandcenter'
        a['dir'] = 'south'
        a['roomX'] = base_x + (i * 85)
        a['roomY'] = 155
    state.setdefault('pendingBreakdowns', {})[team_id] = {
        'at': now_ms + BREAKDOWN_MEET_MS, 'embarked': True,
        'scrumMasterId': scrum_master_id, 'reqIds': req_ids,
        'teamId': team_id, 'people': snap,
    }
    _log_governance(state, scrum_master_id, 'breakdown',
                    {'action': 'meeting_start', 'requests': req_ids})
    return True


def _breakdown_decider_default(state, instructions, goal):
    """Default resolver for the breakdown ceremony: a high-tier (JEV-gated)
    chat call returns a JSON list of stories/spikes for the ask, accrued to its
    own spend bucket. Returns the parsed dict, or None on a model outage (the
    resolve then queues a single card titled the goal -- the ask is never
    dropped)."""
    try:
        import serve
    except Exception:
        return None
    try:
        model = serve._resolve_model_tier(
            f'Breaking a large request into worker-sized stories: {goal[:200]}',
            allow_high=True)
        if not model:
            return None
        data = serve._call_openrouter_sync(
            model,
            [{'role': 'system', 'content': instructions},
             {'role': 'user', 'content': f"Break down this large request: {goal}"}],
            max_tokens=serve._big_task_max_tokens)
        cost = (data.get('usage') or {}).get('cost', 0.0)
        if isinstance(cost, (int, float)) and cost:
            serve._accrue_spend('__breakdowns__', cost)
        reply = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        start = reply.find('{')
        end = reply.rfind('}')
        if start != -1 and end > start:
            reply = reply[start:end + 1]
        parsed = json.loads(reply)
        if isinstance(parsed, dict) and (parsed.get('items') or []):
            return parsed
    except Exception:
        pass
    return None


# Injectable for tests (mirrors _refinement_decider / _retro_decider); the live
# loop uses the high-tier default. The breakdown NEVER blocks the staffing reply
# -- the ask is filed pending and carded on a later pass, so an outage costs
# nothing but a single card titled the goal.
_breakdown_decider = _breakdown_decider_default


_SINGLE_DELIVERABLE_ARTIFACTS = (
    'brief', 'memo', 'report', 'write-up', 'writeup', 'one-page', 'one page',
    'summary', 'recommendation',
)
_SINGLE_DELIVERABLE_VERBS = ('write', 'draft', 'prepare', 'research', 'summarize', 'save')
_SINGLE_DELIVERABLE_MULTI = (
    'multiple', 'separate', 'series of', 'two reports', 'three reports',
    'and also', 'as well as',
)
_SINGLE_DELIVERABLE_CODE = (
    'implement', 'fix ', 'bug', 'code change', 'commit', 'endpoint', 'api',
    'function', 'class', 'unit test',
)


_REQUESTED_PATH_RE = re.compile(
    r'(?:under|at|to|as|in)\s+[`"\']?([A-Za-z0-9_./-]+\.'
    r'(?:md|txt|json|csv|py|js|html))', re.I)


def _requested_library_path(goal):
    """The explicit Library path a goal names, if any. A goal that says
    "save it under admin/x.md" must keep that path through the breakdown."""
    match = _REQUESTED_PATH_RE.search(str(goal or ''))
    if not match:
        return None
    path = match.group(1).lstrip('/')
    if '..' in path.split('/'):
        return None
    return path


def _single_deliverable_goal(goal):
    """True when the goal names ONE written artifact. Such a goal must be one
    report card, not a gather/write/save/verify chain that can run in parallel.
    """
    text = str(goal or '').lower()
    if not any(word in text for word in _SINGLE_DELIVERABLE_ARTIFACTS):
        return False
    if not any(verb in text for verb in _SINGLE_DELIVERABLE_VERBS):
        return False
    if any(word in text for word in _SINGLE_DELIVERABLE_MULTI):
        return False
    if any(word in text for word in _SINGLE_DELIVERABLE_CODE):
        return False
    return True


def _single_deliverable_item(goal, constraints):
    """One report card carrying the whole goal. The report lane already gathers
    sources and writes the file; splitting that into gather/write/save/verify
    cards was the 2026-10-26 real-task failure."""
    text = str(goal or '').lower()
    task_type = 'report' if any(
        word in text for word in _SINGLE_DELIVERABLE_ARTIFACTS) else 'code'
    budget = None
    raw_budget = (constraints or {}).get('budgetUsd')
    if isinstance(raw_budget, (int, float)) and raw_budget > 0:
        budget = float(raw_budget)
    return {
        'title': str(goal or 'untitled request').strip()[:120],
        'type': task_type,
        'acceptanceCriteria': ('One deliverable at the path named in the goal, '
                               'with the requested sources and recommendation.'),
        'sizeEstimate': 'M',
        'budgetUsd': budget,
        'reportPath': _requested_library_path(goal),
        'singleDeliverable': True,
    }


def _resolve_breakdown(state, pending, now_ms, decider=None):
    """When the breakdown window elapses, the scrum master cards each pending
    large-request into concrete stories/spikes via the injectable decider; the
    pieces are queued as REAL work tagged with the receiving team + the goal
    (room-less -- the agent who picks the work up resolves the room at
    assignment, exactly like a shared-backlog card). On an outage a single
    pressoffice card titled the goal is queued so the ask is never silently
    dropped. Restores attendees, records the carryaway, pops the pending
    record. Idempotent if a request vanished."""
    decider = decider or _breakdown_decider
    agents = state.get('agents') or {}
    scrum_master_id = pending.get('scrumMasterId')
    sm_def = next((d for d in (state.get('agentRoster') or []) if d.get('id') == scrum_master_id), {})
    sm_name = sm_def.get('name') or (agents.get(scrum_master_id) or {}).get('name') or scrum_master_id
    team_id = pending.get('teamId')
    for req in (state.get('backlogRequests') or []):
        if req.get('id') not in pending.get('reqIds', []):
            continue
        if req.get('status') != 'pending':
            continue
        goal = req.get('goal') or req.get('title') or 'untitled large request'
        author = agents.get(req.get('filedBy')) or {}
        pc = req.get('playerConstraints') or {}
        # Player pins are CONTEXT for the scrum master's judgment, not
        # mechanical overrides. They're woven into the instructions the same
        # way the goal itself is -- the model weighs them like any other signal.
        pin_bits = []
        if pc.get('budgetUsd'):
            pin_bits.append(f"The player named a total budget of ${pc['budgetUsd']:.2f} "
                            "for this whole ask. Allocate it across the cards with judgment "
                            "-- larger/riskier cards may deserve more, trivial ones less. "
                            "Set each card's \"budgetUsd\" field to its allocated share "
                            "(a positive number).")
        if pc.get('constraints'):
            pin_bits.append('The player listed these hard prohibitions the team must respect: '
                            + '; '.join(pc['constraints']) + '.')
        if pc.get('roles'):
            pin_bits.append('The player prefers cards suited to these roles/skills: '
                            + '; '.join(pc['roles']) + '.')
        if pc.get('order'):
            pin_bits.append('The player wants this rough sequencing (earlier items '
                            'should be planned before later ones): '
                            + ' -> '.join(pc['order']) + '.')
        if pc.get('mustInclude'):
            pin_bits.append('The player named these tasks as important to cover '
                            '(factor them into the plan): '
                            + '; '.join(pc['mustInclude']) + '.')
        pin_text = (' ' + ' '.join(pin_bits)) if pin_bits else ''
        # When the player named a budget, ask the model to return per-card
        # budgetUsd so its judgment (not a mechanical split) allocates the pot.
        budget_field = '"budgetUsd":number-or-omit,' if pc.get('budgetUsd') else ''
        instructions = (
            f"{sm_name} is the scrum master of the receiving team breaking down a large request "
            f"filed by {author.get('name') or req.get('filedBy')}: '{goal[:200]}'.{pin_text} "
            "Break it into as many concrete stories (or spikes for investigation-first work) as the work actually "
            "requires -- at least one, never a fixed count: a small ask may be a single card, a sprawling one may need many. "
            "Do NOT assign a room to a card -- the buildings are shared and the agent who picks the work up figures out "
            "where it needs to happen. Keep every title to ONE short sentence. For each card set \"type\" to "
            "\"story\" (code or repo deliverable work), \"spike\" (an investigation with no committed deliverable), "
            "\"research\" (a fact-gathering/analysis sweep that produces findings, not a shipped deliverable -- "
            "document studies, competitive teardown, desk research), or "
            "\"report\" (a written deliverable, analysis, or recommendation that does NOT change the repo -- use "
            "this instead of story for a memo, review write-up, or recommendation), "
            "a one-line \"acceptanceCriteria\" when the story has a clear test of done, a \"never\" list "
            "ONLY when the card has hard prohibitions the worker must never do (omit otherwise), and a "
            "\"sizeEstimate\" of S/M/L. "
            "Never use story for a prose report or recommendation; use report. "
            "Respond with ONLY valid JSON, no other text, no markdown fences, in exactly this shape: "
            '{"items":[{"title":"short title","type":"story|spike|research|report","acceptanceCriteria":"one line or omitted","never":["one hard constraint or omit"],"sizeEstimate":"S|M|L",' + budget_field + '}]}'
        )
        template = _workflow_match_template(state, goal, team_id)
        if template:
            items = _workflow_template_items(template, goal, pc)
            req['workflowTemplateId'] = template.get('id')
            req['workflowTemplateOwnerTeam'] = template.get('ownerTeamId')
            req['workflowTemplateUsedByTeam'] = team_id
            template['useCount'] = int(template.get('useCount') or 0) + 1
            template['lastUsedAt'] = now_ms
            _log_governance(state, scrum_master_id, 'workflow_template_used',
                            {'request': req.get('id'), 'templateId': template.get('id'),
                             'ownerTeam': template.get('ownerTeamId'),
                             'usingTeam': team_id, 'steps': len(items)})
        elif _single_deliverable_goal(goal):
            items = [_single_deliverable_item(goal, pc)]
            _log_governance(state, scrum_master_id, 'breakdown_single_deliverable',
                            {'request': req.get('id'), 'goal': goal[:120],
                             'type': items[0]['type']})
        else:
            data = decider(state, instructions, goal)
            items = []
            if isinstance(data, dict):
                items = [s for s in (data.get('items') or [])
                         if isinstance(s, dict) and (s.get('title') or '').strip()]
            if not items:
                # Outage / unusable plan: never drop the ask -- queue a single card
                # titled the goal so it stays in the pipeline for a worker to pick up.
                items = [{'title': goal[:120], 'type': 'story'}]
        for seq, s in enumerate(items):
            title = (s.get('title') or '').strip()[:120]
            # Preserve the model's investigation-class type ('spike' or
            # 'research'); a story (or anything unrecognized) becomes 'code'.
            # Without this, a research card was silently rewritten to
            # taskType='code' and got routed into the press-office code
            # pipeline (Jev block + flake8/mypy/pytest) on pure prose -- the
            # exact failure class that stranded the StealthX research cards.
            raw_type = (s.get('type') or 'story').strip().lower()
            # 'report' is prose/recommendation work, not a repo change. It goes
            # to the report lane, which preloads named local sources and falls
            # back to the tool-using spike lane when it has none, so a written
            # recommendation cannot fail on flake8/mypy over the whole repo.
            if raw_type == 'report':
                task_type = 'report'
            elif raw_type in ('spike', 'research'):
                task_type = raw_type
            else:
                task_type = 'code'
            ac = (s.get('acceptanceCriteria') or '').strip()
            never = normalize_never_block(s.get('never'))
            instructions_text = (f"Filed by {req.get('filedBy')} during a large-request breakdown by "
                                 f"{sm_name}. Requested goal: {goal[:2000]}")
            if ac:
                instructions_text += f"\n\nAcceptance criteria:\n{ac}"
            if never:
                instructions_text += f"\n\nNEVER:\n{never}"
            # Budget: when the player named a total, the scrum master was told
            # to allocate it across cards with judgment (see the prompt). Pick
            # up whatever share it assigned; a missing/invalid share means the
            # model chose not to allocate this card, so leave it unbudgeted
            # (type default applies) rather than forcing an even split.
            card_budget = None
            raw_budget = s.get('budgetUsd')
            if isinstance(raw_budget, (int, float)) and raw_budget > 0:
                card_budget = float(raw_budget)
            queue_work(state, [{
                'title': title, 'goal': goal[:200], 'instructions': instructions_text,
                'teamId': req.get('teamId'), 'taskType': task_type,
                # Provenance: every card a breakdown creates points back at the
                # request that asked for it. The A2A facade reads completion back
                # through this link instead of matching titles.
                'requestId': req.get('id'),
                # The decider was told to size each story S/M/L (see the
                # breakdown prompt); carry it so the queued item + task keep the
                # estimate and same-priority work is ordered largest-first.
                'sizeEstimate': s.get('sizeEstimate'),
                'never': never,
                'budgetUsd': card_budget,
                # An explicit path in the goal survives onto the card so the
                # report/spike closer writes the requested file, not a generic
                # archive name.
                'reportPath': s.get('reportPath'),
                'templateId': template.get('id') if template else None,
                # Serial group: cards of one request run in the model's order.
                # The assignment guard waits while an earlier card is queued or
                # active, so gather -> write -> save cannot run in parallel.
                'sequence': seq,
            }])
        req['status'] = 'accepted'
        req['brokenDown'] = True
        req['stories'] = items
        _log_governance(state, scrum_master_id, 'breakdown_carryaway',
                        {'request': req['id'], 'goal': goal[:120], 'stories': len(items)})
    # Restore attendees; like refinement, the meeting burned none of a mid-task
    # attendee's work time (the full ceremony length is given back).
    for aid, snap in (pending.get('people') or {}).items():
        _restore_refinement_agent(state, aid, snap, now_ms, BREAKDOWN_MEET_MS)
    state.setdefault('pendingBreakdowns', {}).pop(team_id, None) if team_id else state.pop('_pendingBreakdown', None)


def _breakdown_step(state, now, now_ms, decider=None):
    """One large-request breakdown pass, called from _task_cycle ungated (a
    staffed large ask is the point even on a quiet think tank). A pass
    (a) advances every in-flight breakdown ceremony (embark on one pass,
    RELEASE on the next -- no required fixed hold; the facilitator is
    RE-RESOLVED at embark so a stored id that has gone busy never blocks), and
    (b) convenes a breakdown for every team that has pending large-request
    breakdowns AND a free+on-duty facilitator (see _ceremony_facilitator) +
    healthy attendees. Never convenes an empty meeting; a team in an active
    sprint is deferred (the sprint is the committed work, and the pending
    breakdown request is carded once the team is free)."""
    pending_map = state.setdefault('pendingBreakdowns', {})
    for team_id in list(pending_map.keys()):
        pending = pending_map[team_id]
        if not pending.get('embarked'):
            facilitator = _ceremony_facilitator(state, team_id)
            if not facilitator:
                continue  # no free+on-duty facilitator right now -- defer a pass
            _start_breakdown(state, team_id, pending.get('reqIds', []),
                             facilitator, now_ms)
        else:
            _resolve_breakdown(state, pending, now_ms, decider=decider)
    slot = 0
    for team in (state.get('teams') or []):
        team_id = team.get('id') or team.get('directorId')
        if team_id in pending_map:
            continue  # this team already has an in-flight breakdown ceremony
        req_ids = [r['id'] for r in _pending_breakdown_requests(state, team_id)]
        if not req_ids:
            continue  # nothing to plan -- don't convene an empty meeting
        if _team_in_active_sprint(state, team_id):
            # The team is committed to a sprint; the pending breakdown request
            # stays queued and is carded once the team is free (same rule as a
            # team in an active sprint not refining mid-sprint).
            continue
        facilitator = _ceremony_facilitator(state, team_id)
        if not facilitator:
            continue  # this team has no free+on-duty facilitator right now
        req_ids = req_ids[:BREAKDOWN_MAX_REQUESTS]
        if _start_breakdown(state, team_id, req_ids, facilitator, now_ms, pos_offset=slot):
            slot += 1  # only to spread simultaneous ceremonies apart in the room


# ---------------------------------------------------------------------------
# WS-14: sprint retrospective (START / STOP / CONTINUE). When a sprint closes,
# its team meets at the Command Center -- the scrum master + the team's
# non-director members, with the DIRECTOR explicitly excluded (a retrospective
# is the team's own reflection, per the player's design) -- and records what to
# start, stop, and keep doing. Same convene-then-resolve ceremony shape as
# backlog refinement; the decider is injectable like every other Jev decision.
# ---------------------------------------------------------------------------


def _retro_scrum_master(state, team_ids):
    """The agent who facilitates a sprint's retrospective: the first team in the
    sprint's team list with a NON-DIRECTOR effective scrum master; else -- for a
    team whose only scrum master is its own director (small teams below
    SCRUM_MASTER_MIN_TEAM_SIZE, per the player's design the director already
    serves as the scrum master) -- that team's OWN director stands in as
    facilitator. The OWN director is preferred over any borrowed one: a retro is
    the team's own reflection and should be run by someone the team actually
    works with, not an outsider. Only when the own director is unavailable
    (busy/off-duty) is a non-busy director from ANOTHER team borrowed in.
    Returns None only when no facilitator can be found at all (the retro then
    waits for a later pass)."""
    teams = {t.get('id'): t for t in (state.get('teams') or [])}
    for tid in team_ids or []:
        sm = _refinement_scrum_master_for_team(state, tid)
        if sm and sm != (teams.get(tid) or {}).get('directorId'):
            return sm
    # No non-director scrum master among the sprint teams: the team's OWN
    # director stands in (a small team's director already serves as its scrum
    # master). Must be free + on-duty to convene; else defer to a later pass.
    for tid in team_ids or []:
        did = (teams.get(tid) or {}).get('directorId')
        if not did:
            continue
        a = (state.get('agents') or {}).get(did)
        if a and not a.get('busy') and not a.get('offDuty'):
            return did
    # Own director busy/off-duty: fall back to borrowing a non-busy director
    # from another team rather than leaving the team without a retro.
    sprint_directors = {(teams.get(tid) or {}).get('directorId') for tid in (team_ids or [])}
    agents = state.get('agents') or {}
    for t in (state.get('teams') or []):
        did = t.get('directorId')
        if not did or did in sprint_directors:
            continue
        a = agents.get(did)
        if a and not a.get('busy') and not a.get('offDuty'):
            return did
    return None


def _retro_attendees(state, team_ids, scrum_master_id):
    """The retrospective's attendees: the scrum master + every non-director
    member of the sprint's teams, with the DIRECTOR excluded. A busy attendee
    defers the whole meeting (never pull an agent out of a live collaboration);
    an off-duty attendee is parked and free, and the convene wakes her. Returns
    the attendee ids, or None if any attendee is busy."""
    agents = state.get('agents') or {}
    ids = [scrum_master_id]
    for tid in team_ids or []:
        t = next((x for x in (state.get('teams') or []) if x.get('id') == tid), None)
        if not t:
            continue
        director_id = t.get('directorId')
        for aid in _sim_direct_reports(state, director_id):
            if aid not in ids and aid != director_id:
                ids.append(aid)
    for aid in ids:
        a = agents.get(aid)
        if not a or a.get('busy'):
            return None
    return ids


def _start_retrospective(state, sprint_id, team_ids, scrum_master_id, now_ms, pos_offset=0):
    """Convene the sprint retrospective at the Command Center: snapshot each
    attendee's prior state into `pending['people']`, mark them busy at a spread,
    and schedule the resolve RETRO_MEET_MS later. Returns True if convened
    (attendees healthy), False to defer and retry next pass."""
    ids = _retro_attendees(state, team_ids, scrum_master_id)
    if not ids:
        return False
    agents = state.get('agents') or {}
    snap = {}
    base_x = 315 + (pos_offset * 460)
    for i, aid in enumerate(ids):
        a = agents.get(aid)
        snap[aid] = {k: a.get(k) for k in ('offDuty', 'visible', 'x', 'y', 'dir',
                                           'task', 'busy', 'inRoom', 'pairWith', 'handoff', 'workUntil')}
        a['busy'] = True
        a['visible'] = True
        a['offDuty'] = False
        a['task'] = None
        a['inRoom'] = 'commandcenter'
        a['dir'] = 'south'
        a['roomX'] = base_x + (i * 85)
        a['roomY'] = 155
    state.setdefault('pendingRetrospectives', {})[sprint_id] = {
        'at': now_ms + RETRO_MEET_MS, 'embarked': True,
        'sprintId': sprint_id, 'scrumMasterId': scrum_master_id,
        'teamIds': team_ids, 'people': snap,
    }
    _log_governance(state, scrum_master_id, 'retrospective',
                    {'action': 'meeting_start', 'sprint': sprint_id})
    return True


def _resolve_retrospective(state, pending, now_ms, decider=None):
    """When the retrospective window elapses, the scrum master records the
    just-closed sprint's START / STOP / CONTINUE reflections via the injectable
    decider, everyone returns to their prior state, and the record lands in
    state['retrospectives'][sprint_id]. Idempotent if the sprint vanished."""
    decider = decider or _retro_decider
    sprint_id = pending.get('sprintId')
    agents = state.get('agents') or {}
    scrum_master_id = pending.get('scrumMasterId')
    scrum_master = (agents.get(scrum_master_id) or {})
    sm_def = next((d for d in (state.get('agentRoster') or []) if d.get('id') == scrum_master_id), {})
    sm_name = sm_def.get('name') or scrum_master.get('name') or scrum_master_id
    record = (state.get('sprints') or {}).get(sprint_id) or {}
    progress = sprint_progress(state, sprint_id) or {}
    landed = progress.get('landed') or []
    landed_text = ', '.join(landed) if landed else '(nothing landed)'
    velocity = record.get('velocity') or {}
    velocity_line = ''
    if velocity:
        pts = ''
        if velocity.get('pointsTotal') is not None:
            pts = (f", {velocity.get('pointsLanded')} of "
                   f"{velocity.get('pointsTotal')} points shipped")
        velocity_line = (f" Sprint velocity: {velocity.get('landed')} of "
                         f"{velocity.get('total')} items landed ({velocity.get('pct')}%), "
                         f"{velocity.get('rolledOver')} carried over{pts}.")
    instructions = (
        f"{sm_name} is the scrum master facilitating the retrospective for the just-closed sprint "
        f"'{record.get('name') or sprint_id}' (goal: {record.get('goal') or 'none'}). "
        f"What landed this sprint: {landed_text}.{velocity_line} "
        "Name concrete, honest START / STOP / CONTINUE items for the team. START = something the team "
        "should begin doing (a new habit). STOP = something that wasted effort or didn't work. "
        "CONTINUE = something that worked and should be kept. 2-4 terse items per bucket; every item must be "
        "a specific behavior the team controls, never a vague compliment. Respond with ONLY valid JSON, no "
        "markdown fences: {\"start\":[\"...\"],\"stop\":[\"...\"],\"continue\":[\"...\"]}"
    )
    data = decider(state, instructions, sprint_id, landed)
    buckets = {'start': [], 'stop': [], 'continue': []}
    if isinstance(data, dict):
        for k in buckets:
            v = data.get(k)
            if isinstance(v, list):
                buckets[k] = [str(x).strip()[:160] for x in v if str(x).strip()][:4]
    # Restore attendees; the meeting burned none of a mid-task attendee's time.
    for aid, snap in (pending.get('people') or {}).items():
        _restore_refinement_agent(state, aid, snap, now_ms, RETRO_MEET_MS)
    state.setdefault('retrospectives', {})[sprint_id] = {
        'id': sprint_id,
        'sprintId': sprint_id,
        'sprintName': record.get('name') or sprint_id,
        'landed': landed,
        'velocity': record.get('velocity') or None,
        'teamIds': pending.get('teamIds') or [],
        'attendees': list((pending.get('people') or {}).keys()),
        'completedAt': now_ms,
        'start': buckets['start'],
        'stop': buckets['stop'],
        'continue': buckets['continue'],
    }
    # The START/STOP/CONTINUE feedback the team just gave is one of the
    # moments the player should hear from the tank (alongside the sprint
    # closing itself). Both channels, with a per-sprint kind so the 30s
    # dedupe can never swallow a second sprint's retro.
    retro = state['retrospectives'][sprint_id]
    feedback = []
    for label, key in (('START', 'start'), ('STOP', 'stop'),
                       ('CONTINUE', 'continue')):
        items = retro.get(key) or []
        if items:
            feedback.append(f'{label}:')
            feedback.extend(f'  - {item}' for item in items)
    if not feedback:
        feedback.append('(no feedback captured)')
    _queue_player_email(
        state, f'sprint_retro:{sprint_id}',
        f'[AI Think Tank] Sprint retrospective: '
        f'{retro.get("sprintName") or sprint_id}',
        (f'Retrospective for sprint "{retro.get("sprintName") or sprint_id}" '
         f'({", ".join(retro.get("teamIds") or []) or "team"}):\n'
         + '\n'.join(feedback)
         + (f'\nLanded: {", ".join(landed)}' if landed
            else '\nLanded: (nothing landed)')),
        now_ms=now_ms)
    _log_governance(state, scrum_master_id, 'retrospective',
                    {'action': 'meeting_end', 'sprint': sprint_id,
                     'start': len(buckets['start']), 'stop': len(buckets['stop']),
                     'continue': len(buckets['continue'])})
    state.setdefault('pendingRetrospectives', {}).pop(sprint_id, None)


def _retro_decider_default(state, instructions, sprint_id, landed):
    """Default resolver for a sprint retrospective: a low-tier chat call that
    returns a START / STOP / CONTINUE JSON object, accrued to its own spend
    bucket. Returns the parsed dict, or an empty dict on a model outage (an
    empty retro is recorded rather than blocking the sprint's close)."""
    try:
        import serve
    except Exception:
        return {}
    try:
        model = serve._low_tier_slug()
        data = serve._call_openrouter_sync(
            model,
            [{'role': 'system', 'content': instructions},
             {'role': 'user', 'content': f"Run the retrospective for sprint {sprint_id}."}],
            max_tokens=200)
        cost = (data.get('usage') or {}).get('cost', 0.0)
        if isinstance(cost, (int, float)) and cost:
            serve._accrue_spend('__retrospectives__', cost)
        reply = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        start = reply.find('{')
        end = reply.rfind('}')
        if start != -1 and end > start:
            reply = reply[start:end + 1]
        parsed = json.loads(reply)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


# Injectable for tests (mirrors _refinement_decider / _governance_decider); the
# live loop uses the low-tier default. The retro NEVER holds the sprint's close
# -- it is a ceremony on a later pass, so an outage costs nothing but the text.
_retro_decider = _retro_decider_default


def _retro_step(state, now, now_ms, decider=None):
    """One sprint-retrospective pass, called from _task_cycle ungated (the retro
    is the point even on a quiet think tank, and must not be starved by the work
    gate). A pass (a) advances every in-flight retrospective (embark on one pass,
    RELEASE on the next -- same no-required-wait as refinement), and (b) convenes
    a retrospective for every sprint queued in pendingSprintRetros whose team has
    a non-director scrum master + healthy attendees; a sprint whose team isn't
    ready yet stays queued for the next pass."""
    pending_map = state.setdefault('pendingRetrospectives', {})
    for sid in list(pending_map.keys()):
        pending = pending_map[sid]
        if not pending.get('embarked'):
            _start_retrospective(state, sid, pending.get('teamIds', []),
                                 pending.get('scrumMasterId'), now_ms)
        else:
            _resolve_retrospective(state, pending, now_ms, decider=decider)
    slot = 0
    queued = state.get('pendingSprintRetros') or []
    for sid in list(queued):
        if sid in pending_map:
            continue  # this sprint already has an in-flight retrospective
        record = (state.get('sprints') or {}).get(sid) or {}
        scrum_master_id = _retro_scrum_master(state, record.get('teamIds') or [])
        if not scrum_master_id:
            continue  # no facilitator available (own director busy, no other free director) -- retro waits
        team_ids = [t for t in (record.get('teamIds') or []) if _team_row(state, t)]
        if _start_retrospective(state, sid, team_ids, scrum_master_id, now_ms, pos_offset=slot):
            queued.remove(sid)
            slot += 1


# ---------------------------------------------------------------------------
# Sprint failure review. A just-closed sprint usually has cards that FAILED --
# a red pipeline, a review that never converged, an infra timeout. Nobody reads
# those after the retro, so the same hole is discovered again next sprint. At
# close the owning DIRECTOR now reads every failed card, names the cause, and
# -- when the cause is a bug, not infrastructure or process -- files a bug card
# and puts it straight on the team's on-call (or any free teammate when the
# on-call is already carrying work). The review is state-only and never blocks
# the sprint close: the close queues it and a later pass resolves it.
# ---------------------------------------------------------------------------
FAILURE_REVIEW_DELAY_MS = 0
FAILURE_CAUSES = ('bug', 'infra', 'process', 'unknown')


def _sprint_failure_ident(task):
    return ((task.get('title') or '').strip(), (task.get('room') or '').strip())


def _sprint_failures(state, sprint_id):
    """Every card in `sprint_id` that ended the sprint FAILED, newest failure
    first. A card is in the sprint when its task carries the sprintId or its
    (title, room) pair is one of the sprint's stored items. Excludes shadow
    work and cards already reviewed for this same sprint, so a review can be
    retried after an outage without double-filing."""
    record = (state.get('sprints') or {}).get(sprint_id) or {}
    pairs = {((p[0] or '').strip(), (p[1] or '').strip())
             for p in (record.get('items') or []) if isinstance(p, (list, tuple)) and len(p) == 2}
    failures = []
    for task in (state.get('tasks') or {}).values():
        if not isinstance(task, dict) or task.get('shadow'):
            continue
        if task.get('status') != 'failed':
            continue
        if task.get('sprintId') != sprint_id and _sprint_failure_ident(task) not in pairs:
            continue
        if task.get('_failureReviewSprint') == sprint_id:
            continue
        failures.append({
            'id': task.get('id'),
            'title': task.get('title') or task.get('id') or '',
            'room': task.get('room') or '',
            'taskType': task.get('taskType') or 'code',
            'assignedTo': task.get('assignedTo') or '',
            'failNote': (task.get('failNote') or task.get('note') or '')[:600],
            'infra': bool(task.get('_infraFailure')),
            'failedAt': task.get('failedAt') or task.get('updatedAt') or 0,
        })
    failures.sort(key=lambda f: f.get('failedAt') or 0)
    return failures


def _normalize_failure_cause(value):
    """Map a model's free-text cause onto the four causes the review acts on.
    Infrastructure and process are deliberate non-bugs: they earn a review
    note, never a code card."""
    text = str(value or '').strip().lower()
    if text in FAILURE_CAUSES:
        return text
    if any(k in text for k in ('infra', 'environment', 'network', 'provider',
                               'model call', 'timeout', 'rate limit', 'outage',
                               'sandbox', 'disk', 'unavailable')):
        return 'infra'
    if any(k in text for k in ('bug', 'defect', 'regression', 'code', 'logic',
                               'exception', 'traceback', 'crash', 'flaky')):
        return 'bug'
    if any(k in text for k in ('process', 'spec', 'scope', 'instruction',
                               'coordination', 'review', 'unclear')):
        return 'process'
    return 'unknown'


def _failure_review_director(state, team_ids):
    """The director who owns the review: the first sprint team with a live,
    non-busy director. Off-duty is allowed -- this is end-of-sprint paperwork,
    not a meeting. Returns None when every director is mid-task; the review
    then waits for a later pass rather than pinning a busy director."""
    teams = {t.get('id'): t for t in (state.get('teams') or [])}
    agents = state.get('agents') or {}
    for tid in team_ids or []:
        did = (teams.get(tid) or {}).get('directorId')
        a = agents.get(did)
        if did and a and not a.get('busy'):
            return did
    return None


def _failure_fix_assignee(state, team_id, sprint_id, now_ms):
    """Who works the bug card: the team's on-call when she is available. If she
    is already mid-work, any free on-duty teammate may take it (the sprint has
    moved on and the bug should not wait on one rotation slot). Falls back to
    the on-call pin when nobody is free, so the card still names an owner."""
    oc = on_call_agent(state, team_id, sprint_id, now_ms)
    agents = state.get('agents') or {}

    def free(aid):
        a = agents.get(aid)
        return bool(a) and not a.get('busy') and not a.get('offDuty')

    if oc and free(oc):
        return oc
    for aid in _team_oncall_members(state, team_id):
        if aid != oc and free(aid):
            return aid
    return oc


def _file_failure_bug(state, sprint_id, team_id, director_id, failure, finding, now_ms):
    """File the backlog bug for a failure the director judged a real bug and
    queue it as an incident pinned to the fix assignee. Returns the issue, or
    None when a live card with the same title already exists (dedupe)."""
    why = str(finding.get('why') or finding.get('rootCause') or '').strip()[:600]
    base = (failure.get('title') or failure.get('id') or 'failed card').strip()
    title = f"Bug: {base}"[:140]
    norm = lambda text: re.sub(r'\s+', ' ', str(text or '').strip().lower())
    for issue in (state.get('issues') or {}).values():
        if (issue.get('teamId') == team_id
                and issue.get('status') in ('open', 'in_progress')
                and norm(issue.get('title')) == norm(title)):
            return None
    for task in (state.get('tasks') or {}).values():
        if (task.get('teamId') == team_id
                and task.get('status') not in ('done', 'cancelled', 'abandoned')
                and norm(task.get('title')) == norm(title)):
            return None
    summary = f"Fix the bug behind {base}"[:200]
    user_story = (f"As the team, I want to remove the bug behind "
                  f"'{base[:120]}', so that this failure cannot recur next sprint.")
    criteria = ("Given the original failure, when the fix lands, then a regression "
                "test reproduces it and passes, and the original card can be "
                "retried without the same error.")
    never = ('Do not close without reproducing the original failure\n'
             'Do not change unrelated files')
    issue = file_issue(state, team_id, 'bug', summary, 'Reliability', director_id,
                       description={'userStory': user_story,
                                    'acceptanceCriteria': criteria,
                                    'never': never.split('\n')},
                       title=title, now_ms=now_ms, story_points=2)
    if not issue:
        return None
    issue['rootCause'] = why or None
    issue['failureOf'] = failure.get('id')
    issue['sprintId'] = sprint_id
    for req in (state.get('backlogRequests') or []):
        if req.get('issueKey') == issue['key'] and req.get('status') == 'pending':
            # The director is placing this card directly, so refinement must
            # not groom it a second time.
            req['status'] = 'accepted'
            req['acceptedAt'] = now_ms
            req['acceptedBy'] = director_id
            break
    assignee = _failure_fix_assignee(state, team_id, sprint_id, now_ms)
    queue_work(state, [{
        'title': f"[BUG] {base}"[:140],
        'room': failure.get('room') or 'pressoffice',
        'taskType': 'bug',
        'assignedTo': assignee,
        'directRoute': True,
        'incident': True,
        'priority': 'high',
        'teamId': team_id,
        'issueKey': issue['key'],
        'userStory': user_story,
        'acceptanceCriteria': criteria,
        'never': never,
        'sizeEstimate': 'S',
        'instructions': (
            f"SPRINT FAILURE REVIEW: {failure.get('title')} failed during sprint "
            f"{sprint_id}. The director judged the cause a BUG: {why or 'see the failed card'}. "
            f"Fix the underlying cause with a regression test, then report the "
            f"exact result. This card is the backstop against the same failure "
            f"next sprint."),
    }])
    issue['fixAssignee'] = assignee
    return issue


def _failure_review_decider_default(state, instructions, sprint_id, failures):
    """Default resolver for a sprint failure review: a low-tier chat call that
    classifies each failure. Returns a dict with a `findings` list, or {} on a
    model outage (the deterministic fallback still reviews every card, using
    the infra flag and an 'unknown' cause)."""
    try:
        import serve
    except Exception:
        return {}
    try:
        model = serve._low_tier_slug()
        data = serve._call_openrouter_sync(
            model,
            [{'role': 'system', 'content': instructions},
             {'role': 'user', 'content': f"Review the failures from sprint {sprint_id}."}],
            max_tokens=900)
        cost = (data.get('usage') or {}).get('cost', 0.0)
        if isinstance(cost, (int, float)) and cost:
            serve._accrue_spend('__failure_reviews__', cost)
        reply = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        start = reply.find('{')
        end = reply.rfind('}')
        if start != -1 and end > start:
            reply = reply[start:end + 1]
        parsed = json.loads(reply)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


# Injectable for tests, like _retro_decider. The review is a ceremony on a
# later pass, so an outage costs the classification, never the sprint close.
_failure_review_decider = _failure_review_decider_default


def _failure_review_findings(data, failures):
    """One finding per failure, in the failures' order. Accepts the decider's
    `findings` list (matched by taskId, else by position) or a bare list. A
    missing or unknown cause falls back to infra when the card was already
    flagged as an infra failure, else 'unknown' -- never silently a bug."""
    raw = data.get('findings') if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raw = []
    by_id = {}
    for entry in raw:
        if isinstance(entry, dict):
            tid = entry.get('taskId') or entry.get('id')
            if tid:
                by_id[str(tid)] = entry
    findings = []
    for i, failure in enumerate(failures):
        entry = by_id.get(str(failure.get('id'))) or (raw[i] if i < len(raw) and isinstance(raw[i], dict) else {})
        cause = _normalize_failure_cause(entry.get('cause'))
        if not entry.get('cause'):
            cause = 'infra' if failure.get('infra') else 'unknown'
        findings.append({
            'taskId': failure.get('id'),
            'title': failure.get('title'),
            'cause': cause,
            'why': str(entry.get('why') or entry.get('rootCause') or '').strip()[:800],
            'prevention': str(entry.get('prevention') or '').strip()[:400],
        })
    return findings


def _failure_review_step(state, now, now_ms, decider=None):
    """One sprint-failure-review pass, called from _task_cycle next to the retro.
    Each queued review waits for a free owning director, classifies that
    sprint's failed cards, files a bug card for every real bug, stamps the
    review onto the failed tasks, and records the whole review in
    state['failureReviews'][sprint_id]. A sprint with no failures records an
    empty review (no model call)."""
    decider = decider or _failure_review_decider
    pending = state.setdefault('pendingFailureReviews', {})
    for sid in list(pending.keys()):
        item = pending.get(sid) or {}
        if int(item.get('at') or 0) > now_ms:
            continue
        record = (state.get('sprints') or {}).get(sid)
        if not record:
            pending.pop(sid, None)
            continue
        team_ids = [t for t in (item.get('teamIds') or record.get('teamIds') or [])
                    if _team_row(state, t)]
        failures = _sprint_failures(state, sid)
        director = _failure_review_director(state, team_ids)
        if failures and not director:
            continue  # every owning director is mid-task; retry next pass
        findings, filed = [], []
        if failures:
            instructions = (
                "You are the owning director reviewing the failures of the sprint "
                f"'{record.get('name') or sid}'. For each failed card, name the ROOT "
                "CAUSE and classify it as exactly one of: bug (a defect in code, "
                "tests, or the deliverable), infra (provider, network, sandbox, "
                "timeout, outage), process (unclear spec, scope, instructions, "
                "review or coordination), unknown. A bug earns a permanent code "
                "card; infra and process earn a note, never a card. Cards:\n"
                + '\n'.join(
                    f"- taskId {f.get('id')} | {f.get('title')} | type {f.get('taskType')} "
                    f"| owner {f.get('assignedTo')} | infraFlag {f.get('infra')} "
                    f"| failure: {f.get('failNote') or '(no note)'}"
                    for f in failures)
                + "\nRespond with ONLY valid JSON, no markdown fences: "
                  '{"findings":[{"taskId":"...","cause":"bug","why":"...",'
                  '"prevention":"..."}]}'
            )
            data = decider(state, instructions, sid, failures)
            findings = _failure_review_findings(data, failures)
            for failure, finding in zip(failures, findings):
                stamp = now_ms
                task = (state.get('tasks') or {}).get(failure.get('id'))
                if isinstance(task, dict):
                    task['_failureReviewSprint'] = sid
                    task['_failureReviewedAt'] = stamp
                    task['_failureReview'] = {'cause': finding['cause'],
                                              'why': finding['why'][:300]}
                if finding['cause'] == 'bug' and director and team_ids:
                    issue = _file_failure_bug(state, sid, team_ids[0], director,
                                              failure, finding, now_ms)
                    if issue:
                        filed.append(issue['key'])
        state.setdefault('failureReviews', {})[sid] = {
            'sprintId': sid,
            'teamIds': team_ids,
            'directorId': director,
            'reviewedAt': now_ms,
            'failures': findings,
            'filedBugs': filed,
        }
        pending.pop(sid, None)
        _log_governance(state, director or '', 'sprint_failure_review',
                        {'sprint': sid, 'failures': len(failures),
                         'bugs': len(filed)})


# ---------------------------------------------------------------------------
# Supervisor brief. A director's job during a sprint is facilitation, not the
# work: watch the team's budget burn, watch process failures, and keep the
# sprint moving. The brief gives each director the scoped facts they need
# (own team only) and the exact commands to inspect status, files, sandbox
# paths and the DB. It is mail, not a task: no model spend, no queue slot, and
# it lands before the director's next turn.
# ---------------------------------------------------------------------------
SUPERVISOR_BRIEF_INTERVAL_MS = 15 * 60 * 1000
SUPERVISOR_CARD_INTERVAL_MS = 30 * 60 * 1000
SUPERVISOR_CARD_BUDGET_USD = 0.05
CHIEF_MONITOR_INTERVAL_MS = 60 * 60 * 1000  # routine cross-team sweep, attention still wins

# Defined after the module's main bind_policy() call; bind_policy merges these
# into the existing sim pairs so an operator reload rebinds them too.
bind_policy(__name__, [
    ('SUPERVISOR_BRIEF_INTERVAL_MS', 'supervisorBriefIntervalMs'),
    ('SUPERVISOR_CARD_INTERVAL_MS', 'supervisorCardIntervalMs'),
    ('CHIEF_MONITOR_INTERVAL_MS', 'chiefMonitorIntervalMs'),
    ('SUPERVISOR_CARD_BUDGET_USD', 'supervisorCardBudgetUsd'),
])


def _supervisor_team_tasks(state, team_id):
    """Live tasks belonging to `team_id` (its members or its teamId), newest
    first. The scope boundary for every supervisor view."""
    team = _team_row(state, team_id)
    members = set((team or {}).get('members') or [])
    members.update(_sim_direct_reports(state, (team or {}).get('directorId')))
    out = []
    for task in (state.get('tasks') or {}).values():
        if not isinstance(task, dict) or task.get('shadow'):
            continue
        if task.get('teamId') == team_id or task.get('assignedTo') in members:
            out.append(task)
    out.sort(key=lambda t: t.get('updatedAt') or t.get('lastProgressAt') or 0, reverse=True)
    return out
