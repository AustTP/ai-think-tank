

def resolve_player_ask(state, message_id, answer):
    """The player's reply to an awaiting inbox message. Marks it answered,
    clears the issue's needsInput, unblocks the agent, and stamps the reply onto
    the agent's task so it resumes with the answer as context. Returns the
    updated inbox message, or None if unknown/not-awaiting."""
    messages = state.get('playerInbox') or []
    m = next((x for x in messages if x.get('id') == message_id), None)
    if not m or m.get('status') != 'awaiting_input':
        return None
    m['status'] = 'answered'
    m['answer'] = (answer or '').strip()
    m['answeredAt'] = int(time.time() * 1000)
    issue = (state.get('issues') or {}).get(m.get('issueKey'))
    if issue:
        issue['needsInput'] = False
        agent_id = m.get('agentId')
        a = (state.get('agents') or {}).get(agent_id)
        if a and isinstance(a.get('profile'), dict):
            notes = a['profile'].setdefault('notes', [])
            notes.append(f"Player on {m['issueKey']} ({issue.get('summary', '')}): {m['answer']}")
        _unblock_agent(state, agent_id)
        # Wake-on-mail: the card's agent must act on the answer. _deliver_mail
        # wakes her if she is off-duty (a woken-but-idle mail agent is then
        # routed by _mail_action_step); if she's already busy, this is a no-op
        # mailbox note and she resumes through the normal locked-task path.
        _deliver_mail(state, agent_id, 'player_answer',
                      {'issueKey': m['issueKey'], 'answer': m['answer'],
                       'text': f"Player answered on {m['issueKey']}: {m['answer']}"})
        # The player answered, so the single blocked field now clears: queue the
        # SM's blocked=false (deterministic -- no Jev needed on the player reply).
        _file_block_change(state, m['issueKey'], False, agent_id,
                           'unblock_response', m['answer'])
    return m


def _pending_player_ask_sweep(state, now_ms, decider=None):
    """Cadence sweep (called from _check_schedules): spin any due pending
    player-ask through the director gate. Returns the delivered inbox id /
    'internal' if it advanced, else None if nothing due."""
    pend = state.get('_pendingPlayerAsk') or {}
    issue_key = pend.get('issueKey')
    if not issue_key:
        return None
    if not _director_gate_due_for(state, issue_key, now_ms):
        return None
    return request_player_input_verdict(state, issue_key, decider=decider)


def complete_player_task(state, task_id, now_ms=None):
    """Human-in-the-loop: the player marks an assigned-to-them task done. The
    task (a 'needs_player' card minted by _assign_player_task) flips to 'done',
    which makes the existing dependency machinery release anything queued
    behind it:
      - agent cards queued depends_on_task=<this id> become assignable
        (_dependency_landed is now true) on the next task cycle;
      - any ISSUE committed blocked on this task is auto-unblocked and the
        waiting agent is woken (_auto_clear_dependency_blocks).
    The player-inbox card is marked done, a confirmation email is queued, and
    the action is logged. Returns the completed task dict, or None if the task
    id is unknown / not a player-owned card / already done."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    tasks = state.get('tasks') or {}
    task = tasks.get(task_id)
    if not isinstance(task, dict):
        return None
    if task.get('assignedTo') != 'player':
        return None
    if task.get('status') != 'needs_player':
        return None
    task['status'] = 'done'
    task['completedAt'] = now_ms
    # Any issue committed blocked on this task frees up, and its waiting agent
    # is woken -- the exact same release finish_task triggers when an agent's
    # story ships.
    _auto_clear_dependency_blocks(state, task_id, now_ms=now_ms)
    # Mark the player-inbox card done so the UI stops listing it as waiting.
    mid = task.get('playerInboxId')
    if mid:
        for m in (state.get('playerInbox') or []):
            if m.get('id') == mid:
                m['status'] = 'done'
                m['answeredAt'] = now_ms
                break
    title = task.get('title') or task_id
    _queue_player_email(
        state, 'player_task_done',
        f"[AI Think Tank] Your task is complete: {title}",
        (f"You marked '{title}' done. The think tank has picked the work back up: "
         f"anything that was queued behind your task resumes now."),
        now_ms=now_ms)
    from serve import log_action
    log_action('player', 'task_completed',
               {'taskId': task_id, 'title': title}, authorized=False)
    return task


# ---------------------------------------------------------------------------
# SM-committed `blocked` field. issue['blocked'] is a single
# player-facing boolean ONLY the owning team's scrum master flips. Every set and
# clear funnels through a block-change REQUEST the SM ceremony commits:
#   set (requirements-met) : agent claims -> supervisor Jev approves -> SM commits True
#   set (stuck-on-player)  : director ask_player verdict -> SM commits True (card
#                            is blocked pending the player's answer)
#   unset (player answered / agent self-resolved): deterministic -> SM commits False
# The SM's commit is MECHANICAL (judgment is always upstream), so _block_step
# never spends Jev or convenes a meeting -- it validates the request, flips the
# field, and logs. Mirrors the escalation-groom's injectable-decider shape only
# for the requirements-met supervisor vote.
# ---------------------------------------------------------------------------
BLOCK_CHANGE_KINDS = (
    'requirements_met',     # agent says spec satisfied; supervisor must confirm
    'stuck_player',         # director already approved asking the player
    'stuck_on_agent',       # agent blocked on another agent's task (no gate)
    'unblock_response',     # player answered the inbox ask
    'unblock_resolved',     # agent/self resolved without the player
    'unblock_landed',       # the dependency task A was waiting on completed
)


def _file_block_change(state, issue_key, wanted, requester_id, kind, reason,
                       depends_on_task=None, now_ms=None):
    """Queue a request for the scrum master to set issue['blocked'] = `wanted`.
    Dedupes: no second live request for the same issue while one is pending.
    Returns the request id, or None if the issue is unknown or a live request
    already exists. Pure mutation of state['_pendingBlockChanges']. A dependency
    block (`stuck_on_agent`) carries `depends_on_task` so completion auto-clears
    it (see _auto_clear_dependency_blocks)."""
    issue = (state.get('issues') or {}).get(issue_key)
    if not issue:
        return None
    if kind not in BLOCK_CHANGE_KINDS:
        return None
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    for r in (state.get('_pendingBlockChanges') or []):
        if r.get('issueKey') == issue_key and r.get('state') == 'pending':
            return None  # already queued; another gate/path must not double-file
    team_id = issue.get('teamId')
    rid = f"blk-{issue_key}-{int(now_ms)}"
    state.setdefault('_pendingBlockChanges', []).append({
        'id': rid, 'issueKey': issue_key, 'teamId': team_id,
        'wanted': bool(wanted), 'requesterId': requester_id,
        'kind': kind, 'reason': (reason or '').strip(),
        'dependsOnTask': depends_on_task or None,
        'at': now_ms, 'state': 'pending', 'committed': False,
    })
    return rid


def _append_mailbox(agent, entry):
    """Append one entry to an agent's mailbox and trim to MAILBOX_KEEP_COUNT.
    Every mailbox write goes through here so retention is enforced on ALL
    append paths (peer-review requests, rejected-author notes, gate re-picks,
    wake-on-mail) -- not just _deliver_mail, which is what MAILBOX_KEEP_COUNT
    originally forgot (audit). Every entry also carries the `read` flag the
    unread surfaces filter on: absent means unread to every reader
    (`.get('read')` is falsy), so stamping it here makes the flag uniform
    across append paths instead of only the browser-mail and wake paths."""
    entry.setdefault('read', False)
    mailbox = agent.setdefault('mailbox', [])
    mailbox.append(entry)
    if len(mailbox) > MAILBOX_KEEP_COUNT:
        archived = agent.setdefault('mailboxArchive', [])
        archived.extend(mailbox[:-MAILBOX_KEEP_COUNT])
        if len(archived) > MAILBOX_ARCHIVE_KEEP_COUNT:
            agent['mailboxArchive'] = archived[-MAILBOX_ARCHIVE_KEEP_COUNT:]
        agent['mailbox'] = mailbox[-MAILBOX_KEEP_COUNT:]


# BP-62: close-out + triage mail. A cross-team card's reporter learns what the
# OWNER did with their card the way the tank talks about everything else -- a
# quiet mailbox note. Mail never wakes the recipient (the send_mail rule): the
# reporter reads it on their next duty cycle. The player is skipped -- the
# player-facing UI and digest surfaces carry the same information -- and
# unknown or missing roster ids fail silently to a no-op.
_CARD_STATUS_MAIL_KINDS = {
    'in_progress': 'issue_started',
    'done': 'issue_done',
    'closed': 'issue_closed',
}


def _mail_card_update(state, to_id, kind, key, title, team_name, extra=''):
    """One no-wake mailbox note from the owning side of a card to its
    reporter. kind: request_carded | request_declined | issue_started |
    issue_done | issue_closed. Returns True when a note landed. Pure."""
    if not isinstance(to_id, str) or not to_id or to_id == 'player':
        return False
    agent = (state.get('agents') or {}).get(to_id)
    if not isinstance(agent, dict) or not agent:
        return False
    label = f'{key} ' if key else ''
    title_bit = (title or '')[:120]
    if kind == 'request_carded':
        text = (f'Your card {label}"{title_bit}" was groomed into the '
                f'sprint by {team_name or "the team"} -- it is queued and '
                'starts when a worker picks it up.')
    elif kind == 'request_declined':
        text = (f'Your card {label}"{title_bit}" was groomed out during '
                f'backlog refinement by {team_name or "the team"}'
                + (f': {extra}' if extra else '.')
                + ' File a follow-up with sharper scope if it still matters.')
    elif kind == 'issue_started':
        text = (f'{team_name or "The team"} picked up your card {label}'
                f'"{title_bit}" -- work is in progress.')
    elif kind == 'issue_done':
        text = (f'Your card {label}"{title_bit}" is marked done'
                f'{f" by {team_name}" if team_name else ""}. Reply in mail '
                'if anything looks off.')
    elif kind == 'issue_closed':
        text = (f'Your card {label}"{title_bit}" is closed'
                f'{f" by {team_name}" if team_name else ""}.')
    else:
        return False
    _append_mailbox(agent, {'kind': kind, 'about': key or None,
                            'title': title_bit, 'text': text})
    return True


def _register_card_appeal(state, reporter_id, issue_key, reply_text, now_ms=None,
                          via_sm=None):
    """BP-62 appeal path: the reporter of a DECLINED card replied in mail with
    new information, and the reply re-surfaces the card exactly once. The
    rejected backlogRequest goes back to 'pending' carrying the reply snippet,
    so the very next refinement ceremony for the owning team reconsiders it in
    the open instead of past the reporter's head.

    Guards: the replier must be the card's own reporter (a bystander citing
    the issue key never triggers an appeal), the request must be currently
    'rejected' (a pending/accepted card needs no appeal), and each card gets
    ONE appeal -- a second reply after a second decline ends the conversation
    through the standing process (file a follow-up with sharper scope), keeping
    the ceremony from being revived indefinitely.

    Pure: mutates the request record and appends the SM's mailbox note.
    Returns True when an appeal landed, else False."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    key = (issue_key or '').strip()
    if not key or reporter_id == 'player' or not reporter_id:
        return False
    req = next((r for r in (state.get('backlogRequests') or [])
                if r.get('issueKey') == key and r.get('status') == 'rejected'),
               None)
    if not isinstance(req, dict) or req.get('appealedAt'):
        return False
    if req.get('filedBy') != reporter_id:
        return False
    issue = (state.get('issues') or {}).get(key)
    if not issue:
        return False
    team = _team_row(state, req.get('teamId'))
    sm_id = (team or {}).get('scrumMasterId')
    if not sm_id or (via_sm is not None and via_sm != sm_id):
        return False
    sm = (state.get('agents') or {}).get(sm_id)
    if not sm:
        return False
    snippet = ((reply_text or '').strip())[:300] or '(the reply carried no text)'
    req['status'] = 'pending'
    req['appealedAt'] = now_ms
    req['appeal'] = snippet
    reporter = next((d for d in (state.get('agentRoster') or [])
                     if isinstance(d, dict) and d.get('id') == reporter_id), {}) or {}
    _append_mailbox(sm, {
        'kind': 'issue_appeal', 'about': key,
        'title': f'Appeal: {issue.get("title") or key}',
        'text': (f'{reporter.get("name") or reporter_id} appealed the earlier decline '
                 f'of card {key}: "{snippet}" -- it re-enters your NEXT refinement '
                 f'ceremony. One appeal per card: after a second decline, the reply '
                 'path is filed follow-ups with sharper scope.'),
    })
    _log_governance(state, sm_id, 'issue_appeal_registered',
                    {'issue': key, 'reporter': reporter_id})
    return True


def _upsert_rejection_mailbox(agent, entry):
    """File a rejection/sent-back notice, but DEDUP: if an unread entry with
    the same kind and the same `about` (task id) already exists, update its
    text and timestamp in place instead of appending a duplicate. Agents
    reported the same rejection appearing 3-5 times in their mailbox with no
    marker for which copy was current -- this makes the latest notice the one
    entry for that task. Falls back to a plain append when no match (a new
    task, or all prior copies already read)."""
    mailbox = agent.setdefault('mailbox', [])
    kind = entry.get('kind')
    about = entry.get('about')
    if kind and about:
        for existing in mailbox:
            if (existing.get('kind') == kind and existing.get('about') == about
                    and not existing.get('read', False)):
                existing['text'] = entry.get('text', existing.get('text'))
                existing['ts'] = entry.get('ts', existing.get('ts'))
                if 'title' in entry:
                    existing['title'] = entry['title']
                return
    _append_mailbox(agent, entry)


def _deliver_mail(state, agent_id, kind, payload=None):
    """File one action-needed mail item into an agent's mailbox. If the
    recipient is OFF-DUTY, wake her (appear_from_outskirts -- lazily loads
    geometry, clears stale busy/task) and set `_mailAwake` so the park-idle pass
    does not re-park her before she has acted on the mail. Pure: mutates
    `state` only. Returns True when dispatched, False for an unknown agent."""
    now_ms = int(time.time() * 1000)
    a = (state.get('agents') or {}).get(agent_id)
    if not isinstance(a, dict):
        return False
    entry = {
        'kind': kind,
        'read': False,
        'ts': now_ms,
        'text': (payload or {}).get('text', ''),
    }
    if payload:
        for k in ('issueKey', 'taskId', 'about'):
            if k in payload:
                entry[k] = payload[k]
    _append_mailbox(a, entry)
    if a.get('offDuty'):
        # Wake the affected agent to act on the mail. Waking from an
        # off-duty rest is the whole point of wake-on-mail.
        appear_from_outskirts(state, agent_id)
        a['_mailAwake'] = {'kind': kind, 'entry': entry, 'at': now_ms}
        _wake_enqueue(state, agent_id, kind, entry, now_ms)
    return True


def _mail_action_step(state, now, now_ms):
    """Called from _task_cycle (next to _block_step): for each agent holding an
    unacted `_mailAwake` marker, route her to the work the mail references so she
    is assigned and can act, then clear the marker once she is busy/working.
    Without a task, a woken mail agent would be parked by _park_idle_wanderers in
    the same tick (fully_idle) -- the marker is what keeps her awake until acted.
    Returns the number of agents routed. Pure state mutation; no Jev."""
    routed = 0
    _wake_recover_stale(state, now_ms)
    _wake_restore_markers(state, now_ms)
    agents = state.get('agents') or {}
    for aid, a in list(agents.items()):
        if not isinstance(a, dict) or not a.get('_mailAwake'):
            continue
        marker = a['_mailAwake']
        if a.get('busy') or a.get('task') or a.get('handoff') or a.get('pairWith'):
            # She is now acting (or already working); hand off to normal flow.
            a.pop('_mailAwake', None)
            _wake_complete_for_agent(state, aid, marker.get('kind'),
                                     marker.get('entry') or {}, 'acting', now_ms)
            routed += 1
            continue
        kind = marker.get('kind') or ''
        entry = marker.get('entry') or {}
        payload = entry
        # (Re)queue the referenced work so she is assigned it.
        queued = _queue_mail_work(state, aid, kind, payload, now_ms)
        if queued:
            a.pop('_mailAwake', None)
            _wake_complete_for_agent(state, aid, kind, payload, 'routed', now_ms)
            routed += 1
        elif not isinstance(payload, dict) or not (
                payload.get('issueKey') or payload.get('taskId')
                or payload.get('about')):
            # Unroutable system notice (no card/task to re-arm -- e.g. the
            # admin's watchdog escalation copy): stop retrying, or the marker
            # holds her awake (and unparkable) forever with nothing to queue.
            a.pop('_mailAwake', None)
            _wake_complete_for_agent(state, aid, kind, payload, 'unroutable', now_ms)
    return routed


def _queue_mail_work(state, agent_id, kind, payload, now_ms):
    """Push a workQueue item so `aid` acts on the mail's referenced card/task.
    Menu of kinds: `player_answer` / `dependency_landed` re-arm the card's task
    for its owner; `peer_review_request` / `peer_review_rejected` re-arm the
    target task for a (re)review; `sim_watchdog` queues the thin investigate
    item (its goal names no issue/task, so it must never reach
    _requeue_task_for_agent, which would stamp the sentinel as reviewOf).
    Returns True if an item was queued."""
    if kind == 'sim_watchdog':
        return _queue_watchdog_alert(state, agent_id, payload, now_ms)
    # A help_signal is advisory personnel context for the director: "this agent
    # has done little real work, check in before assuming." It is not a card.
    # Queuing it produced "Act on mail (<agent>)" in the code room, which ran
    # the Python gate, failed for missing tests, and generated the next fix
    # card forever. Acknowledge the mail and let the director decide.
    if kind == 'help_signal':
        return True
    issue_key = (payload or {}).get('issueKey')
    task_id = (payload or {}).get('taskId') or (payload or {}).get('about')
    if issue_key:
        return _requeue_card_task(state, agent_id, issue_key, now_ms)
    if task_id:
        return _requeue_task_for_agent(state, agent_id, task_id, now_ms)
    return False


def _queue_watchdog_alert(state, agent_id, payload, now_ms):
    """Queue the director's investigate item for a `sim_watchdog` mail. Thin on
    purpose: the alert text is the instructions, the goal is a fixed sentinel
    (help_signal precedent: a goal that names no issue/task), no reviewOf
    (nothing to peer-review a system notice against), room-less so
    _assign_due_item resolves a real room. Returns True if queued."""
    queue = state.get('workQueue')
    if not isinstance(queue, list):
        return False
    text = (payload or {}).get('text') or ''
    queue.append({
        'title': 'Investigate sim-loop watchdog alert',
        'room': None,
        'instructions': text or 'Investigate the sim-loop watchdog alert now that you have been notified.',
        'goal': 'sim-watchdog',
        # A watchdog investigation is a research lane, NOT the default code
        # lane. The senior director (Nora when she is the top director) is
        # excluded from code work by governance.may_work_code, so a default
        # code item pinned to her was filtered out at assignment and the
        # alert never got an owner.
        'taskType': 'research',
        'assignedTo': agent_id,  # wake-on-mail pin: THIS agent investigates
        '_mailResume': True,
        'notBefore': now_ms,
    })
    return True


def _deliver_watchdog_event(state, evt):
    """Deliver one sim-watchdog failure event (R6) inside the sim's single
    read-modify-write. Ladder: the senior director is mailed AND pinned an
    investigate work item (directly when on-duty -- no _mailAwake marker, so
    _mail_action_step would never route it; via the marker when off-duty);
    once the streak reaches 2 restarts the admin is ALSO mailed (distinct
    kind -- isAdmin is excluded from assignment, so the admin copy must never
    queue work) and an audit row is filed. The player is NEVER messaged here:
    serve's watchdog owns the last-resort Telegram when nothing in the tank
    can resolve the outage. Pure state mutation (log_action writes only the
    action_log table, same as help_signal)."""
    now_ms = int(time.time() * 1000)
    kind = (evt or {}).get('kind') or 'unknown'
    detail = (evt or {}).get('detail') or ''
    try:
        restarts = int((evt or {}).get('restarts') or 1)
    except Exception:
        restarts = 1
    text = (f'Sim-loop watchdog alert ({kind}): {detail}. '
            f'Consecutive restart attempt #{restarts}. Investigate the root '
            f'cause now; if it keeps recurring, escalate to the admin.')
    senior = _senior_director(state)
    senior_id = senior.get('id') if senior else None
    if not senior_id:
        print('[sim] watchdog event: no senior director to notify', flush=True)
    else:
        _deliver_mail(state, senior_id, 'sim_watchdog', {'text': text})
        a = (state.get('agents') or {}).get(senior_id)
        if isinstance(a, dict) and not a.get('_mailAwake'):
            # On-duty: no marker, so route the pinned item now (an off-duty
            # director's marker routes it on the next tick instead).
            _queue_mail_work(state, senior_id, 'sim_watchdog',
                             {'text': text}, now_ms)
    if restarts >= 2:
        admin = next((d for d in (state.get('agentRoster') or [])
                      if d.get('isAdmin')), None)
        admin_id = admin.get('id') if admin else None
        if admin_id and admin_id != senior_id:
            _deliver_mail(state, admin_id, 'sim_watchdog_escalated',
                          {'text': f'Watchdog escalation (senior director '
                                   f'notified): {text}'})
        try:
            import serve
            serve.log_action(admin_id or senior_id, 'sim_watchdog_escalated',
                             {'kind': kind, 'restarts': restarts,
                              'directorId': senior_id}, authorized=False)
        except Exception as e:
            print(f'[sim] watchdog escalation log error: {e}', flush=True)


def _requeue_card_task(state, agent_id, issue_key, now_ms):
    """Re-arm the workQueue for a card so its owning agent (or the woken agent)
    is assigned it next pass. If the owner is already busy, no-op (she's acting).
    Pure; returns True if an item was queued."""
    issue = (state.get('issues') or {}).get(issue_key)
    if not issue:
        return False
    queue = state.get('workQueue')
    if not isinstance(queue, list):
        return False
    title = issue.get('title') or issue.get('summary') or 'Resume card work'
    # Room: a feature slug is NOT guaranteed to be a room that has a door
    # (a card's feature is a story lane, not geometry), and the old
    # `or 'townsquare'` fallback named a room that exists nowhere in
    # door_triggers.json -- so the item could never be assigned. Queue it
    # ROOM-LESS and let _assign_due_item resolve a real one, same as a
    # shared-backlog card.
    queue.append({
        'title': title,
        'room': issue.get('feature') or None,
        'instructions': f"Resume work on {issue_key} now that you are unblocked: {issue.get('summary', '')}",
        'goal': issue_key,
        'taskType': issue.get('type'),
        'issueKey': issue_key,
        'assignedTo': agent_id,  # wake-on-mail pin: THIS agent resumes the card
        '_mailResume': True,
        'notBefore': now_ms,
    })
    return True


def _requeue_task_for_agent(state, agent_id, task_id, now_ms):
    """Re-arm an existing task (e.g. a peer review) so `agent_id` is assigned it
    again next pass. Returns True on success."""
    queue = state.get('workQueue')
    if not isinstance(queue, list):
        return False
    # `about` is a TASK id for peer_review_request/rejected mail, but an AGENT
    # id for a help_signal ("Act on mail (harold)" -- the director was mailed
    # about harold). reviewOf means "peer-review subtask of THAT task"
    # everywhere downstream (wake rules, reporting through the parent, the
    # attempts exemption), so stamping an agent id on it both lies to the
    # lifecycle and made the item immortal. Keep reviewOf for a task id; drop
    # it when the value names an employee instead.
    agents = state.get('agents') or {}
    real_task_id = None if task_id in agents else task_id
    # A follow-up on an existing task keeps that task's lane. Without this an
    # untyped re-arm silently defaulted to the code lane in the content
    # dispatcher, so a review/research follow-up ran the Python gate and could
    # never satisfy it. A code parent keeps the existing default (None -> code).
    parent = ((state.get('tasks') or {}).get(real_task_id) or {}) if real_task_id else {}
    parent_type = parent.get('taskType')
    if parent_type in (None, '', 'None', 'code'):
        parent_type = None
    # No hardcoded room: 'townsquare' has no door, so assign_task failed on it
    # every cycle (the 21k-attempt wedge this item caused). Room-less lets
    # _assign_due_item resolve a real room at assignment time. A parent task's
    # room wins, so the follow-up lands in the same workspace/lane.
    queue.append({
        'title': f'Act on mail ({task_id})',
        'room': parent.get('room') or None,
        'instructions': f'Follow up on {task_id} now that you have been notified.',
        'goal': task_id,
        'taskType': parent_type,
        'projectLabel': parent.get('projectLabel') or None,
        'reviewOf': real_task_id,
        'assignedTo': agent_id,  # wake-on-mail pin: THIS agent acts on the mail
        '_mailResume': True,
        'notBefore': now_ms,
    })
    return True


def _block_step(state, now, now_ms):
    """The SM-commit pass, called from _task_cycle (mirrors _escalation_step).
    Commits ONE pending block-change whose owning team's scrum master is free +
    on-duty: flips issue['blocked'], stamps blockedAt/blockedBy/unblockedAt, and
    releases any agent parked waiting on it. Mechanical (no LLM). Returns the
    committed request dict, or None if nothing was due."""
    changes = state.get('_pendingBlockChanges') or []
    pending = [c for c in changes if c.get('state') == 'pending']
    if not pending:
        return None
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    # Respect a per-team cadence so one SM isn't flipping cards every tick, and
    # only ONE request per pass (others wait until a later pass).
    for c in pending:
        issue = (state.get('issues') or {}).get(c.get('issueKey'))
        team_id = c.get('teamId')
        t = _team_row(state, team_id)
        if not t:
            continue
        # The commit is MECHANICAL (judgment is always upstream), so ANY effective
        # scrum master may sign it -- for a small team that is the director
        # standing in, the same fallback refinement uses. Without even that, defer.
        scrum_master_id = _refinement_scrum_master_for_team(state, team_id)
        if not scrum_master_id:
            continue  # no effective scrum master yet -- defer
        a = (state.get('agents') or {}).get(scrum_master_id)
        if not a or a.get('busy') or a.get('offDuty'):
            continue  # SM busy/off-duty -- defer to a later pass
        stamps = (state.get('teamBlockAt') or {})
        if now_ms - (stamps.get(team_id) or 0) < BLOCK_CHANGE_CADENCE_MS:
            continue
        if not issue:
            continue
        # Commit. The judgment (supervisor/director approval or a deterministic
        # player-response/self-resolve) already happened upstream -- the SM only
        # makes the authoritative field change.
        c['state'] = 'committed'
        c['committed'] = True
        c['committedAt'] = now_ms
        c['committedBy'] = scrum_master_id
        wanted = c['wanted']
        issue['blocked'] = wanted
        if wanted:
            issue['blockedAt'] = now_ms
            issue['blockedBy'] = scrum_master_id
            issue['unblockedAt'] = None
            # Real-email: tell the player a story/spike they care about is blocked.
            _queue_player_email(
                state, 'card_blocked',
                f"[AI Think Tank] {c['issueKey']} is blocked",
                (f"{c['issueKey']} ({issue.get('title') or issue.get('summary') or 'unknown story'}) "
                 f"is blocked: {issue.get('blockedKind') or 'blocked'}.\n"
                 f"- Agent: {c.get('requesterId') or 'unknown'}\n"
                 f"- Reason: {c.get('reason') or 'no reason given'}\n"
                 f"Take a look in the think tank issue register if this needs your input."),
                now_ms=now_ms)
            # Store the block's provenance so a review detail can explain WHY the
            # card is blocked. `stuck_on_agent` also carries the dependency task id
            # the auto-clear path keys on. If the card is ALREADY blocked for a
            # different reason, keep that reason (a dependency set on an existing
            # requirements-met card shouldn't wipe the card's true reason).
            if not issue.get('blockedKind'):
                issue['blockedKind'] = c.get('kind')
            if c.get('dependsOnTask'):
                issue['dependsOnTask'] = c.get('dependsOnTask')
        else:
            issue['unblockedAt'] = now_ms
            issue['blockedBy'] = None
            issue['blockedKind'] = None
            issue['dependsOnTask'] = None
        # A `stuck_player` commit means the agent is parked waiting on the player;
        # an unblock (player answered or self-resolved) absolutely must free them.
        if not wanted:
            _unblock_agent(state, c.get('requesterId'))
        _log_block_commit(state, c, issue, now_ms)
        state.setdefault('teamBlockAt', {})[team_id] = now_ms
        return c
    return None


def _log_block_commit(state, c, issue, now_ms):
    """Governance log row for an SM block-field commit."""
    try:
        entries = state.setdefault('actionLog', [])
        entries.append({
            'ts': now_ms, 'actor': c.get('committedBy'),
            'action': 'issue_block',
            'data': {
                'issueKey': c.get('issueKey'),
                'blocked': issue.get('blocked'),
                'kind': c.get('kind'),
                'requester': c.get('requesterId'),
                'reason': c.get('reason'),
            },
        })
    except Exception:
        pass


def request_block_dependency(state, issue_key, agent_id, depends_on_task_id,
                             reason='', now_ms=None):
    """Agent A files that it's blocked because another agent's task (`depends_on
    task_id`) hasn't landed. Per the user's decision there's NO Jev gate on this
    path -- naming the dependency task is objective enough -- so this directly
    queues an SM commit of blocked=true, tagged `stuck_on_agent` + the task it's
    waiting on. Returns the request id, or None if the issue is unknown/there's
    already a live request (or it can't reference a task that doesn't exist yet
    is allowed -- the dependency may be in progress)."""
    if _issue_director(state, issue_key) is None:
        return None  # no owning team to route the SM commit through
    issue = (state.get('issues') or {}).get(issue_key)
    if not issue:
        return None
    return _file_block_change(state, issue_key, True, agent_id, 'stuck_on_agent',
                              (reason or f"Blocked waiting on task {depends_on_task_id}").strip(),
                              depends_on_task=depends_on_task_id, now_ms=now_ms)


def _auto_clear_dependency_blocks(state, completed_task_id, now_ms=None):
    """Deterministic auto-clear: when a task lands, any card blocked (or waiting
    on a pending block-change) in a `stuck_on_agent` state whose dependency was
    that task gets an `unblock_landed` request, so the SM commits blocked=false on
    the next _block_step. Agent A resumes with its dependency satisfied. Returns
    the number of unblocks filed."""
    if not completed_task_id:
        return 0
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    clears = 0
    # The completed task's STORY identity, so a woken agent knows what freed them
    # up ("task T landed"), not just an opaque task id.
    landed_title = None
    landed_task = (state.get('tasks') or {}).get(completed_task_id)
    if isinstance(landed_task, dict):
        landed_title = landed_task.get('title') or None
    # (a) Cards already committed blocked on this dependency.
    for issue in (state.get('issues') or {}).values():
        if (issue.get('blocked') and issue.get('blockedKind') == 'stuck_on_agent'
                and issue.get('dependsOnTask') == completed_task_id):
            if _file_block_change(state, issue['key'], False, 'auto',
                                  'unblock_landed',
                                  f"dependency task {completed_task_id} landed",
                                  now_ms=now_ms):
                clears += 1
                # Wake-on-mail: the agent waiting on this dependency must resume
                # her card now that it can move. Recover the requester from the
                # block-change that originally filed the stuck_on_agent.
                waiting_agent = _dependency_requester(state, issue['key'],
                                                      completed_task_id)
                if not waiting_agent:
                    continue
                # LOOP GUARD: don't wake an agent whose own card just freed if she
                # is STILL blocked on ANOTHER story's dependency. Waking her would
                # be churn -- she can't roll that other card forward and it would
                # fire its own wake when its dependency lands. (She'll resume via
                # the normal assignment path once genuinely unblocked everywhere.)
                if _agent_still_blocked(state, waiting_agent, except_task_id=completed_task_id):
                    continue
                what = landed_title or f"task {completed_task_id}"
                _deliver_mail(
                    state, waiting_agent, 'dependency_landed',
                    {'issueKey': issue['key'], 'taskId': completed_task_id,
                     'text': (f"{what} landed; {issue['key']} is unblocked. "
                              f"Resume your work on that card.")})
    # (b) Pending (not-yet-committed) dependency blocks on this task -- drop the
    # queue entry entirely; no need to set the field it never got to set.
    changes = state.get('_pendingBlockChanges') or []
    for c in list(changes):
        if (c.get('state') == 'pending'
                and c.get('kind') == 'stuck_on_agent'
                and c.get('dependsOnTask') == completed_task_id):
            changes.remove(c)
            clears += 1
    return clears


def _pending_block_changes(state, issue_key):
    """Live (uncommitted) block-change requests for one issue, newest first."""
    out = [c for c in (state.get('_pendingBlockChanges') or [])
           if c.get('issueKey') == issue_key and c.get('state') == 'pending']
    return out


def _dependency_requester(state, issue_key, completed_task_id):
    """Recover the agent who originally filed the `stuck_on_agent` block on
    `issue_key` against `completed_task_id`, so wake-on-mail can route the resume
    to the right robot. Looks across committed and pending block-changes; returns
    an agent id or None."""
    for c in (state.get('_pendingBlockChanges') or []):
        if (c.get('issueKey') == issue_key
                and c.get('kind') == 'stuck_on_agent'
                and c.get('dependsOnTask') == completed_task_id
                and c.get('requesterId') and c.get('requesterId') != 'auto'):
            return c['requesterId']
    return None


def _agent_still_blocked(state, agent_id, except_task_id=None):
    """LOOP GUARD for wake-on-mail: True when `agent_id` is still waiting on
    ANOTHER story's unmet dependency (a committed `stuck_on_agent` block on some
    OTHER dependency task). Waking such an agent is pointless churn -- she can't
    roll that other card forward, and it will fire its own wake when ITS
    dependency lands. `except_task_id` skips the dependency that just landed.
    Returns False when the agent is not waiting on any (other) dependency."""
    for issue in (state.get('issues') or {}).values():
        if not (issue.get('blocked')
                and issue.get('blockedKind') == 'stuck_on_agent'):
            continue
        dep = issue.get('dependsOnTask')
        if dep == except_task_id:
            continue
        requester = _dependency_requester(state, issue.get('key'), dep)
        if requester == agent_id:
            return True
    return False


# --- requirements-met supervisor vote ---------------------------------------
# The one Jev-gated judgment in the blocked-field model. Agent work on a story is
# claimed requirements-met; the owning DIRECTOR (supervisor) must confirm the spec
# is genuinely satisfied before a block-change request is filed. Mirrors the
# escalation-groom / refinement decider shape: injectable decider, fail safe to
# NOT blocking (a leftover claim just doesn't get set) on any outage.


def _supervisor_block_vote(state, issue_key, decider=None):
    """Have the owning director judge a pending requirements-met claim on
    `issue_key`. The caller (a cadence sweep) only invokes this once the claim's
    gate window has elapsed. On approval files the block-change request and
    clears the claim; on rejection logs and clears without blocking. Returns
    True (approved), False (rejected / no claim / no director / outage)."""
    claim = (state.get('_pendingBlockClaim') or {})
    if claim.get('issueKey') != issue_key:
        return False
    state.pop('_pendingBlockClaim', None)  # consumed either way
    director_id = _issue_director(state, issue_key)
    issue = (state.get('issues') or {}).get(issue_key)
    if not director_id or not issue:
        return False
    agent_id = claim.get('agentId')
    question = claim.get('reason') or f"Is {issue_key} requirements-met?"
    decider = decider or _supervisor_vote_decider_default
    approved = bool(decider(state, issue_key, director_id, agent_id,
                            question, claim.get('context')))
    if approved:
        _file_block_change(state, issue_key, True, director_id,
                           'requirements_met', question)
    _log_governance(state, director_id, 'issue_block_vote',
                    {'issue': issue_key, 'approved': approved})
    return approved


def _supervisor_vote_decider_default(state, issue_key, director_id, agent_id,
                                     question, context):
    """Default supervisor resolver: Jev judges whether the agent's claim that the
    spec is met is credible. Returns a bool. Fail safe to False (don't block a
    card on an unverified claim) on any resolver outage."""
    import serve  # noqa
    try:
        data = serve._call_openrouter_decision_sync(
            serve._jev_model(), {'messages': [], 'signals': {}},
            {'choice': {'type': 'choice', 'instructions': (
                f"{director_id} is reviewing whether agent {agent_id}'s claim on "
                f"{issue_key} that the player's requirements are met is credible. "
                f"The agent asserted: '{question}'. Context: {context or ''}. "
                f"Approve ONLY if the acceptance criteria genuinely appear met."),
                'criteria': {
                    'approve': "The requirements appear genuinely met or very close.",
                    'reject': "The claim is unverified, partial, or out of scope."}}})
        choice = serve._jev_choice(data)[0] if serve._jev_choice(data) else None
        if choice == 'approve':
            return True
        if choice == 'reject':
            return False
    except Exception:
        pass
    return False


def request_block_claim_met(state, issue_key, agent_id, context=None, now_ms=None):
    """Seed the supervisor vote for a requirements-met claim by an agent. Returns
    True if the claim was recorded (deduped), False if one is already pending or
    the issue/director is unknown."""
    if _issue_director(state, issue_key) is None:
        return False
    issue = (state.get('issues') or {}).get(issue_key)
    if not issue:
        return False
    claim = (state.get('_pendingBlockClaim') or {})
    if claim.get('issueKey') == issue_key:
        return False  # a vote is already in flight
    # A card already blocked needs an unblock, not another met-claim.
    if issue.get('blocked'):
        _file_block_change(state, issue_key, False, agent_id,
                           'unblock_resolved',
                           'card already blocked; agent moving on')
        return False
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    state['_pendingBlockClaim'] = {
        'issueKey': issue_key, 'agentId': agent_id,
        'context': context or '', 'at': now_ms,
    }
    return True


def _supervisor_block_vote_sweep(state, now_ms, decider=None):
    """Cadence sweep (called from _check_schedules, like _pending_player_ask_sweep):
    spin a due requirements-met claim through the supervisor gate. Returns True on
    an approved commit, False on rejection, None if nothing was due."""
    claim = (state.get('_pendingBlockClaim') or {})
    issue_key = claim.get('issueKey')
    if not issue_key:
        return None
    # The claim's own gate: give the agent a short window to be questioned if
    # needed, then let the supervisor decide. (Distinct from the player-ask gate --
    # this reads _pendingBlockClaim, not _pendingPlayerAsk.)
    if (now_ms - (claim.get('at') or 0)) < BLOCK_CLAIM_GATE_MS:
        return None
    return _supervisor_block_vote(state, issue_key, decider=decider)


def _room_backlog_count(state, room):
    """How much of `room`'s work is currently queued / in-flight (the 'thin room'
    signal: too few items left means a finishing agent's follow-up is worth
    filing, because otherwise the room starves)."""
    queued = sum(1 for it in (state.get('workQueue') or []) if it.get('room') == room)
    in_flight = sum(1 for t in (state.get('tasks') or {}).values()
                    if t.get('room') == room and t.get('status') in ('walking', 'working'))
    return queued + in_flight


# Absolute Zero scoping guidance: when the think tank proposes its
# OWN next work (an agent filing a follow-up, and the grooming ceremony deciding
# whether to accept it), the default failure mode is proposing TRIVIAL work --
# cards easy to write and worth nothing. Guidance steers every self-proposed
# card toward MEDIUM difficulty: a real, well-scoped story that materially
# advances the room. Not a hard rule (the decider still owns accept/reject) --
# it just stops the easy/hard extremes from being the default.
ABSOLUTE_ZERO_SCOPING_GUIDANCE = (
    "Scoping guidance: prefer a MEDIUM-difficulty story -- a real, well-scoped "
    "piece of work that materially advances the room. Reject trivial/filler "
    "proposals and over-ambitious un-scoped ones alike."
)


def _free_spike_week(now_ms):
    """The weekly bucket a free-spike allowance is counted against -- an integer
    ISO-ish week number derived from the wall clock, so the allowance rolls by
    itself (no ceremony dependency, unlike weekApprovals which the Social
    resets)."""
    return int((now_ms or int(time.time() * 1000)) // (7 * 24 * 3600 * 1000))


def _free_spike_week_map(d):
    """Coerce JSON round-tripped week keys (strings) back to ints. state dicts
    keyed by week numbers -- freeSpikeGlobal / freeSpikeRooms -- lose their int
    keys on every save/load (JSON object keys are strings), so the age
    comparisons in _file_free_spike would otherwise mix str and int."""
    out = {}
    for k, v in (d or {}).items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            continue
    return out
