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
    title = f"[{kind}] Program {program.get('id')}: {program.get('title')}" \
        if kind == 'PLAN' else \
        f"[CHANGE-REQUEST] Program {program.get('id')}: {program.get('title')}"
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
        if not program.get('planningCardMinted'):
            by_title = {p.get('title'): p for p in (program.get('pieces') or [])}
            piece_lines = '\n'.join(
                '- ' + t + ' (team: '
                + ((by_title.get(t) or {}).get('teamId') or 'unassigned') + ')'
                for t in states)
            instructions = (
                "You own this program. Plan it before any code moves:\n"
                "1. Confer with the senior directors (file a meeting card): "
                "review the pieces below, decide which senior director owns "
                "which piece, and create the necessary teams under them.\n"
                "2. Write the plan as a Library report with write_report "
                "(meeting notes, sprint breakdown, sequencing, per-piece "
                "acceptance). NEVER overwrite a report you already wrote: "
                "one file per artifact, and re-read it after writing to "
                "verify what landed. Plans do NOT need self_edit or any "
                "state surgery -- team and sprint creation happens through "
                "normal governance (refinement, issues, director-authored "
                "sprint cards).\n"
                "3. Break the work into sprints under the owning directors "
                "(sprint cards are director-authored and pool-staffed).\n"
                "4. Sequencing: pieces with dependencies must wait for them; "
                "overlapping pieces may be pulled from the shared backlog by "
                "teams already working the overlap.\n"
                "5. Every code piece needs peer approvals and must land as a "
                "staged, gated change; the change request(s) come only after "
                "everything is done and validated.\n\n"
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
