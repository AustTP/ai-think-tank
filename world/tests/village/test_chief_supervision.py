"""The supervision-of-supervisors layer (2026-11-06, operator model).

The twelve directors stand EQUAL -- there is no senior-most director and
there never will be one. The regional VP stands alone with her own
instruments and never holds the cross-team cards. The CHIEF OF STAFF
holds them: a DAILY routine sweep carrying the per-team metrics rollup
(directorSupervision), plus attention cards when teams need help.
"""
import sim


def _mk():
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'nora', 'villageChief': True, 'isDirector': True},
            {'id': 'ben', 'isDirector': True, 'director': 'theo'},
            {'id': 'iris', 'isDirector': True, 'director': 'theo'},
            {'id': 'w1', 'director': 'ben'},
        ],
        'agents': {'theo': {}, 'nora': {}, 'ben': {}, 'iris': {}, 'w1': {}},
        'teams': [
            {'id': 'ben', 'name': "Ben's Team", 'directorId': 'ben',
             'members': ['w1']},
        ],
        'sprints': {},
        'tasks': {
            't1': {'teamId': 'ben', 'status': 'working', 'title': 'work',
                   'ownSpendUsd': 0.95, 'budgetUsd': 1.0},
            't2': {'teamId': 'ben', 'status': 'failed', 'title': 'bad'},
        },
        'workQueue': [], 'completedDeliverables': [],
        '_supervisorCardAt': {}, '_supervisorBriefAt': {},
    }


def test_rollup_counts_open_spend_overbudget_failures_and_completions():
    st = _mk()
    now = 10 ** 9
    day = 24 * 60 * 60 * 1000
    st['completedDeliverables'] = [
        {'agentId': 'w1', 'ts': now - 1000, 'grade': 5},
        {'agentId': 'w1', 'ts': now - day - 1000, 'grade': 5},  # >24h old
        {'agentId': 'ghost', 'ts': now - 1000, 'grade': 5},  # unknown agent
    ]
    roll = sim._director_supervision_rollup(st, now_ms=now)
    m = roll['ben']
    assert m['open'] == 2
    assert m['spendUsd'] == 0.95
    assert m['overBudget'] == 1  # 0.95 >= 0.9 * 1.0
    assert m['failedOrBlocked'] == 1
    assert m['completed24h'] == 1  # only the fresh w1 deliverable
    assert m['members'] == 1
    roll2 = sim._director_supervision_rollup(st, now_ms=now + 2000)
    assert roll2['ben']['ts'] == now  # hourly cadence gate holds


def test_cross_team_cards_go_to_the_chief_of_staff_never_the_vp():
    st = _mk()
    queued = []
    import unittest.mock as mock
    with mock.patch.object(sim, 'queue_work', lambda s, q: queued.extend(q)), \
            mock.patch.object(sim, '_supervisor_admin_card_open',
                              lambda s: False):
        sim._supervisor_brief_step(st, 0, 10 ** 9)
    cross = [q for q in queued
             if q['title'].startswith('[SUPERVISE] Cross-team')]
    assert cross, 'attention reasons exist, a cross-team card must fire'
    assert cross[0]['assignedTo'] == 'theo'
    assert all(q['assignedTo'] != 'nora' for q in queued)  # the VP stands alone


def test_routine_sweep_is_daily_and_carries_the_rollup():
    st = _mk()
    # A live-but-unremarkable board: no attention reasons, but work exists
    # (the drained-board invariant keeps the sweep off an EMPTY board).
    st['tasks'] = {'t9': {'teamId': 'ben', 'status': 'working',
                          'title': 'steady work', 'ownSpendUsd': 0.1}}
    queued = []
    import unittest.mock as mock
    with mock.patch.object(sim, 'queue_work', lambda s, q: queued.extend(q)), \
            mock.patch.object(sim, '_supervisor_admin_card_open',
                              lambda s: False):
        sim._supervisor_brief_step(st, 0, 10 ** 9)
        assert queued and queued[0]['assignedTo'] == 'theo'
        assert 'routine sweep' in queued[0]['title']
        assert 'Team metrics' in queued[0]['instructions']
        # within the day: no second routine card
        sim._supervisor_brief_step(st, 0, 10 ** 9 + 2 * 60 * 60 * 1000)
        assert len(queued) == 1
        # after 24h: the next routine card
        sim._supervisor_brief_step(st, 0, 10 ** 9 + 25 * 60 * 60 * 1000)
        assert len(queued) == 2
