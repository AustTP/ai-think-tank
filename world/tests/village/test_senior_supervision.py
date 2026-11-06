"""The supervision-of-supervisors layer (2026-11-06, operator model).

The director-of-a-director chain is RECURSIVE and its top is the
SENIOR-MOST DIRECTOR. The regional VP stands alone with her own
instruments and never holds these cards. The senior-most director's
routine sweep is DAILY. She gets the per-team metrics rollup
(directorSupervision). Until a senior-most director exists, the chief of
staff holds the cross-team card as fallback.
"""
import sim


def _mk():
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'nora', 'villageChief': True, 'isDirector': True},
            {'id': 'ben', 'isDirector': True, 'director': None},
            {'id': 'iris', 'isDirector': True, 'director': 'ben'},
            {'id': 'w1', 'director': 'ben'},
        ],
        'agents': {'theo': {}, 'nora': {}, 'ben': {}, 'iris': {}, 'w1': {}},
        'teams': [
            {'id': 'ben', 'name': "Ben's Team", 'directorId': 'ben',
             'members': ['iris', 'w1']},
            {'id': 'iris', 'name': "Iris's Team", 'directorId': 'iris',
             'members': []},
        ],
        'sprints': {},
        'tasks': {
            't1': {'teamId': 'ben', 'status': 'working', 'title': 'work',
                   'ownSpendUsd': 0.95, 'budgetUsd': 1.0},
            't2': {'teamId': 'ben', 'status': 'failed', 'title': 'bad'},
            't3': {'teamId': 'ben', 'status': 'done', 'title': 'old'},
        },
        'workQueue': [], 'completedDeliverables': [],
        '_supervisorCardAt': {}, '_supervisorBriefAt': {},
    }


def test_senior_most_director_is_the_root_of_the_director_chain():
    st = _mk()
    # ben supervises the director iris -> ben is the senior-most director
    assert sim._senior_most_director(st) == 'ben'


def test_no_director_reports_means_no_senior_most_yet():
    st = _mk()
    st['agentRoster'] = [r for r in st['agentRoster'] if r['id'] != 'iris']
    st['agentRoster'].append({'id': 'w1', 'director': 'ben'})
    assert sim._senior_most_director(st) is None


def test_the_regional_vp_is_never_the_supervision_top():
    st = _mk()
    # even with nora holding villageChief AND a directorship, the
    # supervision top follows the director-of-director pointers
    assert sim._senior_most_director(st) == 'ben'
    st['agentRoster'] = [r for r in st['agentRoster'] if r['id'] != 'ben']
    st['agentRoster'].append({'id': 'iris', 'isDirector': True,
                              'director': 'nora'})
    # nora supervises a director, but the VP stands alone: she is excluded
    st['agentRoster'] = [dict(r, villageChief=(r['id'] == 'nora'))
                         for r in st['agentRoster']]
    nora_row = next(r for r in st['agentRoster'] if r['id'] == 'nora')
    nora_row['isDirector'] = False  # the VP is not a team director
    assert sim._senior_most_director(st) is None


def test_rollup_counts_open_spend_overbudget_failures_and_completions():
    st = _mk()
    now = 10 ** 9
    day = 24 * 60 * 60 * 1000
    st['completedDeliverables'] = [
        {'agentId': 'w1', 'ts': now - 1000, 'grade': 5},   # fresh
        {'agentId': 'w1', 'ts': now - day - 1000, 'grade': 5},  # >24h old
        {'agentId': 'ghost', 'ts': now - 1000, 'grade': 5},  # unknown agent
    ]
    roll = sim._director_supervision_rollup(st, now_ms=now)
    m = roll['ben']
    assert m['open'] == 2  # done is excluded
    assert m['spendUsd'] == 0.95
    assert m['overBudget'] == 1  # 0.95 >= 0.9 * 1.0
    assert m['failedOrBlocked'] == 1
    assert m['completed24h'] == 1  # only the fresh w1 deliverable
    assert m['members'] == 2  # w1 + subdirector iris
    roll2 = sim._director_supervision_rollup(st, now_ms=now + 2000)
    assert roll2['ben']['ts'] == now  # hourly cadence gate holds


def test_cross_team_cards_route_to_the_senior_most_director():
    st = _mk()
    queued = []
    import unittest.mock as mock
    with mock.patch.object(sim, 'queue_work', lambda s, q: queued.extend(q)), \
            mock.patch.object(sim, '_supervisor_admin_card_open',
                              lambda s: False):
        sim._supervisor_brief_step(st, 0, 10 ** 9)
    assert queued, 'attention reasons exist, a card must fire'
    assert queued[0]['assignedTo'] == 'ben'
    assert 'SUPERVISE' in queued[0]['title']
    assert queued[0]['assignedTo'] != 'nora'  # the VP stands alone


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
        assert queued and queued[0]['assignedTo'] == 'ben'
        assert 'routine sweep' in queued[0]['title']
        assert 'Team metrics' in queued[0]['instructions']
        # within the day: no second routine card
        sim._supervisor_brief_step(st, 0, 10 ** 9 + 2 * 60 * 60 * 1000)
        assert len(queued) == 1
        # after 24h: the next routine card
        sim._supervisor_brief_step(st, 0, 10 ** 9 + 25 * 60 * 60 * 1000)
        assert len(queued) == 2


def test_no_senior_most_falls_back_to_the_chief_of_staff():
    st = _mk()
    st['agentRoster'] = [{'id': 'theo', 'isAdmin': True},
                         {'id': 'ben', 'isDirector': True}]
    queued = []
    import unittest.mock as mock
    with mock.patch.object(sim, 'queue_work', lambda s, q: queued.extend(q)), \
            mock.patch.object(sim, '_supervisor_admin_card_open',
                              lambda s: False):
        sim._supervisor_brief_step(st, 0, 10 ** 9)
    cross = [q for q in queued if q['title'].startswith('[SUPERVISE] Cross-team')]
    assert cross and cross[0]['assignedTo'] == 'theo'
