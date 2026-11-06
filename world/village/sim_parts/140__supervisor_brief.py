

def _supervisor_brief(state, team_id, now_ms=None):
    """The scoped monitoring brief for one team's director. Returns a dict
    with `text` (mail body) and `teamId`, or None for an unknown team."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    team = _team_row(state, team_id)
    if not team:
        return None
    members = list(team.get('members') or [])
    tasks = _supervisor_team_tasks(state, team_id)
    sprint = None
    for rec in (state.get('sprints') or {}).values():
        if rec.get('status') == 'active' and team_id in (rec.get('teamIds') or []):
            sprint = rec
            break
    lines = [f"SUPERVISOR BRIEF: {team.get('name') or team_id} ({team_id})", '',
             f"Your team: {', '.join(members) or '(no workers)'}.",
             "Scope: this team only. Do not inspect, comment on, or reassign another team's work.",
             "Role: facilitate. You watch budget and process failures; the workers do the work.",
             '']
    try:
        import serve as _serve
        missing_avatars = [m for m in members if not _serve._avatar_set_complete(m)]
    except Exception:
        missing_avatars = []
    if missing_avatars:
        lines.append('Avatar images missing: ' + ', '.join(missing_avatars) + '.')
        lines.append('  - If PixelLab is available, call generate_agent_avatar for each '
                     'missing worker. The four images belong in that agent\'s own directory.')
        lines.append('')
    if sprint:
        lines.append(f"Sprint: {sprint.get('name') or sprint.get('id')} ({sprint.get('id')}) -- "
                     f"{sprint.get('goal') or 'no goal set'}")
    else:
        lines.append('Sprint: none active for this team.')
    lines.append('')
    lines.append('Cards:')
    if not tasks:
        lines.append('  (none live)')
    for t in tasks[:20]:
        spend = t.get('ownSpendUsd')
        budget = t.get('budgetUsd')
        money = ''
        if budget or spend:
            money = f" -- spend ${float(spend or 0):.4f} / budget ${float(budget or 0):.2f}"
            if budget and float(spend or 0) >= float(budget) * 0.9:
                money += ' -- AT/OVER BUDGET'
        lines.append(f"  - {t.get('id')} [{t.get('status')}] {t.get('title') or ''}"
                     f" -- owner {t.get('assignedTo') or '?'}{money}")
    failures = [t for t in tasks if t.get('status') in ('failed', 'blocked')]
    lines.append('')
    lines.append('Process failures:')
    if not failures:
        lines.append('  (none)')
    for t in failures[:10]:
        note = (t.get('failNote') or t.get('note') or '')[:200]
        lines.append(f"  - {t.get('id')} {t.get('status')}: {t.get('title') or ''} -- {note}")
    abandoned = _recent_abandoned(state, team_id, now_ms)
    lines.append('')
    lines.append('Recently abandoned:')
    if not abandoned:
        lines.append('  (none)')
    for item in abandoned[:10]:
        lines.append(f"  - {item.get('title') or 'untitled'} "
                     f"[{item.get('taskType') or 'task'}] "
                     f"attempts {item.get('attempts') or 0}")
    stuck = [t for t in tasks if t.get('status') == 'needs_review'
             and now_ms - int(t.get('updatedAt') or now_ms) > 30 * 60 * 1000]
    if stuck:
        lines.append('  stuck in review over 30 min: ' + ', '.join(t.get('id') for t in stuck[:10]))
    lines.append('')
    lines.append('Where to look:')
    lines.append("  - Live status: call the supervisor_report tool first (fresh, read-only, your team only).")
    lines.append("  - Files/sandbox: use execute_script to ls/cat the paths below. The sandbox has no network and no database.")
    lines.append("  - Repo shell only (tasks live in the state JSON, filtered to your team):")
    lines.append("    sqlite3 " + os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'think_tank.db'))
                 + " \"select json_extract(value,'$.id'), json_extract(value,'$.status'), "
                   "json_extract(value,'$.assignedTo'), json_extract(value,'$.ownSpendUsd'), "
                   "json_extract(value,'$.budgetUsd') from kv_state, json_each(blob,'$.tasks') "
                   f"where json_extract(value,'$.teamId')='{team_id}';\"")
    lines.append("  - Sandbox paths for this team's live cards:")
    roots = []
    for t in tasks[:10]:
        for cand in (os.path.join(os.path.dirname(__file__), '..', 'sandboxes', str(t.get('id'))),):
            if os.path.isdir(cand):
                roots.append(cand)
    lines.append('    ' + (', '.join(roots) if roots else '(no task sandboxes on disk yet)'))
    lines.append('')
    lines.append('What to act on:')
    lines.append('  - Budget: if a card used its full budget before close, say what the spend bought and whether more is justified; do not do the work yourself.')
    lines.append('  - Process: a failure, a stuck review, or a missing deliverable is yours to escalate or file as a failure review; do not silently retry.')
    lines.append('  - Other teams: out of scope. Route cross-team issues to the chief of staff.')
    return {'teamId': team_id, 'text': '\n'.join(lines), 'sprintId': (sprint or {}).get('id')}


def _deliver_supervisor_brief(state, team_id, now_ms=None):
    """Append the brief to the team director's mailbox. Never wakes or blocks."""
    team = _team_row(state, team_id)
    director_id = (team or {}).get('directorId')
    agent = (state.get('agents') or {}).get(director_id)
    if not director_id or not agent:
        return False
    brief = _supervisor_brief(state, team_id, now_ms)
    if not brief:
        return False
    _append_mailbox(agent, {
        'kind': 'supervisor_brief',
        'about': team_id,
        'title': f"Supervisor brief: {team.get('name') or team_id}",
        'text': brief['text'],
        'createdAt': int(time.time() * 1000) if now_ms is None else int(now_ms),
    })
    return True


def _first_week_onboard_review_step(state, now_ms):
    """One-time first-week review reminder for a completed onboarding.

    The onboarding record carries `firstWeekReviewAt`; when it comes due, mail
    the reporting director so the hire's first task and work agreement get a
    real check instead of the checklist ending at orientation. State-only and
    idempotent per hire.
    """
    agents = state.get('agents') or {}
    for aid, a in list(agents.items()):
        if not isinstance(a, dict):
            continue
        profile = a.get('profile') or {}
        record = profile.get('onboardingRecord') or {}
        provisioning = record.get('provisioning') or {}
        due = provisioning.get('firstWeekReviewAt')
        if not due or now_ms < due or provisioning.get('firstWeekReviewNotifiedAt'):
            continue
        director_id = record.get('directorId')
        director = agents.get(director_id)
        if isinstance(director, dict):
            _append_mailbox(director, {
                'kind': 'onboarding_review',
                'read': False,
                'ts': now_ms,
                'about': aid,
                'text': (f"First-week review due for {a.get('name') or aid}. "
                         'Check their first claimed task, work agreement, and any '
                         'coaching notes, then record whether they are ready for '
                         'normal work.'),
            })
        provisioning['firstWeekReviewNotifiedAt'] = now_ms
        _log_governance(state, 'admin', 'onboarding_review_due',
                        {'agent': aid, 'director': director_id})


def _team_has_active_workers(state, tid):
    """True when the team has live work: any non-terminal task owned by or
    assigned into the team, or any queued item for it. The operator's
    supervision floor (2026-11-06): a director with active workers gets an
    hourly scan even with no sprint -- the pump churn that went unseen for a
    day ran entirely outside sprints."""
    for t in (state.get('tasks') or {}).values():
        if t.get('status') in ('done', 'cancelled', 'abandoned'):
            continue
        if t.get('teamId') == tid or t.get('assignedTo') in _team_member_ids(state, tid):
            return True
    for q in (state.get('workQueue') or []):
        if q.get('teamId') == tid:
            return True
    return False


def _team_member_ids(state, tid):
    team = next((t for t in (state.get('teams') or []) if t.get('id') == tid), None) or {}
    ids = set()
    for a in (state.get('agents') or {}).values():
        if isinstance(a, dict) and (a.get('teamId') == tid or a.get('id') in (team.get('memberIds') or [])):
            ids.add(a.get('id'))
    return ids


def _supervisor_brief_step(state, now, now_ms):
    """One cadence pass: while a team has an active sprint, mail its director a
    fresh scoped brief every SUPERVISOR_BRIEF_INTERVAL_MS. State-only.

    Operator floor (2026-11-06): a team with active workers but no sprint is
    scanned hourly (SUPERVISOR_IDLE_SCAN_INTERVAL_MS) -- supervision must not
    be sprint-gated, or out-of-sprint churn runs unseen. The sprint cadence is
    unchanged (more frequent while the sprint runs)."""
    if automation_paused(state):
        return
    # The supervisor-of-supervisors metrics refresh hourly regardless of who
    # holds a card this pass.
    _director_supervision_rollup(state, now_ms)
    seen = state.setdefault('_supervisorBriefAt', {})
    for team in (state.get('teams') or []):
        tid = team.get('id')
        if not tid:
            continue
        sprint_active = any(r.get('status') == 'active' and tid in (r.get('teamIds') or [])
                            for r in (state.get('sprints') or {}).values())
        if not sprint_active and not _team_has_active_workers(state, tid):
            continue
        interval = (SUPERVISOR_BRIEF_INTERVAL_MS if sprint_active
                    else SUPERVISOR_IDLE_SCAN_INTERVAL_MS)
        last = int(seen.get(tid) or 0)
        if last and now_ms - last < interval:
            continue
        if _deliver_supervisor_brief(state, tid, now_ms):
            seen[tid] = now_ms
        # A brief is context; a SUPERVISE card is a turn where the director can
        # inspect and act. Queue one only when the team actually needs
        # attention, and at most every 30 minutes, so monitoring is not a
        # background spend.
        reason = _supervisor_attention_reason(state, tid, now_ms)
        if not reason:
            continue
        last_card = int((state.get('_supervisorCardAt') or {}).get(tid) or 0)
        if last_card and now_ms - last_card < SUPERVISOR_CARD_INTERVAL_MS:
            continue
        if _supervisor_card_open(state, tid):
            continue
        director = team.get('directorId')
        if not director or director not in (state.get('agents') or {}):
            continue
        queue_work(state, [{
            'title': f"[SUPERVISE] {team.get('name') or tid}: {reason}"[:120],
            'room': 'commandcenter',
            'taskType': 'supervise',
            'assignedTo': director,
            'teamId': tid,
            'priority': 'high',
            'budgetUsd': SUPERVISOR_CARD_BUDGET_USD,
            'sizeEstimate': 'S',
            'instructions': ('Facilitate this team. Read the supervisor report, inspect only this '
                             "team's files and sandbox, act on budget and process, and report back. "
                             'Do not do the work or edit code.'),
        }])
        state.setdefault('_supervisorCardAt', {})[tid] = now_ms
    # The cross-team oversight card: the CHIEF OF STAFF holds it (operator
    # model, 2026-11-06). The twelve directors stand EQUAL -- there is no
    # senior-most director and there never will be one; the regional VP
    # stands alone with her own instruments and never holds these cards.
    admin = next((d.get('id') for d in (state.get('agentRoster') or [])
                  if d.get('isAdmin')), None)
    holder = admin
    if not holder or holder not in (state.get('agents') or {}):
        return
    reasons = []
    for team in (state.get('teams') or []):
        tid = team.get('id')
        if not tid:
            continue
        reason = _supervisor_attention_reason(state, tid, now_ms)
        if reason:
            reasons.append(f"{team.get('name') or tid}: {reason}")
    unowned = [a for a in _recent_abandoned(state, None, now_ms)
               if not a.get('teamId')]
    if unowned:
        reasons.append(f"unowned: {len(unowned)} abandoned card(s)")
    routine = not reasons
    if routine and not (state.get('teams') or []):
        return
    if _supervisor_admin_card_open(state):
        return
    stamp = '__adminRoutine__' if routine else '__admin__'
    interval = CHIEF_MONITOR_INTERVAL_MS if routine else SUPERVISOR_CARD_INTERVAL_MS
    last_admin = int((state.get('_supervisorCardAt') or {}).get(stamp) or 0)
    if last_admin and now_ms - last_admin < interval:
        return
    if routine:
        # Drained-board invariant (2026-11-05 lesson): an idle tank gets no
        # routine card -- the daily sweep supervises WORK, not emptiness.
        if not (state.get('tasks') or {}) and not (state.get('workQueue') or []):
            return
        title = '[SUPERVISE] Cross-team: routine sweep'
        instructions = (
            'Routine cross-team oversight. Read every team report, look for anything '
            'unusual (failures, stuck reviews, budget pressure, missing deliverables, '
            'missed schedules), and act. When something needs work, instruct the owning '
            'director with direct_backlog to put an immediate high-priority task in that '
            "team's backlog, or delegate a scoped check. Do not do the work or edit code."
        )
        rollup_text = _director_supervision_rollup_text(state)
        if rollup_text:
            instructions += '\n\nTeam metrics (24h):\n' + rollup_text
    else:
        title = f"[SUPERVISE] Cross-team: {len(reasons)} team(s) need attention"
        instructions = (
            'Cross-team oversight. Read the report, inspect the live project and the named '
            'teams, act on budget or process, and route cross-team issues. If something '
            'needs work, instruct the owning director with direct_backlog to put an '
            'immediate high-priority task in that team\'s backlog. Do not do the work or '
            'edit code. Attention: ' + '; '.join(reasons[:5])
        )
    queue_work(state, [{
        'title': title[:120],
        'room': 'commandcenter',
        'taskType': 'supervise',
        'assignedTo': holder,
        'priority': 'high',
        'budgetUsd': SUPERVISOR_CARD_BUDGET_USD,
        'sizeEstimate': 'S',
        'instructions': instructions,
    }])
    state.setdefault('_supervisorCardAt', {})[stamp] = now_ms


SM_TOUCH_CADENCE_MS = 7 * 24 * 60 * 60 * 1000


def _sm_standing_touch(state, now_ms=None):
    """Weekly scrum-master touchpoint for PERMANENT-duty teams (operator
    model, 2026-11-06): a team assigned to standing work has no sprint, and
    the refinement ceremony only convenes on pending requests -- so nothing
    periodic ever re-plans a drifting standing queue. Once a week, a team
    with live cards (the drained-board lesson: never convene over an empty
    board) gets one scrum-master governance card: review the standing queue,
    replan or close wedged standing work, confirm the standing purpose is
    still what the operator asked for."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    queued = 0
    touched = state.setdefault('_smTouchAt', {})
    for team in (state.get('teams') or []):
        tid = team.get('id')
        if not tid:
            continue
        if any(s.get('status') == 'active' and tid in (s.get('teamIds') or [])
               for s in (state.get('sprints') or {}).values()):
            continue  # sprint teams groom at close
        if not _team_has_active_workers(state, tid):
            continue
        if now_ms - int(touched.get(tid) or 0) < SM_TOUCH_CADENCE_MS:
            continue
        sm = team.get('scrumMasterId')
        if not sm or sm not in (state.get('agents') or {}):
            continue
        queue_work(state, [{
            'title': f'[GOVERN] Standing backlog review: {team.get("name") or tid}'[:120],
            'room': team.get('room') or 'commandcenter',
            'taskType': 'govern',
            'assignedTo': sm,
            'teamId': tid,
            'priority': 'normal',
            'budgetUsd': SUPERVISOR_CARD_BUDGET_USD,
            'sizeEstimate': 'S',
            'instructions': (
                'Weekly standing-work touchpoint (no sprint is active for this '
                'team): review every live card, replan or close wedged standing '
                'work, and confirm the standing purpose still matches what the '
                'operator asked for. Direct_backlog the owning director for any '
                'new work you surface. Do not do the work yourself.'),
        }])
        touched[tid] = now_ms
        queued += 1
    return queued


DIRECTOR_ROLLUP_CADENCE_MS = 60 * 60 * 1000


def _director_supervision_rollup(state, now_ms=None):
    """Hourly per-team supervision metrics into state['directorSupervision']
    -- the supervisor-of-supervisors view the operator asked for (2026-11-06):
    open cards, spend, over-budget pressure, failures, and 24h completions
    per team. Pure state; computed from live tasks plus the completed-
    deliverables ledger (done cards are archived out of the live blob, so
    completions are counted from the ledger, mapped to a team through the
    member's roster director pointer). Cheap: O(tasks + ledger tail)."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    last = state.get('lastDirectorRollupAt')
    if last is not None and now_ms - last < DIRECTOR_ROLLUP_CADENCE_MS:
        return state.get('directorSupervision') or {}
    state['lastDirectorRollupAt'] = now_ms
    teams = {t.get('id'): t for t in (state.get('teams') or []) if t.get('id')}
    per = {tid: {'open': 0, 'spendUsd': 0.0, 'overBudget': 0,
                 'failedOrBlocked': 0, 'completed24h': 0,
                 'members': len(team.get('members') or [])}
           for tid, team in teams.items()}
    for t in (state.get('tasks') or {}).values():
        tid = t.get('teamId')
        if tid not in per:
            continue
        status = t.get('status')
        if status in ('done', 'cancelled', 'abandoned'):
            continue
        per[tid]['open'] += 1
        spend = float(t.get('ownSpendUsd') or 0)
        per[tid]['spendUsd'] = round(per[tid]['spendUsd'] + spend, 4)
        budget = float(t.get('budgetUsd') or 0)
        if budget and spend >= budget * 0.9:
            per[tid]['overBudget'] += 1
        if status in ('failed', 'blocked'):
            per[tid]['failedOrBlocked'] += 1
    agent_team = {}
    for r in (state.get('agentRoster') or []):
        if isinstance(r, dict) and r.get('id'):
            agent_team[r['id']] = r.get('director')
    day_ago = now_ms - 24 * 60 * 60 * 1000
    for d in (state.get('completedDeliverables') or []):
        if not isinstance(d, dict):
            continue
        ts = d.get('ts') or d.get('gradedAt') or 0
        if not ts or ts < day_ago:
            continue
        tid = agent_team.get(d.get('agentId'))
        if tid in per:
            per[tid]['completed24h'] += 1
    roll = state.setdefault('directorSupervision', {})
    for tid, metrics in per.items():
        metrics['ts'] = now_ms
        roll[tid] = metrics
    # Teams that vanished leave stale rows behind; drop them.
    for tid in list(roll.keys()):
        if tid not in per:
            roll.pop(tid, None)
    return roll


def _director_supervision_rollup_text(state):
    """One line per team from the last rollup, for the routine sweep card."""
    roll = _director_supervision_rollup(state)
    if not roll:
        return ''
    lines = []
    for tid, m in sorted(roll.items()):
        lines.append(
            f"- {tid}: open {m.get('open', 0)}, 24h done {m.get('completed24h', 0)}, "
            f"failed/blocked {m.get('failedOrBlocked', 0)}, "
            f"spend ${float(m.get('spendUsd') or 0):.2f}"
            + (f", OVER BUDGET x{m['overBudget']}" if m.get('overBudget') else ''))
    return '\n'.join(lines)


def _supervisor_admin_card_open(state):
    prefix = '[SUPERVISE] Cross-team'
    for item in (state.get('workQueue') or []):
        if str(item.get('title') or '').startswith(prefix):
            return True
    for task in (state.get('tasks') or {}).values():
        if (str(task.get('title') or '').startswith(prefix)
                and task.get('status') not in ('done', 'cancelled', 'abandoned')):
            return True
    return False


def _recent_abandoned(state, team_id=None, now_ms=None, window_ms=24 * 60 * 60 * 1000):
    """Recent assignment-abandoned cards, scoped to one team or all teams."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    out = []
    for item in (state.get('_abandonedCards') or []):
        if not isinstance(item, dict):
            continue
        if now_ms - int(item.get('at') or 0) > window_ms:
            continue
        if team_id is not None and item.get('teamId') != team_id:
            continue
        out.append(item)
    return out


def _supervisor_attention_reason(state, team_id, now_ms):
    """Why this team needs a supervisor turn now, or '' when it does not:
    a failure, a review stuck over 30 minutes, a live card at 80%+ of its
    budget, or a recently abandoned card. Cheap and state-only."""
    abandoned = _recent_abandoned(state, team_id, now_ms)
    if abandoned:
        return f"{len(abandoned)} abandoned card(s)"
    for t in _supervisor_team_tasks(state, team_id):
        status = t.get('status')
        if status in ('failed', 'blocked'):
            return f"{t.get('id')} {status}"
        if status == 'needs_review':
            age = now_ms - int(t.get('updatedAt') or t.get('lastProgressAt') or now_ms)
            if age > 30 * 60 * 1000:
                return f"{t.get('id')} stuck in review"
        budget = t.get('budgetUsd')
        spent = t.get('ownSpendUsd')
        if budget and float(spent or 0) >= float(budget) * 0.8:
            return f"{t.get('id')} at budget"
    return ''


def _supervisor_card_open(state, team_id):
    """True when this team already has an open SUPERVISE card, so the cadence
    never stacks duplicate turns."""
    for item in (state.get('workQueue') or []):
        if item.get('taskType') == 'supervise' and item.get('teamId') == team_id:
            return True
    for task in (state.get('tasks') or {}).values():
        if (task.get('taskType') == 'supervise' and task.get('teamId') == team_id
                and task.get('status') not in ('done', 'cancelled', 'abandoned')):
            return True
    return False


# ---------------------------------------------------------------------------
# Cut 2: three missing team processes -- deliverable grading +
# director roadmap, the coaching loop (growth plan -> next task), and incident
# runbooks. All follow the established pattern: injectable Jev decision +
# deterministic outage fallback, fire-and-forget (never block the completion
# path), pure state mutation. Grade + runbook write on COMPLETION; coaching +
# runbook notes are APPENDED to instructions at ASSIGNMENT.
# ---------------------------------------------------------------------------

_GRADE_BUCKETS = {
    '0': 'Useless or broken -- does not meet the spec at all; no real value shipped.',
    '2': 'Barely functional -- major gaps, mostly missing the spec, would need to be redone.',
    '4': 'Weak -- meets a small part of the spec; needs significant rework before it is usable.',
    '6': 'Adequate -- meets the core spec with real rough edges or gaps in polish.',
    '8': 'Solid -- meets the spec well; only minor polish or edge cases remain.',
    '10': 'Excellent -- fully meets the spec, no real gaps, cleanly serves the roadmap.',
}

# Cap on the deliverable evidence embedded in a grade prompt. The work order
# and the deliverable body are the two signals that decide a grade; everything
# else is corroboration, so it gets a smaller slice. Without these caps a
# runaway report would push the prompt past what the decision endpoint accepts
# (tape maxes have already shown 27k-char prompts on other sites).
_GRADE_WORK_ORDER_CAP = 2000
_GRADE_BODY_CAP = 4000
_GRADE_NOTE_CAP = 1500


def _deliverable_evidence_lines(task):
    """The actual deliverable evidence a grader needs, one section per signal
    the task record carries. The original grade prompt carried ONLY the
    deliverable's title -- Jev never saw the work, so every real grade came
    back '0' ("useless") at 0.98+ confidence: from a title alone, '0' IS the
    defensible answer, and the whole coaching/firing loop downstream fed on
    those rubber-stamped zeros. This gathers what the completion path already
    stored on the task (see _apply_content_result): the work order with its
    acceptance criteria, the deliverable text itself, the agent's completion
    note, written files, and where the artifact landed. Returns a list of
    section strings (possibly empty) -- the caller decides what absence means."""
    lines = []
    order = (task.get('instructions') or '').strip()
    if order:
        lines.append('Work order (what this task asked for):\n' + order[:_GRADE_WORK_ORDER_CAP])
    body = (task.get('deliverableText') or '').strip()
    if body:
        lines.append('Deliverable text:\n' + body[:_GRADE_BODY_CAP])
    note = (task.get('note') or '').strip()
    if note:
        lines.append("Agent's completion note:\n" + note[:_GRADE_NOTE_CAP])
    files = task.get('writtenPy') or []
    if files:
        lines.append('Code files written: ' + ', '.join(str(f) for f in files[:20]))
    lib = (task.get('libraryPath') or '').strip()
    if lib:
        lines.append('Library artifact: ' + lib)
    wiki = (task.get('wikiPage') or '').strip()
    if wiki:
        lines.append('Wiki page: ' + wiki)
    urls = task.get('sourceUrls') or []
    if urls:
        lines.append('Sources cited: ' + ', '.join(str(u)[:80] for u in urls[:8]))
    return lines


def _grading_decider_default(state, instructions, task_title, room, task_id=None):
    """Default Jev resolver for grading a landed deliverable (0-10 score).
    Returns a float score or None on a Jev outage (-> deterministic fallback).

    Bug fixed: Jev's real API (POST /api/alpha/decisions) is
    a TYPED MULTIPLE-CHOICE system -- every other real call site in this file
    passes several named options and Jev picks one; it has no "give me a free
    number" mode. The original version here passed exactly ONE option keyed
    '0-10' with instructions asking for "a plain integer" -- Jev can only ever
    hand that single key back verbatim, so `choice` was always the STRING
    '0-10', `isinstance(choice, (int, float))` was always False, and grading
    silently fell back to the deterministic default on every single call --
    every deliverable graded exactly 5.0. Fixed by actually
    giving Jev several real, named score buckets to choose among (matching
    every other working choice call in this file), which restores real
    variance instead of a permanent, invisible fallback.

    `task_id` rides the tape as structured metadata (`_taskRef`, stripped
    before the wire payload like every underscore key) so the judge-
    calibration ledger can join this decision to the task's downstream
    outcome without parsing it out of the prompt text."""
    try:
        import serve
    except Exception:
        return None
    questions = {'choice': {'type': 'choice', 'instructions': instructions,
                            'criteria': dict(_GRADE_BUCKETS)},
                 '_kind': 'grade'}
    if task_id:
        questions['_taskRef'] = str(task_id)
    try:
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}}, questions)
        choice, _, _ = serve._jev_choice(data)
        if choice in _GRADE_BUCKETS:
            return float(choice)
    except Exception:
        pass
    return None


_grading_decider = _grading_decider_default


def _runbook_decider_default(state, instructions, product_id, room, title, agent_id=None):
    """Default resolver for an incident runbook: one line "what broke + how it
    was fixed". Returns a string, or None on a call failure -> fallback.

    Bug fixed: this used to route through Jev's typed
    multiple-choice decision endpoint with a single named option ('summary'),
    the same malformed shape _grading_decider_default had. Jev could only ever
    hand that one key back verbatim -- but here the `isinstance(choice, str)`
    check doesn't reject a bare string, so it silently accepted the literal
    word "summary" as the runbook content on every call (a real, findable bug:
    grep runbooks for the word "summary" verbatim). This was never actually a
    CHOICE task (pick one of several named options) -- it's free-text
    generation, which Jev's decisions endpoint cannot do at all. Fixed by
    routing it through a real /api/chat completion instead, the same
    self-loopback pattern every content executor already uses, attributed to
    the agent who actually handled the incident."""
    try:
        import serve
    except Exception:
        return None
    try:
        base = serve.SELF_BASE_URL
        key = serve.get_or_create_agent_key(agent_id) if agent_id else None
        tier_slug = serve._resolve_model_tier('Write one runbook line: what broke in this incident and how it was fixed')
        if not tier_slug:
            return None
        r = serve._http_json('POST', base, '/api/chat',
                             {'model': tier_slug,
                              'messages': [{'role': 'system', 'content': instructions},
                                           {'role': 'user', 'content': 'Write the runbook line.'}],
                              'max_tokens': 80, 'agentId': agent_id,
                              'service': product_id or '__general__'}, key)
        reply = (r.get('reply') or '').strip() if isinstance(r, dict) and not r.get('error') else ''
        if reply:
            return reply[:300]
    except Exception:
        pass
    return None


_runbook_decider = _runbook_decider_default


_AUXILIARY_DELIVERABLE_PREFIXES = (
    '[AVATAR]', '[SUPERVISE]', '[MEETING]', '[SPIKE]', '[REVIEW]', '[QA]',
)


def _is_auxiliary_deliverable(task):
    """True for a completed task whose note is not a committed deliverable:
    meeting minutes, spikes, reviews/QA, avatars, and supervisor sweeps. These
    used to be graded against the code/report roadmap and recorded as real 0.0
    scores, which then dragged the room's trailing grade to 0.0 and made the
    consensus relay tell every worker to focus on that room. The 0.0 was an
    artifact of the wrong rubric, not a quality signal."""
    ttype = str((task or {}).get('taskType') or '').lower()
    if ttype in ('meeting', 'spike', 'bug', 'review', 'qa'):
        return True
    title = str((task or {}).get('title') or '').strip().upper()
    return any(title.startswith(prefix) for prefix in _AUXILIARY_DELIVERABLE_PREFIXES)


def retire_room_grade_signal(state, room, reason='', now_ms=None):
    """Retire a room's trailing-grade signal without deleting the historical
    grades. Used when the operator decides a 0.0 does not measure anything
    real: the records stay for audit, but the roadmap, consensus relay, and
    fallback grading stop treating that room's mean as a live signal."""
    if not room:
        return
    state.setdefault('retiredGradeSignals', {})[room] = {
        'reason': (reason or 'retired by operator').strip()[:300],
        'at': now_ms or int(time.time() * 1000),
    }
    relay = state.get('consensusRelay') or {}
    if relay.get('room') == room and 'weak trailing grade' in (relay.get('consensus') or ''):
        state.pop('consensusRelay', None)


def _room_trailing_grade(state, room):
    """Trailing-window mean grade for a room's recent completed deliverables
    (the signal the roadmap recompute keys on, and this room's own fallback
    when Jev can't grade a NEW deliverable). Returns a float or None.

    Bug fixed (Hermes Town comparison -- "never invent an
    outcome, keep it visibly unresolved instead"): a fabricated fallback grade
    used to carry no marker distinguishing it from a real Jev judgment, so a
    string of Jev outages could compound -- each fallback's flat/room-mean
    grade fed straight into the NEXT outage's trailing mean, quietly drifting
    the roadmap signal on manufactured data. `gradeIsReal` (see
    _grade_completed_task) lets this exclude fabricated grades from the
    average; legacy rows written before this field existed have no key at
    all and are treated as real (`.get(..., True)`) rather than silently
    dropped, since there's no way to know which they were retroactively."""
    if room in (state.get('retiredGradeSignals') or {}):
        return None
    grades = [d.get('grade') for d in (state.get('completedDeliverables') or [])
              if d.get('room') == room and isinstance(d.get('grade'), (int, float))
              and d.get('gradeIsReal', True)
              and not d.get('gradeSuspect')
              and not _is_auxiliary_deliverable(d)]
    if not grades:
        return None
    return round(sum(grades) / len(grades), 1)


def _roadmap_release_demand(state, room):
    """How much a room has shipped recently -- the 'demand' half of the roadmap
    feedback (rooms that keep delivering stay workstreams; ones that stall on
    weak grades get reprioritized by the director). Count of recent completions."""
    return sum(1 for d in (state.get('completedDeliverables') or [])
               if d.get('room') == room)


def _agent_trailing_grade(state, agent_id):
    """Trailing-window mean grade for ONE agent's real completed deliverables
    (the per-agent health signal the director's team-health review keys on).
    Only real Jev grades count -- fabricated fallbacks (gradeIsReal=False) would
    otherwise pull a weak agent's mean toward 5.0 and hide the signal. A
    gradeSuspect entry (the calibration audit's contradiction flag) is also
    excluded: a suspected grading error is not evidence about the agent until
    it is reconciled. Returns a float or None when the agent has no real
    graded deliverable yet."""
    grades = [d.get('grade') for d in (state.get('completedDeliverables') or [])
              if d.get('agentId') == agent_id and isinstance(d.get('grade'), (int, float))
              and d.get('gradeIsReal', True)
              and not d.get('gradeSuspect')
              and not _is_auxiliary_deliverable(d)]
    if not grades:
        return None
    return round(sum(grades) / len(grades), 1)


def _team_health_review(state, team_ids, now_ms):
    """Director gap 1: team-health review at sprint close. Each director reviews
    the health of the WORKERS under them (never directors -- see the worker-only
    judgment directive): an agent whose real trailing grade sits below the
    delivery floor gets a coaching growth-plan note routed to their NEXT task
    (via the existing _write_growth_plan/_coaching_note_for loop), so a weak
    close turns into changed next execution rather than silent drift. Workers
    with no real graded work yet are not judged. Pure state mutation, no Jev
    spend -- the trailing mean IS the ground truth signal. Idempotent per agent:
    _write_growth_plan dedups by kind, so a repeated low close re-coaches (the
    coaching loop, W5) without spamming identical notes."""
    if not team_ids:
        return 0
    reviewed = 0
    for tid in team_ids:
        team = _team_row(state, tid)
        if not team:
            continue
        director_id = team.get('directorId') or team.get('id')
        _floor = team_policy(state, tid, 'deliverableGradeFloor')
        # Worker-only judgment: a director's own team-health review must never
        # grade another director (or the admin), matching who_needs_review.
        for worker_id in _sim_direct_reports(state, director_id):
            wdef = next((d for d in (state.get('agentRoster') or [])
                         if d.get('id') == worker_id), None)
            if not wdef or wdef.get('isDirector') or wdef.get('isAdmin'):
                continue
            grade = _agent_trailing_grade(state, worker_id)
            if grade is None or grade >= _floor:
                continue
            if _scale_tier_worker(state, worker_id):
                _log_governance(state, 'admin', 'scale_tier_grade_accepted',
                                {'agent': worker_id, 'grade': grade,
                                 'floor': _floor, 'source': 'team_health'})
                continue
            _write_growth_plan(
                state, worker_id, team.get('room') or 'pressoffice',
                'team_health', now_ms,
                f"Sprint close team-health review: trailing grade {grade}/10 is "
                f"below the {_floor:.0f} delivery floor. Focus this "
                f"sprint's work on meeting the spec and room standards end-to-end.",
                repeat=True)
            reviewed += 1
    return reviewed


def _artifact_evidence_present(task):
    """True when the task record carries real ARTIFACT evidence a grader
    could actually have judged -- deliverable text, completion note, written
    files, library/wiki/source pointers -- beyond the work order itself
    (mirror of _deliverable_evidence_lines minus its first section)."""
    if not isinstance(task, dict):
        return False
    return bool((task.get('deliverableText') or '').strip()
                or (task.get('note') or '').strip()
                or task.get('writtenPy')
                or (task.get('libraryPath') or '').strip()
                or (task.get('wikiPage') or '').strip()
                or task.get('sourceUrls'))


GRADE_SUSPECT_CEILING = 1.0


# Background grader (2026-11-01 scale fix): the tick thread never waits on
# the grader's OpenRouter round trip. Same two-phase discipline as
# _content_results (020): the worker fills a locked mailbox, and the NEXT
# _task_cycle pass merges results into state -- the worker never touches
# state, so the tick stays the single state writer.
_pending_grades = {}
_pending_grades_lock = threading.Lock()
_grade_results = {}
_grade_results_lock = threading.Lock()
_grading_worker_started = False
_grading_worker_stop = threading.Event()
# Tests disable the daemon (tests/village/conftest.py) and drive the same
# two-phase contract deterministically via _grade_pending_sync -- a daemon
# racing a test's queue could pop a job between queue and sync drain.
_GRADING_WORKER_DISABLED = False


def _process_grading_batch(jobs):
    """One grader pass: fetch state once, judge each queued job with the
    same _grading_decider the synchronous path used, and post the results to
    the mailbox. Returns False when the batch was requeued (DB unavailable),
    True otherwise. Also the deterministic seam tests use --
    _grade_pending_sync runs this exact logic without the daemon thread."""
    if not jobs:
        return True
    try:
        import serve as _serve_mod
        state = _serve_mod.get_state_from_db()
    except Exception:
        # DB unavailable: requeue the batch; the caller backs off. The
        # fallback grades already recorded keep consumers working meanwhile.
        with _pending_grades_lock:
            for job in jobs:
                _pending_grades.setdefault(job['taskId'], job)
        return False
    for job in jobs:
        try:
            try:
                grade = _grading_decider(
                    state, job['instructions'], job['title'],
                    job['room'], job['taskId'])
            except TypeError:
                # Older injected stubs keep their 3-arg signature; the
                # task ref only enriches the decision tape, so a stub
                # without it grades normally.
                grade = _grading_decider(
                    state, job['instructions'], job['title'],
                    job['room'])
        except Exception:
            grade = None  # outage -> the merge keeps the fallback
        with _grade_results_lock:
            _grade_results[job['taskId']] = {
                'grade': grade, 'agentId': job['agentId'],
                'room': job['room'], 'title': job['title'],
                'artifactEvidence': job.get('artifactEvidence')}
    return True


def _grade_pending_sync(state):
    """Deterministic two-phase drain (the test seam for the background
    grader): synchronously judge every queued job and merge the results
    into `state` -- the exact contract the daemon thread plus _task_cycle's
    merge pass implement, without threads or sleeps."""
    with _pending_grades_lock:
        jobs = list(_pending_grades.values())
        _pending_grades.clear()
    _process_grading_batch(jobs)
    return _merge_completed_grades(state)


def _ensure_grading_worker():
    """Start the background deliverable grader once per process. The worker
    drains queued judgments in small batches (one fresh state fetch per
    batch via _process_grading_batch); an outage posts grade None and the
    merge keeps the already-recorded fallback, exactly as the synchronous
    outage path did."""
    global _grading_worker_started
    if _grading_worker_started or _GRADING_WORKER_DISABLED:
        return
    _grading_worker_started = True

    def _loop():
        while not _grading_worker_stop.is_set():
            with _pending_grades_lock:
                jobs = [_pending_grades.pop(k)
                        for k in list(_pending_grades)[:8]]
            if not jobs:
                if _grading_worker_stop.wait(0.5):
                    return
                continue
            if _process_grading_batch(jobs):
                if _grading_worker_stop.wait(0.05):
                    return  # yield; more work may already be queued
            else:
                if _grading_worker_stop.wait(2.0):
                    return  # requeued batch: back off before retrying

    threading.Thread(target=_loop, daemon=True,
                     name='deliverable-grader').start()


# ---------------------------------------------------------------------------
# Agent long-term memory (2026-11-01): when a deliverable lands and is
# judged, distill what that worker learned into agent_ltm (agents/memory.py)
# so their NEXT task on the same ground recalls it. Same two-phase discipline
# as the grader above: enqueue on the tick, extract off it; a per-agent
# cadence and a note-length floor keep the model spend bounded (one cheap
# tier call per agent per window at most). Tests drive the deterministic
# seam (_agent_memory_pending_sync) with the daemon disabled.

_AGENT_MEMORY_CADENCE_S = 6 * 3600
_AGENT_MEMORY_MIN_NOTE_CHARS = 120
_pending_agent_memory = {}
_pending_agent_memory_lock = threading.Lock()
_agent_memory_worker_started = False
_AGENT_MEMORY_WORKER_DISABLED = False
_agent_last_extract = {}  # agent_id -> last enqueue ts (process-local cadence)


def _maybe_queue_agent_memory(state, task, res, now_ms):
    """Tick-side enqueue guard: cheap checks only, never a model call. A
    task with no substantial report teaches nothing worth a call."""
    if not task or not isinstance(task, dict):
        return
    agent_id = res.get('agentId') or task.get('assignedTo')
    note = str(task.get('note') or '')
    if not agent_id or len(note) < _AGENT_MEMORY_MIN_NOTE_CHARS:
        return
    if agent_id not in (state.get('agents') or {}):
        return
    last = _agent_last_extract.get(agent_id, 0.0)
    if now_ms - last < _AGENT_MEMORY_CADENCE_S * 1000:
        return
    _agent_last_extract[agent_id] = now_ms
    with _pending_agent_memory_lock:
        _pending_agent_memory[task.get('id') or agent_id] = {
            'agentId': agent_id, 'title': res.get('title') or task.get('title'),
            'note': note[:1500], 'room': res.get('room') or task.get('room')}
    _ensure_agent_memory_worker()


def _process_agent_memory_batch(jobs):
    """One extraction pass off the tick thread: distill each queued job
    into agent_ltm. Returns False only if the DB is unreachable (requeue)."""
    if not jobs:
        return True
    try:
        import serve as _serve_mod
        from agents import memory as _memory
        _memory._ensure_tables()
        with _serve_mod._db() as conn:
            conn.execute('SELECT 1')
    except Exception:
        return False
    from agents import memory as _memory
    for job in jobs:
        try:
            _memory.agent_ltm_extract(job['agentId'], job.get('title'),
                                      job.get('note'), job.get('room'))
        except Exception:
            pass  # one bad extraction never blocks the rest
    return True


def _agent_memory_pending_sync():
    """Deterministic seam: drain the queue synchronously (no threads)."""
    with _pending_agent_memory_lock:
        jobs = list(_pending_agent_memory.values())
        _pending_agent_memory.clear()
    _process_agent_memory_batch(jobs)


def _ensure_agent_memory_worker():
    global _agent_memory_worker_started
    if _agent_memory_worker_started or _AGENT_MEMORY_WORKER_DISABLED:
        return
    _agent_memory_worker_started = True

    def _loop():
        while True:
            with _pending_agent_memory_lock:
                jobs = [_pending_agent_memory.pop(k)
                        for k in list(_pending_agent_memory)[:4]]
            if not jobs:
                time.sleep(2.0)
                continue
            if not _process_agent_memory_batch(jobs):
                time.sleep(5.0)  # DB outage: back off

    threading.Thread(target=_loop, daemon=True,
                     name='agent-memory').start()


def _agent_memory_note_for(agent_id, instructions):
    """The agent's own recalled work-learnings for this task, as lines to
    append to its instructions. Bounded (2 memories), failure-swallowed --
    recall enriches the task, it is never a precondition for it."""
    try:
        import serve as _serve_mod
        from agents import memory as _memory
        block = _memory.agent_ltm_recall(agent_id, instructions, limit=2)
        if block:
            return 'From your own completed work, what you learned before:\n' + block
    except Exception:
        pass
    return None


def _merge_completed_grades(state, now_ms=None):
    """Drain the background grader's mailbox into state (tick-thread only).
    Upgrades a pending deliverable entry with the real judgment and runs the
    grade-dependent actions the synchronous grader used to do inline: the
    calibration suspect audit, low-grade coaching, and success mining. A
    None grade (Jev outage) just closes the pending marker -- the fallback
    grade recorded at completion stands, exactly as before. Entries whose
    task/deliverable rolled out of state are dropped."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    global _grade_results
    with _grade_results_lock:
        if not _grade_results:
            return 0
        results = _grade_results
        _grade_results = {}
    merged = 0
    by_id = {}
    for entry in state.get('completedDeliverables') or []:
        if isinstance(entry, dict) and entry.get('gradePending'):
            by_id.setdefault(entry.get('id'), entry)
    tasks = state.get('tasks') or {}
    for task_id, res in results.items():
        entry = by_id.get(task_id)
        if entry is None:
            continue  # deliverable rolled out of state; nothing to upgrade
        grade = res.get('grade')
        if not isinstance(grade, (int, float)):
            entry['gradePending'] = False  # outage: the fallback stands
            continue
        grade = float(grade)
        task = tasks.get(task_id)
        # Suspect audit on the QUEUE-TIME evidence snapshot: the audit judges
        # the artifact evidence as completed (a task record that rolled out
        # of state, or was mutated since, must not flip the verdict).
        grade_suspect = (grade <= GRADE_SUSPECT_CEILING
                         and bool(res.get('artifactEvidence')))
        if grade_suspect:
            _log_governance(state, 'admin', 'grade_artifact_mismatch',
                            {'task': task_id, 'agent': res.get('agentId'),
                             'grade': grade, 'room': res.get('room'),
                             'title': (res.get('title') or '')[:120]})
        entry.update({'grade': grade, 'gradeIsReal': True,
                      'gradePending': False, 'gradedAt': now_ms,
                      'gradeSuspect': grade_suspect})
        _tref = ((task or {}).get('teamId')
                 or agent_team_ref(state, res.get('agentId')))
        _floor = team_policy(state, _tref, 'deliverableGradeFloor')
        _success = team_policy(state, _tref, 'successGradeFloor')
        if grade < _floor and not grade_suspect \
                and res.get('agentId') in (state.get('agents') or {}):
            # A suspect grade never coaches (see the calibration audit), and
            # a rolled-off-roster agent has no growth plan to coach.
            _coach_low_grade(state, res.get('agentId'), res.get('room'),
                             grade, now_ms, res.get('title'), task=task)
        elif grade >= _success:
            # Positive learning loop (ChatDev ECL): best-effort, failure
            # swallowed so merging never blocks the tick.
            try:
                import serve as _serve_mod
                _serve_mod._record_success(res.get('room'), res.get('title'),
                                           grade, agent_id=res.get('agentId'))
            except Exception:
                pass
        # Agent long-term memory (2026-11-01): a landed, judged deliverable
        # is exactly the moment a worker learned something. Enqueue a
        # bounded-cadence distillation (worker thread; never on the tick).
        _maybe_queue_agent_memory(state, task, res, now_ms)
        merged += 1
    return merged


def _grade_completed_task(state, agent_id, task, now_ms):
    """Record a landed deliverable and QUEUE its Jev judgment. Never blocks
    completion, and (2026-11-01 scale fix) never blocks the tick: the entry
    lands synchronously with the deterministic fallback grade the outage path
    always produced (gradeIsReal False, gradePending True), and the
    background grader (_ensure_grading_worker) computes the real judgment
    off-thread; a later _merge_completed_grades pass upgrades the entry and
    runs the grade-dependent actions (suspect audit, coaching, success
    mining). On a grader outage the fallback stands, exactly as before.
    Also triggers a growth-plan entry if graded below the floor.

    Calibration audit (1,000-agent scale prep): a devastating grade (<=
    GRADE_SUSPECT_CEILING) on a record that carries real artifact evidence is
    the false-negative signature -- the grader judged the title, not the work.
    Such grades are annotated `gradeSuspect`, logged, excluded from the
    trailing-grade averages, and never trigger coaching. An honest empty
    record (no artifact evidence) is still a legitimate low grade."""
    if not task or task.get('reviewOf') or _is_auxiliary_deliverable(task):
        # No committed deliverable -> nothing to grade. Meetings, spikes,
        # reviews, avatars, and supervisor sweeps were being graded against the
        # code/report roadmap, recording real 0.0 scores and coaching
        # escalations for work that never had a deliverable to grade.
        return
    room = task.get('room')
    if not room or room not in VALUED_QUEUE_ROOMS:
        return
    title = task.get('title') or 'untitled deliverable'
    a = (state.get('agents') or {}).get(agent_id) or {}
    instructions = (f"Grade the deliverable '{title}' landed in the {room} room by "
                    f"{a.get('name') or agent_id} ({a.get('role') or 'worker'}). "
                    f"Did it meet the work order and the room's spec? Judge ONLY from "
                    f"the evidence below and choose the 0-10 bucket that best matches "
                    f"what was actually produced.")
    evidence = _deliverable_evidence_lines(task)
    if evidence:
        instructions += '\n\n' + '\n\n'.join(evidence)
    else:
        # Honest absence: a task record with no deliverable content beyond the
        # title is a legitimate 0 -- but Jev should know it is judging an empty
        # record, not silently treat the title as the whole deliverable.
        instructions += ('\n\nThe task record carries no deliverable content beyond '
                         'the title itself: no deliverable text, no completion note, '
                         'no written files, no artifact path. Grade what is actually here.')
    # Room anchors (serve's institutional-memory retrieval): what comparable
    # deliverables in this room were graded, and whether they held up. A
    # judge with real anchors grades against the room's demonstrated bar
    # instead of its own reflex. Bounded and failure-swallowed -- anchors
    # enrich the judgment, they are never a precondition for it.
    # NB: this MUST stay a plain try/except with no else -- an else here ran
    # whenever the anchor call merely did not raise, which appended the
    # honest-absence line to prompts that DID carry evidence (2026-11-01
    # regression: every graded deliverable read its own completion note and
    # "no deliverable content" in the same prompt, and the judge honestly
    # graded 0; grade_outcomes became all-'0' and every trailing grade read
    # 0.0/10).
    try:
        import serve as _serve_mod
        anchors = _serve_mod._rag_room_anchors(
            room, exclude_ref=task.get('id'),
            village=task.get('villageId') or None)
        if anchors:
            instructions += '\n\n' + '\n\n'.join(anchors)
    except Exception:
        pass
    # Provisional record (2026-11-01 scale fix): the tick thread never waits
    # on the grader's OpenRouter round trip (measured: 12.5s of a 67s
    # 200-agent lifecycle run was synchronous Jev calls inside _task_cycle).
    # The entry lands NOW with the same deterministic fallback an outage
    # always produced (gradeIsReal False), and the background worker computes
    # the real judgment; _merge_completed_grades (next _task_cycle pass)
    # upgrades the entry and runs the grade-dependent actions.
    grade = _room_trailing_grade(state, room)
    if grade is None:
        grade = 5.0
    grade_is_real = False
    # Calibration audit: the grade-vs-artifact contradiction test. Runs at
    # MERGE time against the real judgment (see _merge_completed_grades);
    # the provisional record is never suspect by construction.
    grade_suspect = False
    if grade_suspect:
        _log_governance(state, 'admin', 'grade_artifact_mismatch',
                        {'task': task.get('id'), 'agent': agent_id,
                         'grade': grade,
                         'room': room, 'title': (title or '')[:120]})
    state.setdefault('completedDeliverables', []).append({
        'id': task.get('id'), 'room': room, 'title': title,
        'agentId': agent_id, 'grade': grade,
        'taskType': task.get('taskType') or 'code',
        # Hermes Town lesson: a fabricated fallback grade must be
        # distinguishable from a real Jev judgment, not silently identical --
        # see _room_trailing_grade's own comment for why this also matters
        # beyond just labeling (it stops fallback grades compounding into
        # future fallbacks).
        'gradeIsReal': grade_is_real, 'gradedAt': now_ms,
        'gradeSuspect': grade_suspect,
        # The real Jev judgment is computed off-thread (see
        # _ensure_grading_worker) and merged onto this entry later.
        'gradePending': True,
        # Knowledge-rot/freshness signal (external-eval port): the in-document
        # "Last reviewed" date is authoritative for aging -- mtime alone is a weak
        # signal (resets on copy). Consumers can treat a deliverable newer than
        # its lastReviewed date as awaiting (re)review.
        'lastReviewed': datetime.datetime.utcnow().isoformat() + 'Z',
        'source': 'refinement' if (task.get('instructions') or '').find('backlog refinement') >= 0 else 'other',
    })
    _tref = task.get('teamId') or agent_team_ref(state, agent_id)
    _floor = team_policy(state, _tref, 'deliverableGradeFloor')
    _success = team_policy(state, _tref, 'successGradeFloor')
    # A fabricated fallback is a placeholder, not a personnel signal. It may
    # be recorded for continuity, but it must not coach, escalate, or feed
    # success mining as if Jev had judged the work.
    if grade_is_real and grade < _floor and not grade_suspect:
        # A suspect grade never coaches: the calibration audit flagged it as
        # a probable grader error, and coaching an agent for the grader's
        # mistake is the noise the audit exists to stop.
        _coach_low_grade(state, agent_id, room, grade, now_ms,
                         title, task=task)
    elif grade_is_real and grade >= _success:
        # Positive learning loop (ChatDev ECL): a deliverable graded at/above
        # the success floor is the raw material for the weekly success-mining
        # pass -- "what made this good" should propagate, not just "what went
        # wrong". Best-effort: a ledger failure is swallowed so grading never
        # blocks completion (same contract as _rule_mine_step's failure path).
        try:
            import serve as _serve_mod
            _serve_mod._record_success(room, title, grade, agent_id=agent_id)
        except Exception:
            pass
    # Queue the real judgment for the background grader (see the module-top
    # note on the two-phase mailbox discipline). The evidence flag is a
    # queue-time snapshot: the calibration audit judges the artifact evidence
    # AS COMPLETED -- the task record may mutate or roll out of state before
    # the merge runs.
    with _pending_grades_lock:
        _pending_grades[task.get('id')] = {
            'taskId': task.get('id'), 'agentId': agent_id, 'room': room,
            'title': title, 'instructions': instructions,
            'artifactEvidence': bool(_artifact_evidence_present(task))}
    _ensure_grading_worker()


def _scale_tier_worker(state, agent_id):
    """True when `agent_id` is a plain worker under the operator's
    scaleTierWorkers switch (the 1,000-agent scenario): cheap-model closes
    skip the coaching loop, and low grades log instead of coaching."""
    import core.config as _cfg
    if not (_cfg.OPERATOR_POLICY or {}).get('scaleTierWorkers'):
        return False
    return bool(governance.scale_worker_row(state, agent_id))


def _coach_low_grade(state, agent_id, room, grade, now_ms, title, task=None):
    """W5: round-aware low-grade coaching. A single _write_growth_plan deduped by
    kind coached a weak worker ONCE; a worker who keeps closing below the floor
    then absorbed the identical note forever. Each low close re-lands a fresh,
    escalating coaching note (repeat=True) up to COACHING_MAX_ROUNDS, then the
    problem escalates to the owning director (a work-request + governance entry)
    -- either the coaching works, or it surfaces. Bounded: escalation fires once
    per round-cap, and the work-request dedups.

    The scale-tier gate lives HERE (not at call sites): a scale-tier worker's
    cheap-model close skips the coaching loop entirely and logs
    `scale_tier_grade_accepted` instead -- at 1,000-agent scale, coaching
    noise IS the cost.

    When `task` carries acceptance criteria or a user story, the note quotes
    them -- agents reported 0.0-grade coaching that said "meet the spec" but
    never stated what the spec was, leaving nothing to act on."""
    if _scale_tier_worker(state, agent_id):
        _log_governance(state, 'admin', 'scale_tier_grade_accepted',
                        {'agent': agent_id, 'grade': grade,
                         'title': (title or '')[:120]})
        return
    _tref = agent_team_ref(state, agent_id)
    _maxr = team_policy(state, _tref, 'coachingMaxRounds')
    _floor = team_policy(state, _tref, 'deliverableGradeFloor')
    # Count low-grade coaching rounds SINCE THE LAST REAL RECOVERY. A lifetime
    # count punished an agent who recovered months ago: the next single low
    # close escalated immediately. Rounds are still plan-based so the daily
    # catch-up sweep (which has no new deliverable) can re-coach or escalate
    # from the plans that already exist.
    recovery_ts = 0
    for d in (state.get('completedDeliverables') or []):
        if d.get('agentId') != agent_id or not d.get('gradeIsReal', True):
            continue
        if isinstance(d.get('grade'), (int, float)) and d.get('grade') >= _floor:
            recovery_ts = max(recovery_ts, d.get('gradedAt') or 0)
    plans = state.get('growthPlans', {}).get(agent_id) or []
    rounds = 0
    for p in plans:
        if p.get('kind') != 'low_grade':
            continue
        ts = p.get('ts')
        # Legacy plans carry no timestamp. They count only when there has
        # been no recovery to order them against.
        if ts is None:
            if recovery_ts == 0:
                rounds += 1
        elif ts > recovery_ts:
            rounds += 1
    if rounds >= _maxr:
        _escalate_coaching_loop(state, agent_id, room, grade, now_ms, title)
        return
    spec_bits = []
    if task:
        if (task.get('acceptanceCriteria') or '').strip():
            spec_bits.append('Acceptance criteria for this task:\n' + task['acceptanceCriteria'].strip()[:600])
        if (task.get('userStory') or '').strip():
            spec_bits.append('User story: ' + task['userStory'].strip()[:300])
    spec_block = ('\n\n' + '\n\n'.join(spec_bits)) if spec_bits else ''
    _write_growth_plan(
        state, agent_id, room, 'low_grade', now_ms,
        f"Deliverable '{title}' in {room} graded {grade}/10 (below the "
        f"{_floor:.0f} floor). Coaching round {rounds + 1} of "
        f"{_maxr}: you've been coached on this before and the grade "
        f"hasn't recovered -- meet the spec and room standards end-to-end this time."
        f"{spec_block}",
        repeat=True)


def _escalate_coaching_loop(state, agent_id, room, grade, now_ms, title):
    """Terminal coaching-loop escalation: a worker who has closed below the
    floor COACHING_MAX_ROUNDS times without improvement is surfaced to the
    owning team's scrum master as a work-request (the same SM-routing as the
    stale-work/help sweeps), so the SM can help, re-plan, or reassign -- a
    bounded end to the loop, never an infinite re-coach. Logs governance."""
    def _director_for(aid):
        for d in (state.get('agentRoster') or []):
            if d.get('id') == aid:
                return d.get('director')
        return None
    director_id = _director_for(agent_id) or room  # fall back to room-keyed team
    team = None
    for t in (state.get('teams') or []):
        if t.get('directorId') == director_id or t.get('room') == room:
            team = t
            break
    filer = None
    if team:
        filer = _escalation_scrum_master_for(state, team.get('directorId') or team.get('id'))
        if not filer:
            filer = team.get('directorId') or team.get('id')
    _tref = (team.get('directorId') or team.get('id')) if team else director_id
    _floor = team_policy(state, _tref, 'deliverableGradeFloor')
    _maxr = team_policy(state, _tref, 'coachingMaxRounds')
    routed = False
    if filer:
        routed = bool(file_work_request(
            state, filer,
            (f'Help: {agent_id} keeps closing below the delivery floor')[:120],
            room,
            reason=(f'{agent_id} has closed below the {_floor:.0f} floor '
                    f'{_maxr}+ times (latest: {title}, {grade}/10). Coaching is '
                    f'not working; the scrum master decides whether to help, re-plan, or reassign.')))
    _log_governance(state, 'admin', 'coaching_loop_escalated',
                    {'agent': agent_id, 'title': (title or '')[:120], 'room': room,
                     'grade': grade, 'routed': routed})


VERIFY_EVIDENCE_MAX_FILES = 4


def _verify_incident_restore(state, task, result, now_ms):
    """BP-31 close probe: an incident (bug task) may not close on a bare
    "fixed it". Deterministic evidence check, no model involved: the coding
    executor's `writtenPy` (the files the fix actually landed in), an
    executor-supplied `restoreCheck` note, or a Library artifact count as
    restore evidence; an ok-completion with NONE of those is closed loud --
    `restoreVerified` False on the task, a feedback note to the on-call saying
    exactly what was missing, an escalation to the owning team's scrum master
    (the same bounded pipeline an abandoned incident rides), and a plain
    `restoreVerified` mark in the product runbook entry so the astronaut-grade
    lesson ("we fixed it" / but nothing changed) never reads as a verified
    restore. Called from _apply_content_result, which runs once per completed
    content pass, in the tick's single read-modify-write. Pure state
    mutation."""
    if not task or task.get('taskType') != 'bug' or task.get('restoreCheck'):
        return
    if not isinstance(result, dict) or not result.get('ok'):
        return
    files = result.get('writtenPy') or []
    check_note = (result.get('restoreCheck') or '').strip()
    command = (result.get('command') or '').strip()
    artifact = bool(result.get('libraryPath'))
    # A restore note must be paired with the command that produced it. A bare
    # note is self-attestation; files and a Library artifact are mechanical.
    verified = bool(files) or (bool(check_note) and bool(command)) or artifact
    receipt = {
        'ok': verified,
        'ts': now_ms,
        # Name the actual evidence: the landed files win (the strongest, most
        # mechanical kind), then the executor's own verification note, then a
        # Library artifact; 'none' is the loud empty that triggers the rest.
        'evidence': (', '.join([str(f) for f in files[:VERIFY_EVIDENCE_MAX_FILES]])[:240]
                     if files
                     else (check_note[:240] if check_note
                           else ('library: ' + str(result.get('libraryPath'))[:200]
                                 if artifact else 'none'))),
    }
    task['restoreCheck'] = receipt
    task['restoreVerified'] = verified
    aid = task.get('assignedTo')
    title = task.get('title') or 'incident'
    if not verified:
        if aid:
            _append_feedback(state, aid, (
                f'Your fix for "{title}" closed WITHOUT restore evidence: no files '
                f'were written (writtenPy empty), no restore check was attached, '
                f'and no Library artifact was recorded. If the restore was real, '
                f'say how you verified it when you file your next card.'),
                             source='incident_restore')
        _oncall_restore_escalate(state, task, title, now_ms)


def _oncall_restore_escalate(state, task, title, now_ms):
    """Route an evidence-free incident close to the owning team's scrum master
    via the existing bounded escalation pipeline (dedup + per-team cap):
    'restore_unverified' is the new source tag, so the SM ceremony can weigh a
    no-activity card exactly like an unrestored one instead of it dying
    silently. Mirrors _on_work_item_abandoned's guard set."""
    product_id = task.get('productId')
    if not product_id:
        return
    director_id = _product_director(state, product_id)
    if not director_id:
        return
    _escalate_oncall_failure(state, product_id, director_id, title,
                             'restore_unverified', now_ms)


def _runbook_task(state, task, now_ms):
    """Fire-and-forget runbook writer for a completed INCIDENT (bug): one line
    captured for the owning product so future handlers learn, not re-discover."""
    if not task or task.get('taskType') != 'bug':
        return
    product_id = task.get('productId')
    if not product_id:
        return
    title = task.get('title') or 'incident'
    a = (state.get('agents') or {}).get(task.get('assignedTo')) or {}
    instructions = (f"Write one line for the runbook: what broke in this incident "
                    f"('{title}') and how {a.get('name') or task.get('assignedTo')} fixed it.")
    real_summary = _runbook_decider(state, instructions, product_id, task.get('room'), title,
                                    agent_id=task.get('assignedTo'))
    summary_is_real = bool(real_summary)
    summary = real_summary or f"{title} -- fixed by {a.get('name') or task.get('assignedTo')}"
    rb = state.setdefault('runbooks', {}).setdefault(product_id, [])
    # Hermes Town lesson: mark a fallback template line as such --
    # a reader shouldn't mistake a generic "X -- fixed by Y" filler for a real,
    # model-written account of what actually broke and how.
    receipt = task.get('restoreCheck') or {}
    rb.append({'ts': now_ms, 'room': task.get('room'), 'summary': summary,
               'summaryIsReal': summary_is_real,
               # BP-31 close probe: None (no completion run), True, or False --
               # never a fake check.
               'restoreVerified': receipt.get('ok')
               if task.get('restoreCheck') else None,
               'evidence': receipt.get('evidence')})
    if len(rb) > RUNBOOK_MAX_ENTRIES_PER_PRODUCT:
        del rb[:-RUNBOOK_MAX_ENTRIES_PER_PRODUCT]


def _write_growth_plan(state, agent_id, room, kind, now_ms, note, repeat=False):
    """Record a coaching note against an agent (dedup by kind so repeated low
    grades don't spam identical plans). The note is APPENDED once to that
    agent's next assigned task by _coaching_note_for. `repeat=True` bypasses the
    dedup (a NEW commitment each event -- e.g. a fresh Knowledge Social adopt --
    must land even when an older one of the same kind already queued/applied)."""
    plans = state.setdefault('growthPlans', {}).setdefault(agent_id, [])
    if not repeat:
        for p in plans:
            if p.get('kind') == kind:
                return  # already coaching on this axis
    plans.append({'kind': kind, 'ts': now_ms, 'room': room, 'note': note, 'applied': False})


def _coaching_note_for(state, agent_id):
    """Pop the oldest un-applied growth plan for an agent and return it as a
    coaching line to append to their next task; marks it applied. Returns None
    if none pending. This is the 'manager feedback changes the next execution'
    step -- each note is injected exactly once."""
    plans = (state.get('growthPlans') or {}).get(agent_id)
    if not plans:
        return None
    for p in plans:
        if not p.get('applied'):
            p['applied'] = True
            return p.get('note')
    return None


def _runbook_note_for(state, product_id, instructions=None):
    """A prior incident for this product, as a one-line coaching-style note
    for a new incident task. With `instructions`, the MOST RELEVANT prior
    incident is retrieved by word overlap against the task (an older
    relevant incident beats a newer unrelated one), preferring real
    model-written accounts over fallback filler and fixes that held up;
    with no overlap at all (or no instructions), the most recent entry is
    the note -- the old behavior. Returns None if the product has no
    history."""
    entries = (state.get('runbooks') or {}).get(product_id) or []
    if not entries:
        return None
    if instructions:
        words = {w for w in re.findall(r"[a-z0-9]{3,}", str(instructions).lower())}
        best = None
        for idx, entry in enumerate(entries):
            summary = str(entry.get('summary') or '')
            overlap = len(words & {w for w in re.findall(r"[a-z0-9]{3,}", summary.lower())})
            if not overlap:
                continue
            # Prefer a real account over filler, and a fix that held up;
            # later entries break ties (recency is the remaining signal).
            rank = (overlap + (1 if entry.get('summaryIsReal') else 0)
                    + (0.5 if entry.get('restoreVerified') is True else 0.0), idx)
            if best is None or rank > best[0]:
                best = (rank, entry)
        if best is not None:
            return f"Prior incident in this product was: {str(best[1].get('summary'))[:240]}"
    last = entries[-1]
    return f"Prior incident in this product was: {last.get('summary')}"


def _refocus_note_for(state, product_id):
    """One-line reminder of the product's stated purpose (name + summary),
    for a task that's REVISITING already-done work -- a review/revision or
    an incident -- rather than starting fresh. That's the moment a series of
    locally-defensible additions has the most room to have already compounded
    into scope nobody asked for (each individual review comment made sense;
    the sum of them may not still serve what the product was for). Returns
    None when there's no product record or no summary to anchor against, so
    this never fabricates a "why" that isn't actually on file.
    Added after comparing this think tank's own accumulated review/
    escalation machinery (see _CEREMONY_ACTIONS in serve.py) to a pattern
    seen elsewhere -- checking alignment against the stated goal specifically
    at the re-review moment, not on every fresh task, which would just be
    more ceremony."""
    if not product_id:
        return None
    prod = (state.get('products') or {}).get(product_id)
    if not prod:
        return None
    summary = (prod.get('summary') or '').strip()
    if not summary:
        return None
    return (f'Refocus before continuing: this task is part of "{prod.get("name")}", '
            f'whose stated purpose is: {summary}. Confirm what you\'re about to do still '
            f'serves that -- not just what looks like a natural next addition from here.')


def _augment_task_instructions(state, agent_id, product_id, instructions, refocus=False, scoped=False):
    """Append coaching + runbook knowledge to a task's instructions BEFORE
    assignment. Pure: returns the augmented instructions (or the original).
    A pending growth plan is injected once (marking it applied); a product's
    runbook history is pulled into a new incident on that product; a refocus
    reminder (see _refocus_note_for) is added only when the caller says this
    task is revisiting existing work, not starting fresh. The hive-mind
    consensus relay (see _consensus_relay_step) is READ FIRST -- a one-line
    'current direction' the next cycle acts on -- EXCEPT for a scoped card
    (player-filed, JIRA-linked, or a repair/review of an existing task). A
    global room focus must never override a card that already names its own
    room and deliverable; that was the BEN-6 failure, where a pressoffice
    repair was told to focus on observatory and abandoned its acceptance
    criteria."""
    pieces = [instructions] if instructions else []
    # The spine FIRST: the charter goal opens every task's instructions so the
    # work a room does points at the player's direction (see _charter_note).
    charter_line = _charter_note(state)
    if charter_line:
        pieces.insert(0, charter_line)
    relay = _consensus_relay_note(state)
    if relay and not scoped:
        pieces.insert(0, relay)
    note = _coaching_note_for(state, agent_id)
    if note:
        pieces.append(note)
    # The agent's own distilled work-learnings (agent_ltm): what THIS worker
    # learned on comparable completed tasks, recalled against these
    # instructions. Bounded and failure-swallowed (see _agent_memory_note_for).
    learned = _agent_memory_note_for(agent_id, instructions)
    if learned:
        pieces.append(learned)
    if product_id:
        rb = _runbook_note_for(state, product_id, instructions=instructions)
        if rb:
            pieces.append(rb)
    if refocus:
        rf = _refocus_note_for(state, product_id)
        if rf:
            pieces.append(rf)
    if not pieces:
        return instructions
    return '\n'.join(p for p in pieces if p)
