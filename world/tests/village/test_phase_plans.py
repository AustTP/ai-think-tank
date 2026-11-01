"""Phase plans in the breakdown ceremony (2026-11-01).

A prep-then-build ask ("define tasks, research, copy, UI research -- then
build") needs the prep phase to COMPLETE before build cards start, while
cards inside one phase run in parallel. The breakdown ceremony is where
the plan is authored: the scrum master's decider tags each card with a
phase, and the assignment guard enforces the barrier -- the same guard
that serializes a single-phase request via `sequence`, relaxed one level:
phaseIndex serializes BETWEEN phases, sequence serializes everything.

The invariants pinned here:
- a multi-phase plan stamps phase/phaseIndex and DROPS the serial sequence
  (same-phase cards may run concurrently);
- a later-phase card waits at assignment until every earlier-phase card is
  done (queued or active both block), and the created task carries the
  phase so active earlier-phase work is visible;
- a single-phase (or phase-less) plan keeps the legacy strict serial
  sequence -- the gather -> write -> save race protection is not relaxed
  by an untagged plan;
- an untagged card inside a phased plan joins the FIRST phase (an omitted
  tag must never open a hole in the barrier);
- the prompt teaches the decider the phase contract.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sim  # noqa: E402


def _state():
    return {
        'sim': {'owner': 'server'},
        'agents': {
            'w1': {'id': 'w1', 'name': 'W One', 'busy': False, 'offDuty': False,
                   'task': None, 'visible': True},
            'w2': {'id': 'w2', 'name': 'W Two', 'busy': False, 'offDuty': False,
                   'task': None, 'visible': True},
        },
        'agentRoster': [{'id': 'w1'}, {'id': 'w2'}],
        'teams': [{'id': 'ops', 'name': 'Ops', 'directorId': 'ops',
                   'members': ['w1', 'w2']}],
        'workQueue': [],
        'tasks': {},
        'backlogRequests': [],
        'pendingBreakdowns': {},
        'sprints': {},
    }


def _request(state, goal='Build the onboarding portal in phases'):
    req = {'id': 'wrq-1', 'status': 'pending', 'breakdown': True,
           'teamId': 'ops', 'filedBy': 'theo', 'goal': goal}
    state['backlogRequests'].append(req)
    return req


def _run_breakdown(state, items, instructions_log=None, now_ms=1000):
    """Drive _resolve_breakdown with an injectable decider returning the
    given items; returns the queued work items (post-whitelist)."""
    req = _request(state)
    pending = {'teamId': 'ops', 'scrumMasterId': 'w1', 'reqIds': [req['id']],
               'people': {}}

    def decider(_state, instructions, _goal):
        if instructions_log is not None:
            instructions_log.append(instructions)
        return {'items': items}

    sim._resolve_breakdown(state, pending, now_ms, decider=decider)
    return state['workQueue']


def _stub_assignment(monkeypatch, assignments):
    """Stub the assignment path so _assign_due_item's guard is the only code
    under test. assign_task receives the pick's field dict as `extra`
    (arg index 6) and mints the task id itself, so the stub replays both:
    it mints a real id from the holder and spreads extra -- the created
    task carries requestId/sequence/phase exactly as production would."""
    def fake_assign_task(*args, **kwargs):
        state, chosen_id = args[0], args[1]
        task_id_holder = args[10] if len(args) > 10 else kwargs.get('task_id_holder')
        task = {'id': sim._next_task_id(state, task_id_holder or sim._TASK_ID_HOLDER),
                'title': args[2], 'agentId': chosen_id, 'status': 'working'}
        task.update(args[6] or {})
        state['tasks'][task['id']] = task
        assignments.append(task)
        return task

    monkeypatch.setattr(sim, '_eligible_candidates', lambda s, include: ['w1', 'w2'])
    monkeypatch.setattr(sim, '_one_off_routing_applies', lambda p: False)
    monkeypatch.setattr(sim, '_feature_affinity', lambda s, a, p: 0)
    monkeypatch.setattr(sim, '_description_affinity', lambda s, a, p: 0)
    monkeypatch.setattr(sim, 'appear_from_outskirts', lambda *a: None)
    monkeypatch.setattr(sim.policies, 'apply_enabled', lambda: False)
    monkeypatch.setattr(sim, 'assign_task', fake_assign_task)
    return assignments


def _assign(state, pick, monkeypatch, assignments, now_ms=2_000):
    got = sim._assign_due_item(state, pick, True, None,
                               {'commandcenter': {}}, now_ms)
    # The dispatcher pops a successfully assigned item; a card left queued
    # keeps gating later phases as "queued earlier-phase work".
    if got is not None and got is not sim._ASSIGN_WAIT:
        state['workQueue'].remove(pick)
    return got


def _pick(state, needle):
    return next(i for i in state['workQueue'] if needle in i['title'])


def test_phased_plan_stamps_phases_and_drops_serial_sequence(monkeypatch):
    state = _state()
    items = [
        {'title': 'Define the task list', 'type': 'report', 'phase': 'prep'},
        {'title': 'Research the market', 'type': 'research', 'phase': 'prep'},
        {'title': 'Build the portal', 'type': 'story', 'phase': 'build'},
        {'title': 'Ship the walkthrough', 'type': 'story', 'phase': 'build'},
    ]
    queue = _run_breakdown(state, items)
    prep = [i for i in queue if i['phase'] == 'prep']
    build = [i for i in queue if i['phase'] == 'build']
    assert len(prep) == 2 and len(build) == 2
    assert all(i['phaseIndex'] == 0 for i in prep)
    assert all(i['phaseIndex'] == 1 for i in build)
    # The serial chain is replaced by the phase barrier: no card carries a
    # sequence, so same-phase cards may run in parallel.
    assert all(i['sequence'] is None for i in queue)


def test_phase_barrier_holds_build_until_prep_is_done(monkeypatch):
    state = _state()
    items = [
        {'title': 'Write the copy deck', 'type': 'report', 'phase': 'prep'},
        {'title': 'Build the portal', 'type': 'story', 'phase': 'build'},
    ]
    _run_breakdown(state, items)
    assignments = _stub_assignment(monkeypatch, [])

    prep_pick = _pick(state, 'copy deck')
    prep_pick['room'] = 'commandcenter'
    got = _assign(state, prep_pick, monkeypatch, assignments)
    assert got is not None and got is not sim._ASSIGN_WAIT
    prep_task_id = got['id']

    build_pick = _pick(state, 'Build the portal')
    build_pick['room'] = 'commandcenter'
    # Prep is active: the build card waits.
    assert _assign(state, build_pick, monkeypatch, assignments) is sim._ASSIGN_WAIT

    state['tasks'][prep_task_id]['status'] = 'done'
    # Prep landed: the build card starts, and its task carries the phase.
    got = _assign(state, build_pick, monkeypatch, assignments)
    assert got is not None and got is not sim._ASSIGN_WAIT
    assert got['phase'] == 'build'
    assert got['phaseIndex'] == 1


def test_same_phase_cards_run_in_parallel(monkeypatch):
    state = _state()
    # A multi-phase plan: the two prep cards may run concurrently (that is
    # the point of a prep sprint -- research, copy and UI research overlap),
    # while the build card waits for the whole prep phase.
    items = [
        {'title': 'Research the market', 'type': 'research', 'phase': 'prep'},
        {'title': 'Write the copy deck', 'type': 'report', 'phase': 'prep'},
        {'title': 'Build the portal', 'type': 'story', 'phase': 'build'},
    ]
    _run_breakdown(state, items)
    assignments = _stub_assignment(monkeypatch, [])

    first = _pick(state, 'Research the market')
    first['room'] = 'commandcenter'
    got = _assign(state, first, monkeypatch, assignments)
    assert got is not sim._ASSIGN_WAIT
    # The first prep card is ACTIVE: the second must NOT wait (same phase).
    second = _pick(state, 'copy deck')
    second['room'] = 'commandcenter'
    got = _assign(state, second, monkeypatch, assignments)
    assert got is not sim._ASSIGN_WAIT
    # But the build card still waits: prep is queued-or-active.
    build = _pick(state, 'Build the portal')
    build['room'] = 'commandcenter'
    assert _assign(state, build, monkeypatch, assignments) is sim._ASSIGN_WAIT


def test_three_phase_chain_is_transitive(monkeypatch):
    state = _state()
    items = [
        {'title': 'Define the tasks', 'type': 'report', 'phase': 'spec'},
        {'title': 'Research the market', 'type': 'research', 'phase': 'research'},
        {'title': 'Build the portal', 'type': 'story', 'phase': 'build'},
    ]
    _run_breakdown(state, items)
    assignments = _stub_assignment(monkeypatch, [])

    spec = _pick(state, 'Define the tasks')
    spec['room'] = 'commandcenter'
    got = _assign(state, spec, monkeypatch, assignments)
    assert got is not sim._ASSIGN_WAIT
    spec_id = got['id']

    build = _pick(state, 'Build the portal')
    build['room'] = 'commandcenter'
    # spec active -> build waits (research phase sits between them).
    assert _assign(state, build, monkeypatch, assignments) is sim._ASSIGN_WAIT
    state['tasks'][spec_id]['status'] = 'done'
    # spec done but the middle phase never ran -> still gated.
    assert _assign(state, build, monkeypatch, assignments) is sim._ASSIGN_WAIT

    research = _pick(state, 'Research the market')
    research['room'] = 'commandcenter'
    got = _assign(state, research, monkeypatch, assignments)
    assert got is not sim._ASSIGN_WAIT
    research_id = got['id']
    state['tasks'][research_id]['status'] = 'done'
    # Both earlier phases done -> build starts.
    assert _assign(state, build, monkeypatch, assignments) is not sim._ASSIGN_WAIT


def test_single_phase_plan_keeps_serial_sequence(monkeypatch):
    state = _state()
    items = [
        {'title': 'Gather the brief', 'type': 'report'},
        {'title': 'Write the page', 'type': 'story'},
    ]
    queue = _run_breakdown(state, items)
    # Legacy behavior is untouched: strict serial chain, no phase fields.
    assert [i['sequence'] for i in queue] == [0, 1]
    assert all(i['phaseIndex'] is None for i in queue)
    assignments = _stub_assignment(monkeypatch, [])

    first = _pick(state, 'Gather the brief')
    first['room'] = 'commandcenter'
    got = _assign(state, first, monkeypatch, assignments)
    assert got is not sim._ASSIGN_WAIT
    # Same-phase tag on both cards is ALSO a single-phase plan (serial).
    second = _pick(state, 'Write the page')
    second['room'] = 'commandcenter'
    assert _assign(state, second, monkeypatch, assignments) is sim._ASSIGN_WAIT


def test_single_phase_tagged_plan_is_still_serial(monkeypatch):
    state = _state()
    items = [
        {'title': 'Gather the brief', 'type': 'report', 'phase': 'only'},
        {'title': 'Write the page', 'type': 'story', 'phase': 'only'},
    ]
    queue = _run_breakdown(state, items)
    # One distinct phase == the legacy serial plan; the gather -> write race
    # protection stays armed.
    assert [i['sequence'] for i in queue] == [0, 1]
    assert all(i['phaseIndex'] is None for i in queue)


def test_untagged_card_in_phased_plan_joins_first_phase(monkeypatch):
    state = _state()
    items = [
        {'title': 'Define the tasks', 'type': 'report'},
        {'title': 'Research the market', 'type': 'research', 'phase': 'prep'},
        {'title': 'Build the portal', 'type': 'story', 'phase': 'build'},
    ]
    queue = _run_breakdown(state, items)
    untagged = _pick(state, 'Define the tasks')
    assert untagged['phase'] == 'prep'
    assert untagged['phaseIndex'] == 0
    build = _pick(state, 'Build the portal')
    assert build['phaseIndex'] == 1
    # And the barrier still holds: the (untagged, first-phase) card gates
    # the build card.
    assignments = _stub_assignment(monkeypatch, [])
    untagged['room'] = 'commandcenter'
    got = _assign(state, untagged, monkeypatch, assignments)
    assert got is not sim._ASSIGN_WAIT
    task_id = got['id']
    build['room'] = 'commandcenter'
    assert _assign(state, build, monkeypatch, assignments) is sim._ASSIGN_WAIT
    state['tasks'][task_id]['status'] = 'done'
    # Prep is not done yet: the tagged research card is still queued.
    research = _pick(state, 'Research the market')
    research['room'] = 'commandcenter'
    got = _assign(state, research, monkeypatch, assignments)
    assert got is not sim._ASSIGN_WAIT
    state['tasks'][got['id']]['status'] = 'done'
    # The whole prep phase landed -> the build card starts.
    assert _assign(state, build, monkeypatch, assignments) is not sim._ASSIGN_WAIT


def test_prompt_teaches_the_phase_contract():
    state = _state()
    seen = []
    _run_breakdown(state, [
        {'title': 'Define the tasks', 'type': 'report', 'phase': 'prep'},
        {'title': 'Build the portal', 'type': 'story', 'phase': 'build'},
    ], instructions_log=seen)
    text = seen[0]
    assert 'PHASES' in text
    assert 'EXECUTION order' in text
    assert 'run in parallel' in text
    assert '"phase"' in text
