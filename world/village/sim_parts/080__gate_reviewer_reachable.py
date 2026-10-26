

def _gate_reviewer_reachable(state, rid, gate, now_ms):
    """Can this locked reviewer actually produce a vote? True when she is ON-DUTY
    and idlable (free to take her review subtask next cycle), OR when her review
    subtask is still live in the queue (so the existing pin-wake can bring her --and
    the gate a healthy chance). A reviewer who is off-duty-but-present is reachable
    too (she wakes on click); one who is *gone* (fired, or perpetually busy with
    other pinned work, or her vote subtask died) is not."""
    if not rid:
        return False
    a = (state.get('agents') or {}).get(rid)
    if a is None:
        return False  # fired -- an id in the gate but no live agent
    # A live agent is reachable if she isn't entrenched in non-review other work
    # (off-duty is wakeable; busy-on-own-task is not, though she may free soon --
    # count a still-queued review subtask pinned to her as reachable below).
    if not a.get('busy') and not a.get('task'):
        return True
    for item in (state.get('workQueue') or []):
        if item.get('reviewOf') and item.get('assignedTo') == rid:
            return True
    return False


def _prune_state_blob(state, now_ms):
    """Bound the append-only tails inside the kv_state blob. The whole blob is
    serialized + autosaved every ~1s, so anything that grows without bound keeps
    growing that write forever (the 'save takes a second' slowdown). Every
    structure here is a bounded TAIL: live/open/pending entries are ALWAYS kept,
    only the historical tail is capped. `tasks` is deliberately untouched -- it
    backs per-agent history and counters, and a hard cap there would silently
    corrupt them. Pure; mutates `state` only. Called from _task_cycle on a coarse
    cadence."""
    cd = state.get('completedDeliverables')
    if isinstance(cd, list) and len(cd) > _TAIL_COMPLETED_DELIVERABLES:
        state['completedDeliverables'] = cd[-_TAIL_COMPLETED_DELIVERABLES:]
    br = state.get('backlogRequests')
    if isinstance(br, list):
        keep = [r for r in br if (r or {}).get('status') == 'pending']
        tail = [r for r in br if (r or {}).get('status') != 'pending']
        if len(tail) > _TAIL_BACKLOG_RESOLVED:
            tail = tail[-_TAIL_BACKLOG_RESOLVED:]
        state['backlogRequests'] = keep + tail
    pi = state.get('playerInbox')
    if isinstance(pi, list):
        keep = [m for m in pi if not (m or {}).get('answered')]
        tail = [m for m in pi if (m or {}).get('answered')]
        if len(tail) > _TAIL_PLAYER_INBOX_ANSWERED:
            tail = tail[-_TAIL_PLAYER_INBOX_ANSWERED:]
        state['playerInbox'] = keep + tail
    issues = state.get('issues')
    if isinstance(issues, dict):
        open_entries = [e for e in issues.values() if (e or {}).get('status') not in ('resolved', 'closed')]
        closed_entries = [e for e in issues.values() if (e or {}).get('status') in ('resolved', 'closed')]
        if len(closed_entries) > _TAIL_ISSUES_CLOSED:
            closed_entries = closed_entries[-_TAIL_ISSUES_CLOSED:]
        state['issues'] = {e.get('key') or i: e for i, e in enumerate(open_entries + closed_entries)}
    gp = state.get('growthPlans')
    if isinstance(gp, list):
        keep = [p for p in gp if (p or {}).get('status') != 'applied']
        tail = [p for p in gp if (p or {}).get('status') == 'applied']
        if len(tail) > _TAIL_GROWTH_PLANS_APPLIED:
            tail = tail[-_TAIL_GROWTH_PLANS_APPLIED:]
        state['growthPlans'] = keep + tail


def _sweep_stuck_gates(state, now_ms):
    """Phase E3.1: stuck-at-review escalation watchdog. For every story sitting in
    `needs_review`, if its LOCKED reviewer pair cannot produce a vote (both
    reviewers unreachable/inert) but the pool COULD be widened, re-pick a fresh
    reachable pair and re-pin the pending `reviewOf` subtasks so the gate gains a
    viable path (the existing _assign_due_item pin-wake then wakes the new
    reviewer for free). Bounded: never widens a gate younger than
    STUCK_GATE_GRACE_MS, and at most one re-pick per gate per
    STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS. Called from _task_cycle on a cadence.
    Pure; mutates `state` only. Never auto-closes -- an unreachable gate is made
    REACHABLE, and done stays defined by _peer_gate_should_close."""
    for task in (state.get('tasks') or {}).values():
        gate = task.get('_peerGate')
        if not gate or task.get('status') != 'needs_review':
            continue
        if gate.get('escalated'):
            continue  # frozen -- _maybe_escalate_stuck_gate already notified the player
        entered = gate.get('enteredMs') or 0
        if now_ms - entered < STUCK_GATE_GRACE_MS:
            continue  # young gate -- reviewer 1 may still be about to vote
        last = gate.get('stuckRescueTs') or 0
        if now_ms - last < STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS:
            continue  # already rescued too recently -- bounded, no re-pick storm
        locked = gate.get('reviewerIds') or []
        pending_reviews = [q for q in (state.get('workQueue') or [])
                           if q.get('reviewOf') == task.get('id')]
        in_flight_reviews = [
            t for t in (state.get('tasks') or {}).values()
            if t.get('reviewOf') == task.get('id')
            and t.get('status') in ('walking', 'working')]
        # A review already walking/working is not a lost review. Widening the
        # gate here changes reviewerIds and resets enteredMs while those cards
        # are still running, so their votes are discarded by the age guard
        # (the 2026-10-26 task-649 gate). Wait for them; the stale-work sweep
        # handles a review that genuinely died.
        if in_flight_reviews:
            continue
        # A gate with one clean vote needs a real second vote. If the timeout
        # window has passed and no review is pending from someone other than
        # the approver, re-queue one fresh review instead of lowering the bar.
        approvals = int(gate.get('approvals') or 0)
        approvers = set(gate.get('approvers') or [])
        required = int(gate.get('requiredApprovals') or PEER_APPROVALS_DEFAULT)
        if (required > 1 and approvals == 1 and entered and
                now_ms - entered >= PEER_REVIEW_TIMEOUT_MS):
            pending_second = [q for q in pending_reviews
                              if q.get('assignedTo') not in approvers]
            if not pending_second:
                last_second = gate.get('secondVoteRescueTs') or 0
                if now_ms - last_second >= STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS:
                    author = task.get('assignedTo')
                    fresh = [rid for rid in _pick_reviewer_ids(
                                 state, author, task_room=task.get('room'))
                             if rid not in approvers and rid != author]
                    for d in (state.get('agentRoster') or []):
                        if fresh:
                            break
                        rid = d.get('id')
                        if (not rid or d.get('isAdmin') or rid == author
                                or rid in approvers):
                            continue
                        if _gate_reviewer_reachable(state, rid, gate, now_ms):
                            fresh.append(rid)
                    if fresh:
                        if _reenter_gate_review(state, task, [fresh[0]]):
                            gate['secondVoteRescueTs'] = now_ms
                            try:
                                from serve import log_action
                                log_action(None, 'task_second_vote_requested',
                                           {'taskId': task.get('id'),
                                            'title': task.get('title'),
                                            'reviewer': fresh[0],
                                            'approvers': sorted(approvers)},
                                           authorized=True)
                            except Exception:
                                pass
            continue  # one-vote gate: never fall through to the widen path
        # Phase E3 recovery: a review that completed WITHOUT folding a vote (e.g.
        # under the old router it ran the research executor, or a content timeout
        # dropped it) leaves the gate with NO pending review to pin. The pair may
        # even be fully reachable -- `_gate_reviewer_reachable` is true -- yet
        # there is nothing to do, so the existing widen branch takes the
        # "still advance" continue and the story sits in needs_review forever
        # with reviewedTaskIds growing and approvals stuck at 0. Detect that
        # exact state: reachable reviewers but NO pending reviewOf work item.
        if not pending_reviews:
            if any(_gate_reviewer_reachable(state, rid, gate, now_ms) for rid in locked):
                # Reviewers are free but the review work silently vanished
                # (consumed with no verdict, or never enqueued): re-queue a fresh
                # review to the SAME (reachable) pair so the gate gains a vote
                # path back. Bounded by the rescue cooldown above + grace, AND
                # by the shared cycle cap -- this rescue path used to be able
                # to re-enter indefinitely (once per cooldown window, forever)
                # with no overall bound at all.
                if _maybe_escalate_stuck_gate(state, task, gate, 'repeated failed review attempts'):
                    continue
                repl = _reenter_gate_review(state, task, locked)
                if repl:
                    gate['stuckRescueTs'] = now_ms
                    gate['enteredMs'] = now_ms  # restart the timeout window fairly
                    try:
                        from serve import log_action
                        log_action(None, 'task_review_requeued',
                                   {'taskId': task.get('id'), 'title': task.get('title'),
                                    'to': repl}, authorized=True)
                    except Exception:
                        pass
                continue
        if any(_gate_reviewer_reachable(state, rid, gate, now_ms) for rid in locked):
            continue  # the locked pair can still advance -- leave it alone
        # Locked pair is unreachable; widen. Prefer on-team + room-contributors,
        # excluding the locked pair, any non-author non-admin.
        author = task.get('assignedTo')
        fresh = _pick_reviewer_ids(state, author, task_room=task.get('room'))
        fresh = [rid for rid in fresh if rid not in locked and rid != author]
        needed = int(gate.get('requiredApprovals') or PEER_APPROVALS_DEFAULT)
        # Top up to the gate's bar from the broader reachable non-admin roster
        # when the team-widened pool came up short (the locked filter can thin
        # it). The rescue must yield a viable set, not leave the gate one vote
        # short of its close rule.
        for d in (state.get('agentRoster') or []):
            if len(fresh) >= needed:
                break
            if d.get('isAdmin') or d.get('id') == author or d.get('id') in locked:
                continue
            if d.get('id') in fresh:
                continue
            if _gate_reviewer_reachable(state, d.get('id'), gate, now_ms):
                fresh.append(d.get('id'))
        if not fresh:
            continue  # genuinely nobody left (author-only think tank) -- nothing to do
        # Re-pin the still-queued review subtasks to the fresh reviewers, and re-
        # pin any already-assigned reviewer; then record the gate as rescued.
        repl = iter(fresh)
        replaced = 0
        for item in (state.get('workQueue') or []):
            if item.get('reviewOf') == task.get('id'):
                item['assignedTo'] = next(repl, item.get('assignedTo'))
                replaced += 1
        gate['reviewerIds'] = fresh[:needed]
        gate['stuckRescueTs'] = now_ms
        gate['enteredMs'] = now_ms  # restart the timeout window fairly
        if replaced:
            try:
                from serve import log_action
                log_action(None, 'task_peer_widened',
                           {'taskId': task.get('id'), 'title': task.get('title'),
                            'from': locked, 'to': fresh[:needed]}, authorized=True)
            except Exception:
                pass
        else:
            # No queued subtask was available to re-pin: the old reviews were
            # consumed or never enqueued. Widen alone leaves the new pair with
            # no work and the gate waits out the full rescue cooldown. Queue
            # fresh reviews now (the 2026-10-26 task-649 gate).
            reenqueued = _reenter_gate_review(state, task, fresh[:needed])
            if reenqueued:
                try:
                    from serve import log_action
                    log_action(None, 'task_review_requeued',
                               {'taskId': task.get('id'), 'title': task.get('title'),
                                'to': reenqueued}, authorized=True)
                except Exception:
                    pass


def _reenter_gate_review(state, parent, reviewer_ids):
    """Recover a peer-gated story whose review work was silently consumed without
    a verdict (the gate's reviews are all done but approvals is still 0 and no
    reviewOf item remains queued). Enqueue a FRESH review subtask to each of the
    (reachable) `reviewer_ids` so the gate regains a vote path. Mirrors the
    review item _enter_peer_review builds, minus re-picking. Returns the list of
    reviewers re-enqueued (empty if none)."""
    reviewers = [rid for rid in reviewer_ids
                 if (state.get('agents') or {}).get(rid) is not None]
    if not reviewers:
        return []
    items = []
    for rid in reviewers:
        _append_mailbox((state.get('agents') or {}).get(rid, {}), {
            'kind': 'peer_review_request',
            'about': parent.get('id'),
            'title': parent.get('title'),
            'text': f'A previous review of "{parent.get("title")}" was lost without a decision. Work on it is ready for your review again -- please review and approve or send it back.'})
        items.append({
            'title': f'Review: {parent.get("title")}',
            'room': parent.get('room'),
            'instructions': (f'Review the work for "{parent.get("title")}" and approve it only if it is genuinely solid '
                             'with no concrete, fixable problems. Be a skeptical reviewer, not a rubber stamp.'),
            'goal': parent.get('projectLabel') or parent.get('goal'),
            'taskType': 'review',
            'projectLabel': parent.get('projectLabel') or parent.get('title'),
            'assignedTo': rid,
            'reviewOf': parent.get('id'),
            'reviewAuthorId': parent.get('assignedTo'),
        })
    queue_work(state, items)
    return reviewers


def _close_gated_story(state, parent):
    """Close a peer-gated story once its gate is satisfied. The parent is not on
    any agent (its author was released when primary work finished), so this is a
    direct status flip + log rather than finish_task, which needs a working
    agent. Returns True on close."""
    review_counts = parent.get('_peerGate') or {}
    # The author's approval and the deliverable grade belong to the gate
    # closing, not the earlier release. Count them once per close.
    if not parent.get('_gateCloseRecorded'):
        parent['_gateCloseRecorded'] = True
        author_id = parent.get('assignedTo')
        a = (state.get('agents') or {}).get(author_id)
        if a:
            a['approvedCount'] = (a.get('approvedCount') or 0) + 1
            a['weekApprovals'] = (a.get('weekApprovals') or 0) + 1
        if author_id:
            _note_completed_room(state, author_id, parent)
    review_counts['closed'] = True
    parent['status'] = 'done'
    # The gated story shipped: its per-story capability grant dies with it.
    _revoke_task_access(parent.get('id'))
    # The peer gate is satisfied: merge the story's isolated git worktree back
    # into the base workroom's main. Only coding-class parents ever got a
    # worktree (see content._review_sandbox_id); a non-coding gate skips this.
    # Village-scoped parents merge into their own village workroom. Merge
    # failure NEVER blocks the close: the branch stays unmerged and the
    # result dict records why (escalation/manual recovery stays possible).
    if _peer_coding_class(parent):
        try:
            from serve import (story_sandbox_id as _story_sandbox_id,
                               _merge_story_branch,
                               _village_sandbox_id)
            _sid = _story_sandbox_id(parent.get('id'))
            _sid = _village_sandbox_id(parent.get('assignedTo'), _sid, state=state)
            _merged = _merge_story_branch(_sid)
            if isinstance(_merged, dict) and not _merged.get('ok'):
                print(f'[story-merge] task {parent.get("id")}: '
                      f'{_merged.get("reason", "merge failed")}')
        except Exception as _e:
            # A missing worktree/branch (story ran entirely in the shared
            # sandbox, git hiccup, etc.) must not prevent the story closing.
            print(f'[story-merge] task {parent.get("id")}: {_e}')
    try:
        from serve import log_action
        log_action(None, 'task_peer_approved',
                   {'taskId': parent.get('id'), 'title': parent.get('title'),
                    'approvals': review_counts.get('approvals', 0),
                    'approvers': review_counts.get('approvers', [])}, authorized=True)
    except Exception:
        pass
    # Entering review deliberately stays quiet (peer review is
    # agent-to-agent), and a story SHIPPING is quiet too now -- the
    # player's close signal is the sprint retro. Called directly
    # (not via the notifyPlayer indirection content executors need)
    # since this already runs inside the tick's real read-modify-write
    # with live, mutable `state` in hand.
    _deliver_card_report(state, parent, 'peer_approved',
                         approvals=review_counts.get('approvals', 0))
    return True


def _parent_close_from_vote(state, parent, now_ms):
    """After a clean review vote lands, decide whether the parent's gate is now
    satisfied (2 distinct clean, or 1 + timeout) and close it if so. Mirrors the
    caller's needs: called from _task_cycle when a review/fix subtask completes."""
    gate = parent.get('_peerGate')
    if not gate:
        return False
    if (parent.get('status') in ('blocked', 'failed', 'cancelled', 'abandoned')
            or parent.get('manualAttention') or gate.get('escalated')):
        return False
    if _peer_gate_should_close(gate, now_ms, gate.get('enteredMs')):
        _close_gated_story(state, parent)
        return True
    return False


def _team_member_candidates(state, director_id, member_ids, now_ms):
    """Team-scoped whoNeedsHelp: the members of ONE director's team (their
    direct reports), described for Jev by morale + workload signals. A hire
    adds overflow help to an overloaded teammate, never outside the team."""
    agents = state.get('agents') or {}
    roster = {d.get('id'): d for d in (state.get('agentRoster') or [])}
    out = []
    for member_id in member_ids:
        a = agents.get(member_id)
        if not a:
            continue
        # Display name from the roster ideal (capitalized), fall back to live.
        display = roster.get(member_id, {}).get('name') or a.get('name') or member_id
        morale = morale_for(state, member_id, now_ms)
        out.append({'id': member_id, 'name': display,
                    'description': f"{display}, {a.get('role')}, on the {director_id} team. "
                                   f"Morale: {morale}/100. Approved work: {a.get('approvedCount')}. "
                                   f"Dropped work: {a.get('droppedCount')}. "
                                   f"Reports filed against them: {len(reports_about(state, member_id))}."})
    return out


def _free_outdoor_spot(state, grid, rnd=random.random):
    """Place a new hire on foot. Mirrors the JS finishHire: a free outdoor spot
    (pick_free_spot, reachable from SPAWN). The outskirts trailhead doors are
    gone, so a new hire simply appears at a free think tank spot."""
    agents = (state.get('agents') or {})
    occupied = [{'x': a.get('x'), 'y': a.get('y')} for a in agents.values() if a.get('visible')]
    spot = pick_free_spot(grid, avoid_points=occupied, rnd=rnd)
    return spot


def _borrow_inactive_agent_for_team(state, borrower_director_id, now_ms=None):
    """Cross-team borrowing: when a team needs help, borrow an INACTIVE
    (off-duty, dormant-inventory) agent from ANOTHER team for the borrower
    team's sprint instead of hiring a brand-new clone. The loan is recorded on
    the roster entry (`loan: {teamId, since, reason}`) and ends when the
    borrower's sprint closes -- unless the borrower still has pending work (a
    queued large-request breakdown or any pending backlog request), in which
    case the loan persists: that's the 'feature need -> agent may change teams'
    case. The borrowed agent keeps her home director and authority chain
    ('teams stay somewhat consistent'); only her work-assignment preference
    shifts to the borrower team for the loan's duration (see _assign_due_item's
    soft team preference). Returns the borrowed agent id, or None when no
    inactive agent from another team is available."""
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    borrower_members = set(_sim_direct_reports(state, borrower_director_id))
    idle_home = []
    busy_home = []
    for d in roster:
        if d.get('id') in borrower_members:
            continue  # already on the borrower team
        if d.get('isAdmin'):
            continue  # the admin is never loaned out
        if d.get('loan'):
            continue  # already on loan elsewhere
        a = agents.get(d.get('id'))
        if not a or not a.get('offDuty'):
            continue  # only INACTIVE (off-duty, dormant) agents are borrowed
        home = d.get('director')
        if not home or home == borrower_director_id:
            continue
        # Least-disruptive first: borrow from a home team with no active sprint
        # (its people aren't committed to anything right now).
        (busy_home if _team_in_active_sprint(state, home) else idle_home).append(d)
    pick = (idle_home or busy_home or [None])[0]
    if not pick:
        return None
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    loan_id = pick['id']
    pick['loan'] = {'teamId': borrower_director_id, 'since': now_ms, 'reason': 'sprint_borrow'}
    a = agents[loan_id]
    a['offDuty'] = False
    a['visible'] = True
    _log_governance(state, borrower_director_id, 'team_borrow',
                    {'borrowed': loan_id, 'team': borrower_director_id,
                     'home': pick.get('director'), 'action': 'loaned'})
    return loan_id


def _end_loans_for_team(state, team_id, now_ms=None):
    """End cross-team loans to a team whose sprint just closed -- unless the
    borrower still has pending work (a queued large-request breakdown or any
    pending backlog request), in which case the loan persists: that's the
    'feature need -> agent may change teams' case, and the need is ongoing. The
    borrowed agent keeps her home director; only the loan tag is cleared, so
    her assignment preference returns to her home team."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    pending = [r for r in (state.get('backlogRequests') or [])
               if r.get('status') == 'pending' and r.get('teamId') == team_id]
    ongoing = bool(pending)
    for d in (state.get('agentRoster') or []):
        loan = d.get('loan') or {}
        if loan.get('teamId') != team_id:
            continue
        if ongoing:
            loan['since'] = now_ms  # refresh the loan while the need persists
            _log_governance(state, team_id, 'team_borrow',
                            {'borrowed': d['id'], 'team': team_id,
                             'home': d.get('director'), 'action': 'extended'})
            continue
        d.pop('loan', None)
        _log_governance(state, team_id, 'team_borrow',
                        {'borrowed': d['id'], 'team': team_id,
                         'home': d.get('director'), 'action': 'returned'})


def _dormant_workers_will_be_woken(state, now_ms):
    """True when an off-duty (dormant) worker is about to be re-awakened for
    pending or scheduled work, so the active slot she vacated isn't actually
    free for a new hire. Mirrors the assignment loop's wake rule (see
    _task_cycle): a due, unparked, dependency-met queue item wakes a dormant
    agent when it is pinned/scheduled (reviewOf/sprintId/notBefore) or when no
    awake-idle agent is around to take it, and only while the active ceiling
    has room (can_activate_another). Standing cadences (research topics, skill
    review, distill, pipelines) queue their work in _check_schedules BEFORE
    governance runs, so the workQueue already reflects what will wake them."""
    roster = state.get('agentRoster') or []
    agents = state.get('agents') or {}
    # Any dormant (off-duty) worker at all? No dormant workers, no wake risk.
    has_dormant = any(
        not d.get('isAdmin')
        and isinstance(agents.get(d.get('id')), dict)
        and agents[d['id']].get('offDuty')
        for d in roster)
    if not has_dormant:
        return False
    if not can_activate_another(state):
        return False  # at/over the ceiling no dormant agent can be woken
    for item in (state.get('workQueue') or []):
        if not is_work_item_due(item, now_ms):
            continue
        if _is_parked(item) or not _work_item_dependency_met(state, item):
            continue
        if item.get('reviewOf') or item.get('sprintId') or item.get('notBefore'):
            return True  # pinned/scheduled work wakes regardless of idle count
        if _awake_idle_count(state) == 0:
            return True  # no awake-idle agent -> a dormant one gets woken
    return False


def _start_auto_hire(state, now_ms, grid, decider):
    """hiring.js attemptAutoHire, team-scoped: each TEAM DIRECTOR hires for
    their own team (not one global admin hiring anyone). If a director is free
    + someone on THEIR team needs help, busy that director at
    Command Center and defer the actual agent creation HIRE_DURATION_MS via a
    durable `_pendingHire` record the next pass completes. Returns True if a
    hire started. NOTE: there is no per-team lifetime size cap -- a director
    may hire unlimited workers; MAX_TEAM_MEMBERS only limits how many staff a
    single sprint (see _sprint_worker_pool)."""
    roster = state.get('agentRoster') or []
    if len(roster) >= MAX_TOTAL_AGENTS:
        state['_hireBlockedAtCapNote'] = len(roster)
        return False
    # One ceremony at a time. _complete_auto_hire overwrites _pendingOnboard,
    # so starting a second hire while a Town Hall/readiness hold is still open
    # strands the earlier hire with profile.onboarding forever. The live tank
    # hit this: 446 of 457 agents were stuck, 420 at stage 0, and the same
    # overload kept triggering new hires that could never relieve it.
    if state.get('_pendingOnboard') or state.get('_onboardQueue'):
        return False
    # Active-ceiling gate: stop spawning at MAX_ACTIVE_AGENTS (25). When a
    # sprint ends and workers go dormant, active_agent_count drops below the
    # ceiling and this gate reopens on its own. `lastHireAt` is deliberately
    # NOT advanced, so the next governance pass retries the moment a slot frees
    # (the note records WHY a blocked hire was blocked, for the UI).
    if active_agent_count(state) >= MAX_ACTIVE_AGENTS:
        state['_hireBlockedAtActiveCapNote'] = active_agent_count(state)
        return False
    # Dormant-wake refinement: workers going dormant only frees a slot for a
    # new hire if those dormant workers have no scheduled task or work they
    # will be re-awakened for. If a dormant worker is about to be woken by due
    # scheduled/pinned work (or by a due queue item with no awake-idle agent),
    # the slot is already spoken for -- hiring on top would overshoot the
    # ceiling once she wakes.
    if _dormant_workers_will_be_woken(state, now_ms):
        state['_hireBlockedDormantWakeNote'] = True
        return False
    agents = state.get('agents') or {}

    # Directors are anyone with >=1 direct report (the `director` pointer is
    # already on every roster entry). The admin/stand-in is included if they
    # lead a team; otherwise each team has its own director.
    director_ids = []
    for d in roster:
        if _sim_direct_reports(state, d.get('id')):
            director_ids.append(d.get('id'))

    # Candidate pools, grouped by hiring director: only that director's own
    # direct reports may be hired-for, so the new assistant lands in the team.
    # A director chooses which of THEIR members needs overflow help.
    pool_by_director = {}
    for did in director_ids:
        dir_agent = agents.get(did)
        if not dir_agent or dir_agent.get('busy') or dir_agent.get('offDuty'):
            continue  # this director can't run a hire right now
        members = [m for m in _sim_direct_reports(state, did)
                   if agents.get(m)]
        if not members:
            continue
        # A team carries six workers plus its scrum master. Hiring is for
        # requested work the current workers cannot absorb, never for growth
        # past that cap (the 455-person roster came from exactly this).
        if not _team_under_cap(state, did):
            continue
        pool_by_director[did] = _team_member_candidates(state, did, members, now_ms)

    if not pool_by_director:
        state['lastHireAt'] = 0  # nothing to decide
        return False

    # Cross-team borrowing comes FIRST: before a director spends a hire (a Jev
    # decision + a brand-new clone's onboarding), try to borrow an INACTIVE
    # agent from another team for the borrower team's sprint. Only when no
    # inactive agent is available do we fall through to hiring a new assistant.
    for did in pool_by_director:
        if _borrow_inactive_agent_for_team(state, did, now_ms):
            state['lastHireAt'] = now_ms
            return True

    state['lastHireAt'] = now_ms
    # Flat candidate list for the decider: each entry says which team + member.
    flat = []
    for did, cands in pool_by_director.items():
        for c in cands:
            c['directorId'] = did
            flat.append(c)
    pick = decider(state, 'Pick whoever most needs additional help right now -- weigh morale, workload signals (approved vs. dropped work), and any reports filed against them. You may only assign help WITHIN a team: choose a director who is free, then one of their own overloaded direct reports to add an assistant to. Someone already busy or off-duty cannot run a hire.', flat)
    picked = next((c for c in flat if c['id'] == pick), None) if pick else None
    if not picked:
        # Fallback: the lowest-morale report across all available teams.
        worst, worst_score = None, float('inf')
        for c in flat:
            s = morale_for(state, c['id'], now_ms)
            if s is not None and s < worst_score:
                worst_score, worst = s, c
        picked = worst
    if not picked:
        state['lastHireAt'] = 0
        return False
    director_id = picked.get('directorId') or picked.get('id')
    director = agents.get(director_id)
    if not director or director.get('busy'):
        state['lastHireAt'] = 0
        return False
    director_def = next((d for d in roster if d.get('id') == director_id), {})
    director['busy'] = True
    director['visible'] = False
    director['inRoom'] = 'commandcenter'
    director['roomX'] = 315
    director['roomY'] = 155
    director['dir'] = 'south'
    state['_pendingHire'] = {
        'adminId': director_id, 'adminName': director_def.get('name') or director_id,
        'directorId': director_id,
        'helpForId': picked['id'], 'helpForName': picked.get('name') or picked['id'],
        'at': now_ms + HIRE_DURATION_MS,
    }
    return True


def _begin_onboarding(state, agent_id, director_id, director_name,
                      help_for_id=None, help_for_name=None, coworker_ids=None,
                      now_ms=None, leader_id=None, leader_name=None):
    """Attach the onboarding marker and queue one ceremony record.

    `director_id` is who the hire reports to. `leader_id` is who runs the
    Town Hall; for a brand-new team that is the existing admin, because the
    new director may still be dormant. The queue keeps a multi-agent team
    spawn from overwriting a single `_pendingOnboard`.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    agents = state.setdefault('agents', {})
    agent = agents.get(agent_id)
    if not isinstance(agent, dict):
        return None
    profile = agent.setdefault('profile', {})
    profile['onboarding'] = {
        'stage': 0,
        'directorId': director_id,
        'provisioning': {
            'agentKey': 'pending',
            'mailbox': 'welcome_queued' if agent.get('mailbox') else 'missing',
            'accessGrant': agent.get('accessGrant'),
            'toolGrants': [],
            'firstTask': 'pending',
            'firstWeekReviewAt': now_ms + (7 * 24 * 3600 * 1000),
        },
    }
    record = {
        'agentId': agent_id,
        'directorId': director_id,
        'directorName': director_name,
        'leaderId': leader_id or director_id,
        'leaderName': leader_name or director_name,
        'helpForId': help_for_id,
        'helpForName': help_for_name or help_for_id,
        'coworkerIds': list(coworker_ids or []),
        'stage': 0,
        'at': now_ms + ONBOARD_MEET_DURATION_MS,
    }
    if state.get('_pendingOnboard') is None:
        state['_pendingOnboard'] = record
    else:
        state.setdefault('_onboardQueue', []).append(record)
    return record


def _complete_auto_hire(state, pending, grid, now_ms):
    """hiring.js finishHire: actually add the new agent to the roster + agents
    once HIRE_DURATION_MS has elapsed. Idempotent -- a restart mid-hire just
    re-runs this with the next present agents. Returns the new agent id or None."""
    admin = (state.get('agents') or {}).get(pending['adminId'])
    if admin:
        admin['busy'] = False
        admin['visible'] = True
        admin['inRoom'] = None
    help_for = (state.get('agents') or {}).get(pending['helpForId']) or {}
    # Every hire reports to a TEAM DIRECTOR (pending['directorId']), so the new
    # agent lands inside that director's team + shared dir the moment they're
    # created. Falls back to the hiring admin id only if not stamped.
    director_id = pending.get('directorId') or pending.get('adminId')
    role = f"Assistant to {pending['helpForName']}"
    # The hiring DIRECTOR (pending['adminName']) names the new employee via a
    # real generative call -- no predetermined names. All-time uniqueness is
    # enforced against _all_used_names (roster + every name ever reserved), and
    # a model outage or invalid candidate falls back to the fixed pool, which
    # itself skips all-time-used names.
    name = _hire_name_chooser(
        state, pending.get('adminName') or pending.get('adminId'),
        _all_used_names(state), role)
    # Defense in depth: the default chooser validates uniqueness itself, but a
    # substituted/test chooser must not be able to collide -- re-check against
    # the all-time set before trusting it, else fall back to the pool.
    if not name or name.lower() in _all_used_names(state):
        name = _next_hire_name(state)
    if not name:
        state.pop('_pendingHire', None)
        return None
    _remember_name(state, name)
    color_pool = HIRE_COLOR_POOL
    color = color_pool[random.randrange(len(color_pool))]
    access_grant = f"Read/write access to {pending['helpForName']}'s {help_for.get('role', '')} files and tooling."
    profile = {
        'mission': f"Support the {director_id} team by picking up overflow work for {pending['helpForName']} ({help_for.get('role', '')}).",
        'instructions': [
            f"Report to {pending['helpForName']} -- pick up whatever they flag as overloaded.",
            f"Your director is {pending['adminName']}; your work lands in the {director_id} team's shared space.",
            "Hired via Command Center -- elevated access is scoped to what you're helping with, not think tank-wide.",
        ],
        'notes': [f"Hired by {pending['adminName']} (director of the {director_id} team)."],
    }
    new_id = name.lower()
    roster = state.setdefault('agentRoster', [])
    # The start gate may no longer hold by the time the ceremony finishes
    # (a spawn, a borrowed agent, or another completion can grow the roster).
    # Re-check the hard total before appending, so a delayed hire cannot push
    # the tank past MAX_TOTAL_AGENTS.
    if len(roster) >= MAX_TOTAL_AGENTS:
        state['_hireBlockedAtCapNote'] = len(roster)
        state.pop('_pendingHire', None)
        return None
    # Reentry guard: a fired employee can never come back. Refuse the hire and
    # drop the pending record so the next pass stages a fresh (different) hire
    # instead of retrying the same forbidden one forever.
    if _is_fired(state, new_id, name):
        state.pop('_pendingHire', None)
        return None
    roster.append({
        'id': new_id, 'name': name, 'color': color, 'role': role, 'model': 'small',
        'director': director_id,   # -> resolves into the team for _derive_team_members
        'villageId': village_of_agent(state, director_id),
        'approvedCount': 0, 'droppedCount': 0, 'weekApprovals': 0,
        'mailbox': [f"Welcome aboard -- you're here to help {pending['helpForName']} with {help_for.get('role', '').lower()} work."],
        'elevatedAccess': True, 'accessGrant': access_grant, 'profile': profile,
    })
    spot = _free_outdoor_spot(state, grid)
    # A hire joins active (on foot at a free spot) only if there's room under
    # the ACTIVE ceiling; otherwise she joins the inventory dormant (off-duty),
    # exactly hiring.js finishHire's canActivateAnother() fork -- a large total
    # inventory is fine, but only MAX_ACTIVE_AGENTS are online at once.
    can_activate = can_activate_another(state)
    agents = state.setdefault('agents', {})
    agents[new_id] = {
        'id': new_id, 'name': name, 'color': color, 'role': role, 'profile': profile,
        'model': 'small', 'approvedCount': 0, 'droppedCount': 0, 'weekApprovals': 0,
        'mailbox': [{'text': f"Welcome aboard -- you're here to help {pending['helpForName']} with {help_for.get('role', '').lower()} work.", 'read': False, 'ts': now_ms}],
        'conversationLog': [], 'lastContactedAt': None, 'hiredAt': now_ms,
        'elevatedAccess': True, 'accessGrant': access_grant,
        'inRoom': None, 'roomX': None, 'roomY': None,
        'x': spot['x'], 'y': spot['y'], 'dir': 'south',
        'visible': can_activate, 'busy': False, 'meetingId': None,
        'offDuty': not can_activate,
    }
    state.pop('_pendingHire', None)
    # Phase B: the moment a hire exists, enqueue its onboarding ceremony -- the
    # director + the coworkers who'll work with it convene at Command Center,
    # then stage the new agent's AGENT.md progressively. Stamped here so a
    # _governance_pass after this hire starts the Town-Hall meeting. Colleagues
    # are the hire's intended collaborator set (its `helpFor`, plus any team
    # member already on the roster for that director).
    coworkers = [c for c in _sim_direct_reports(state, director_id) if c != new_id]
    if pending.get('helpForId') and pending['helpForId'] not in coworkers:
        coworkers.append(pending['helpForId'])
    agents[new_id]['profile'] = dict(profile)
    _begin_onboarding(
        state, new_id, director_id,
        pending.get('adminName') or director_id,
        help_for_id=pending.get('helpForId'),
        help_for_name=pending.get('helpForName') or pending.get('helpForId'),
        coworker_ids=coworkers, now_ms=now_ms,
    )
    return new_id


def _next_hire_name(state):
    """A deterministic-ish unique first name for a hire, used only as a
    fail-closed FALLBACK when the director's generative name chooser is
    unavailable. The live path is _hire_name_chooser (a real LLM call that
    makes the hire feel directed); this pool only steps in on a model outage.
    Never collides with any name the think tank has ever used, else None."""
    pool = ['maya', 'leo', 'zara', 'owen', 'lyra', 'ida', 'vela']
    used = _all_used_names(state)
    for n in pool:
        if n not in used:
            return n
    return None


# Larger pool for spawning a BRAND-NEW team's director + members.
# Distinct from _next_hire_name's small assistant pool so a think tank whose hire
# pool is exhausted can still spin up a new team for an incoming large request.
_NEW_TEAM_NAME_POOL = [
    'aria', 'briar', 'cairo', 'dune', 'elio', 'fawn', 'gull', 'hale',
    'indie', 'jove', 'kestrel', 'lian', 'marlow', 'niso', 'orion', 'penn',
    'quill', 'rune', 'sable', 'taro', 'umi', 'vireo', 'wren', 'ximena',
    'yarrow', 'zephyr', 'astra', 'baxter', 'cedar', 'dax', 'ember', 'fox',
]


def _next_new_team_name(state):
    """A unique name for a new-team director or member, drawn from
    _NEW_TEAM_NAME_POOL. Never collides with any name the think tank has ever
    used (_all_used_names includes retired/fired names, so a new team never
    re-takes one). Returns None when exhausted."""
    used = _all_used_names(state)
    for n in _NEW_TEAM_NAME_POOL:
        if n not in used:
            return n
    return None


def _all_used_names(state):
    """Every first name the think tank has EVER used: the current roster plus
    `_usedNames`, the durable all-time accumulation (fired/retired agents keep
    their name reserved forever, so a director never re-hires a 'jane' after
    jane was let go). Lowered for collision-safe comparisons."""
    used = {str(d.get('name', '')).lower() for d in (state.get('agentRoster') or [])}
    used.update(state.get('_usedNames') or [])
    return used


def _remember_name(state, name):
    """Durably reserve a first name forever (all-time uniqueness). Called at
    every server-side creation AND when an agent is fired, so a retired name is
    never offered to a director again. Roster-only checks would forget a fired
    agent's name the moment it left the roster -- this list does not."""
    if not name:
        return
    low = name.strip().lower()
    if not low:
        return
    state.setdefault('_usedNames', [])
    if low not in state['_usedNames']:
        state['_usedNames'].append(low)


def _is_fired(state, agent_id, name=None):
    """True when `agent_id` (or its name, when given) is on the durable
    fired-employee blocklist. The reentry guard checked at every agent-creation
    gate: a fired employee can never reenter the think tank, no matter which
    hire path (auto-hire, large-request team spawn) or generative name chooser
    is involved. The blocklist itself is written at the moment of firing (see
    _resolve_firing_review) and survives the fired agent leaving the roster."""
    fired = state.get('firedAgents') or {}
    if agent_id in fired:
        return True
    if name:
        low = name.strip().lower()
        return any(str(v.get('name') or '').strip().lower() == low
                   for v in fired.values())
    return False


def _name_chooser_default(state, chooser_name, used_names, for_role):
    """Default generative name chooser for a hire, late-importing serve.py so
    sim.py stays unit-testable offline. The hiring DIRECTOR (`chooser_name`,
    persona) picks a real, ordinary first name for the new agent (`for_role`),
    told exactly which names are already taken all-time. "Asked nicely" isn't
    "verified": the returned name is only trusted if it is a plain ASCII
    alphabetic word AND genuinely absent from `used_names` -- anything else
    returns None so the caller falls back to a pool name instead of risking an
    id collision. Mirrors hiring.js generateHireProfile's shape, but name-only
    (no profile fields) so a hire stays one cheap small-tier call. The call's
    cost is accrued to its own service bucket like every other model call."""
    import serve
    try:
        model = serve._low_tier_slug()
        listed = ', '.join(sorted(str(u) for u in used_names)) or '(none)'
        prompt = (
            f"You are {chooser_name}, a director at a think tank who just hired "
            f"someone as \"{for_role}\". Pick a real, ordinary first name for them "
            f"-- it must NOT be any of these already-used names: {listed}. "
            "Respond with ONLY the name: a single word, alphabetic characters only, no punctuation."
        )
        data = serve._call_openrouter_sync(
            model,
            [{'role': 'system', 'content': prompt},
             {'role': 'user', 'content': 'Pick the name.'}],
            max_tokens=10)
        cost = (data.get('usage') or {}).get('cost', 0.0)
        if isinstance(cost, (int, float)) and cost:
            serve._accrue_spend('__hire_names__', cost)
        reply = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        token = reply.strip().strip('"').split()[0] if reply.strip() else ''
        token = token.strip('.,;:!?')
        if token.isalpha() and token.isascii():
            name = token.capitalize()
            if name.lower() not in used_names:
                return name
        return None
    except Exception:
        return None


# Injectable so tests substitute a deterministic chooser; the live loop uses
# the default (the hiring director names the hire via a real model call).
# Mirrors how _governance_decider is injected.
_hire_name_chooser = _name_chooser_default
