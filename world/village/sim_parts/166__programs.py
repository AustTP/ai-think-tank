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
    by_title = {}
    for t in tasks.values():
        if isinstance(t, dict) and t.get('title'):
            by_title.setdefault(t['title'], t)
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
                and not program.get('followupMinted') \
                and all(s[0] == 'unminted' for s in states.values()) \
                and states:
            # The plan landed but nothing moved: mint the owner a
            # follow-through card once. Execution stalls exactly here
            # otherwise (prog-2's plan sat complete with zero sprints and
            # the owner idle).
            instructions = (
                "Your program plan has landed but no piece has moved: no "
                "teams/sprints created, no piece cards minted. Execute the "
                "plan now, by delegation:\n"
                "1. File the senior-director MEETING card your plan specified "
                "(ownership + sequencing ratification).\n"
                "2. Have each owning senior director author their piece's "
                "sprint cards and mint the piece card (refinement / director "
                "governance -- not self_edit).\n"
                "3. The pieces must carry the titles below verbatim so the "
                "program roll-up can track them.\n\n"
                f"Pieces:\n{piece_lines}\n\n"
                + (('Plan context: ' + (program.get('goal') or '') + '\n') if program.get('goal') else ''))
            minted += queue_work(state, [
                _program_card_item(program, 'EXECUTE', instructions, now_ms)])
            program['followupMinted'] = True
            _log_governance(state, None, 'program_followup_minted',
                            {'id': program.get('id'), 'owner': owner})
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
