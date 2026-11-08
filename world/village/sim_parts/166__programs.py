"""Programs engine (2026-11-07): the roll-up + owner cards.

Counterpart to serve_parts/430_programs.py, which stores state['programs'].
Each sim pass this engine:
  * derives per-piece status (unminted / queued / live status) from the
    workQueue + tasks by title match, and persists it onto the program;
  * mints the owner's PLANNING card once (confer with the senior directors,
    create teams under the right senior directors, break the pieces into
    sprints; overlapping pieces may be pulled from the shared backlog by
    teams already working the overlap; peer approvals apply);
  * when every piece has landed done/validated, stamps the program and
    mints the owner's CHANGE-REQUEST card (create the selfedit push
    window(s): one CR, or several given the scale).

Directors own sprint/team creation -- the engine never mints piece cards
itself, so the program cannot bypass the org's authority layers.
"""
import time

_PROGRAM_DONE_STATUSES = {'done'}
_PROGRAM_OPEN_STATUSES = {
    'queued', 'waiting', 'walking', 'working', 'assigned', 'needs_review',
    'blocked', 'failed', 'repairing',
}


def _program_piece_states(program, state):
    """Match each piece spec to its live/queued card by title. Returns
    (states, all_done) where states maps title -> (state, taskId)."""
    tasks = state.get('tasks') or {}
    queue = state.get('workQueue') or []
    _OPEN = ('working', 'walking', 'queued', 'repairing',
             'needs_review', 'investigating', 'recovery_planned')
    _TERMINAL = ('blocked', 'failed', 'cancelled', 'abandoned')
    by_title = {}
    for t in tasks.values():
        if not (isinstance(t, dict) and t.get('title')):
            continue
        prior = by_title.get(t['title'])
        if prior is None:
            by_title[t['title']] = t
            continue
        # An open card outranks anything; among the rest, newest wins --
        # the roll-up must not report a piece from its FIRST stale card
        # while a live retry runs under the same title.
        p_open = prior.get('status') in _OPEN
        t_open = t.get('status') in _OPEN
        if t_open and not p_open:
            by_title[t['title']] = t
        elif t_open == p_open:
            if int(t.get('createdAt') or 0) > int(prior.get('createdAt') or 0):
                by_title[t['title']] = t
    states = {}
    all_done = True
    for piece in (program.get('pieces') or []):
        title = piece.get('title')
        t = by_title.get(title)
        if t is None:
            if any(q.get('title') == title for q in queue):
                states[title] = ('queued', None)
            else:
                states[title] = ('unminted', None)
            all_done = False
            continue
        st = t.get('status')
        if st in _PROGRAM_DONE_STATUSES:
            states[title] = ('done', t.get('id'))
        elif st in _PROGRAM_OPEN_STATUSES:
            states[title] = (st, t.get('id'))
            all_done = False
        else:  # cancelled / abandoned: needs the owner's attention
            states[title] = (st or 'unknown', t.get('id'))
            all_done = False
    return states, all_done


def _program_card_item(program, kind, instructions, now_ms):
    if kind == 'PLAN':
        title = f"[{kind}] Program {program.get('id')}: {program.get('title')}"
    elif kind == 'EXECUTE':
        title = (f"[{kind}] Program {program.get('id')}: "
                 f"{program.get('title')}")
    else:
        title = f"[CHANGE-REQUEST] Program {program.get('id')}: {program.get('title')}"
    return {
        'title': title,
        'room': None,
        'instructions': instructions,
        'taskType': 'report',
        'deliverable': 'report',
        'assignedTo': program.get('ownerAgentId'),
        'directedTo': True,
        'priority': 'high',
        'feature': f"program:{program.get('id')}:{kind.lower()}",
    }


def _program_meeting_state(program, state):
    """The kickoff meeting card's lifecycle: 'working' (live or queued),
    'done' (completed -- pieces may mint), or 'dead' (cancelled/failed:
    nothing was ratified, the meeting must be convened again)."""
    title = f"[MEETING] {program.get('id')} kickoff: ownership and sequencing"
    tasks = state.get('tasks') or {}
    for t in tasks.values():
        if isinstance(t, dict) and t.get('title') == title:
            if t.get('status') == 'done':
                return 'done'
            if t.get('status') in ('failed', 'cancelled'):
                return 'dead'
            return 'working'
    return 'working'


def _programs_pass(state, now_ms=None):
    """Roll every program up and mint the owner cards at the two lifecycle
    edges (planning, change request). Returns the number of cards minted."""
    now_ms = int(now_ms or time.time() * 1000)
    programs = state.get('programs') or []
    if not isinstance(programs, list):
        return 0
    minted = 0
    for program in programs:
        if not isinstance(program, dict) or program.get('status') == 'closed':
            continue
        states, all_done = _program_piece_states(program, state)
        program['pieceStatus'] = {t: s[0] for t, s in states.items()}
        program['rolledUpAtMs'] = now_ms
        owner = program.get('ownerAgentId')
        if not owner:
            continue
        directors = {t.get('id'): t.get('directorId')
                     for t in (state.get('teams') or [])
                     if isinstance(t, dict)}
        by_title = {p.get('title'): p for p in (program.get('pieces') or [])}
        piece_lines = '\n'.join(
            '- ' + t + ' (team: '
            + ((by_title.get(t) or {}).get('teamId') or 'unassigned') + ')'
            for t in states)
        if not program.get('planningCardMinted'):
            instructions = (
                "You own this program -- and you are the CHIEF OF STAFF: your "
                "job is to convene and delegate, not to do the analysis "
                "yourself. The senior directors decide the how:\n"
                "1. File a MEETING card for the senior directors with the "
                "pieces below and the goal: each senior director reviews the "
                "pieces, claims the ones in their lane, and proposes how "
                "their piece should be incorporated/integrated (tests, "
                "approvals, sequencing) -- then you consolidate their "
                "responses into the plan.\n"
                "2. Write the consolidated plan as a Library report with "
                "write_report (meeting notes, sprint breakdown, sequencing, "
                "per-piece acceptance). NEVER overwrite a report you already "
                "wrote: one file per artifact, and re-read it after writing "
                "to verify what landed. Plans do NOT need self_edit or any "
                "state surgery -- team and sprint creation happens through "
                "normal governance (refinement, issues, director-authored "
                "sprint cards).\n"
                "3. Teams and sprints are created by the owning senior "
                "directors under you, not by you alone; where pieces "
                "overlap, teams pull from the shared backlog.\n"
                "4. Sequencing: pieces with dependencies must wait for them.\n"
                "5. Every code piece needs peer approvals and must land as a "
                "staged, gated change; the change request(s) come only after "
                "everything is done and validated, and a change request is "
                "only successful when CI is green.\n\n"
                f"Pieces:\n{piece_lines}\n\n"
                + (('Goal: ' + (program.get('goal') or '') + '\n') if program.get('goal') else '')
                + (('Context files: ' + ', '.join('library/' + f for f in program.get('contextFiles') or [])
                    + '\n') if program.get('contextFiles') else ''))
            minted += queue_work(state, [
                _program_card_item(program, 'PLAN', instructions, now_ms)])
            program['planningCardMinted'] = True
            program['status'] = 'planning-card-minted'
            _log_governance(state, None, 'program_planning_card_minted',
                            {'id': program.get('id'), 'owner': owner})
        elif program.get('status') == 'planning-card-minted' \
                and not program.get('meetingCardMinted'):
            # The plan landed. The owner CANNOT file the meeting card
            # herself (worker/director lanes refuse commit-tier api_call;
            # verified in prog-2's EXECUTE report) and the engine is the
            # only card-minting lane left -- so the engine convenes the
            # senior-director meeting on the owner's behalf, exactly as the
            # plan specifies. Governance stays real: the meeting ratifies
            # ownership; the engine only performs the mechanical mint.
            attendees = []
            for rid in governance.village_chief_ids(state):
                if rid not in attendees:
                    attendees.append(rid)
            for tid in governance.team_director_ids(state):
                if tid and tid not in attendees:
                    attendees.append(tid)
            if owner not in attendees:
                attendees.append(owner)
            instructions = (
                "Program kickoff meeting for " + program.get('id') + " ("
                + (program.get('title') or '') + "):\n"
                "1. Review the pieces below against the program goal and its "
                "context files.\n"
                "2. RATIFY per-piece ownership: each senior director claims "
                "the pieces in their lane; where no one claims a piece it "
                "returns to the shared backlog.\n"
                "3. Confirm the teams needed under each owning director.\n"
                "4. Write the minutes into this card's note: the ownership "
                "table and the sequencing decisions. The engine mints the "
                "piece cards to the owning directors once this meeting "
                "completes.\n\n"
                f"Pieces:\n{piece_lines}\n\n"
                + (('Goal: ' + (program.get('goal') or '') + '\n') if program.get('goal') else '')
                + (('Context files: ' + ', '.join('library/' + f for f in program.get('contextFiles') or [])
                    + '\n') if program.get('contextFiles') else ''))
            minted += queue_meeting(
                state,
                title=f"[MEETING] {program.get('id')} kickoff: ownership "
                      f"and sequencing",
                room='pressoffice',
                instructions=instructions,
                goal=program.get('goal'),
                attendees=attendees,
                facilitator_id=owner,
                assigned_to=owner)
            program['meetingCardMinted'] = True
            _log_governance(state, None, 'program_meeting_card_minted',
                            {'id': program.get('id'), 'owner': owner,
                             'attendees': len(attendees)})
        elif program.get('status') == 'planning-card-minted' \
                and program.get('meetingCardMinted') \
                and not program.get('piecesMinted'):
            meeting = _program_meeting_state(program, state)
            if meeting == 'dead':
                # The kickoff died (cancel/failed) without ratifying
                # anything: convene it again rather than minting pieces
                # into an unratified org (prog-2's first meeting was
                # walk-cancelled and the pieces minted on that cancel).
                program['meetingCardMinted'] = False
                continue
            if meeting != 'done':
                continue
            # The kickoff meeting completed: mint each piece card, directed
            # to the owning team's director (the meeting's ratified owner;
            # unclaimed pieces go to the shared backlog unassigned).
            items = []
            for piece in (program.get('pieces') or []):
                title = piece.get('title')
                if not title:
                    continue
                assignee = directors.get(piece.get('teamId'))
                items.append({
                    'title': title,
                    'room': None,
                    'instructions': (
                        "Program piece for " + program.get('id') + ". Your "
                        "team owns this piece (ratified at the kickoff "
                        "meeting). Author your sprint for it, then do the "
                        "work; peer approvals and a staged, gated change "
                        "apply to code pieces."),
                    'goal': program.get('goal'),
                    'projectLabel': f"program:{program.get('id')}",
                    'priority': WORK_PRIORITY['high'],
                    'taskType': piece.get('taskType') or 'code',
                    'deliverable': piece.get('deliverable') or 'code',
                    'assignedTo': assignee,
                    'directedTo': bool(assignee),
                })
            if items:
                minted += queue_work(state, items)
                program['piecesMinted'] = True
                _log_governance(state, None, 'program_pieces_minted',
                                {'id': program.get('id'),
                                 'pieces': len(items)})
        elif program.get('piecesMinted') and program.get('meetingCardMinted') \
                and not all_done:
            # A piece whose card died (blocked/failed/cancelled) gets ONE
            # re-mint per cooldown window. The pattern-suppression gate
            # (070__fire_decision) still stops genuine contract-failure
            # loops; this covers infrastructure deaths (restart churn,
            # provider timeouts) that left the program stranded.
            reissues = program.setdefault('pieceReissues', {})
            cooldown_ms = 30 * 60 * 1000
            items = []
            for piece in (program.get('pieces') or []):
                title = piece.get('title')
                if not title:
                    continue
                st_, _tid = states.get(title, ('unminted', None))
                if st_ not in ('blocked', 'failed', 'cancelled', 'abandoned'):
                    continue
                last = int(reissues.get(title) or 0)
                if last and now_ms - last < cooldown_ms:
                    continue
                reissues[title] = now_ms
                items.append({
                    'title': title,
                    'room': None,
                    'instructions': (
                        "Re-issue of a program piece for "
                        + program.get('id') + "; the previous card died "
                        "without completing (blocked/failed). Pick the "
                        "work up: author your sprint, do the piece, peer "
                        "approvals and a staged, gated change apply to "
                        "code pieces."),
                    'goal': program.get('goal'),
                    'projectLabel': f"program:{program.get('id')}",
                    'priority': WORK_PRIORITY['high'],
                    'taskType': piece.get('taskType') or 'code',
                    'deliverable': piece.get('deliverable') or 'code',
                    'assignedTo': directors.get(piece.get('teamId')),
                    'directedTo': True,
                })
            if items:
                minted += queue_work(state, items)
                _log_governance(state, None, 'program_pieces_reissued',
                                {'id': program.get('id'),
                                 'pieces': [i['title'][:40] for i in items]})
        elif program.get('status') != 'ready-for-cr' and all_done \
                and (program.get('pieces')):
            instructions = (
                "Every program piece has landed done/validated. Create the "
                "change request(s) now: stage the validated work and run the "
                "selfedit push window(s) with an independent validator -- one "
                "CR, or several given the scale. CI must be green after the "
                "push. When the push lands, close this program.")
            minted += queue_work(state, [
                _program_card_item(program, 'CHANGE-REQUEST', instructions, now_ms)])
            program['status'] = 'ready-for-cr'
            program['crRequested'] = True
            _log_governance(state, None, 'program_ready_for_cr',
                            {'id': program.get('id'), 'owner': owner})
    return minted
