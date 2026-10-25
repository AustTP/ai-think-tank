"""One-off / scheduled routing (ask 6): fire-at-a-time cards (queue_once
one-offs, `notBefore`) and standing cadence sweeps (research / skillReview
/ distill) go to an OFF-DUTY agent, so a focus team mid-sprint is never
disturbed by borrowed work. Two tiers, both SOFT preferences, never a
hard lock:

  Tier 1 ("finished"): an off-duty agent who has completed all her
  sprint work AND is on the item's team (any team when the item is
  teamless) -- the owning team's finisher takes the card.
  Tier 2 ("borrowed"): when no on-team finisher is off-duty, an
  off-duty agent on a team OTHER than the item's own borrows it.

Sprint cards never reach the rule: their hard pool restriction (sprint
staffing wins for the sprint's own cards) already narrowed the
candidates. When no off-duty candidate qualifies at all, the generic
preferences (team / fault-aware / affinity) pick exactly as before.

Isolated the same way tests/test_fault_aware_routing.py is (a real
think_tank.db side effect lives inside _assign_due_item via
serve.log_action) -- DB_PATH/THINK_TANK_DIR/etc redirected to a temp
dir for the whole module.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve
import sim

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-oneoff-routing-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _member(rid, director, is_admin=False, **over):
    d = {'id': rid, 'name': rid.title(), 'role': 'director' if is_admin else 'engineer',
         'isAdmin': is_admin, 'director': director}
    d.update(over)
    return d


def _roster():
    # A team is identified by its director id. Alpha (director 'ada'):
    # ben, cora. Beta (director 'ida'): dax, zia. maya is the admin.
    return [
        _member('maya', 'maya', is_admin=True),
        _member('ada', 'maya'),
        _member('ben', 'ada'),
        _member('cora', 'ada'),
        _member('ida', 'maya'),
        _member('dax', 'ida'),
        _member('zia', 'ida'),
    ]


def _state(**over):
    roster = _roster()
    # offDuty=True: x=0,y=0 is not a real walkable spot, so a CHOSEN
    # off-duty agent goes through appear_from_outskirts (real reachable
    # placement) before assign_task's find_path call, same as
    # test_fault_aware_routing._state does.
    agents = {d['id']: {'id': d['id'], 'name': d['name'], 'x': 0, 'y': 0,
                         'busy': False, 'visible': True, 'offDuty': True,
                         'inRoom': None} for d in roster}
    state = {
        'sim': {'owner': 'server', 'rr': {'task': 0}},
        'agentRoster': roster,
        'agents': agents,
        'teams': [],
        'products': {},
        'sprints': {},
        'workQueue': [],
        'tasks': {},
        'reports': [],
    }
    state.update(over)
    return state


def _on_duty(state, rid):
    # On-duty agents need real walkable coords with a route to
    # the room doors: (680,340) is the spot tests/test_charter_lanes.py
    # uses for its assigned agent ((100,100) has no path to the
    # pressoffice door, so assign_task's find_path fails there).
    a = state['agents'][rid]
    a['offDuty'] = False
    a['x'], a['y'] = 680, 340


def _busy(state, rid):
    state['agents'][rid]['busy'] = True


def _alpha_sprint(state, sid='s1'):
    # An ACTIVE sprint on team alpha (director 'ada') staffed by its
    # workers (ben, cora -- the _sprint_worker_pool headcount cap),
    # with one queued card still in the workQueue: the sprint has NOT
    # drained, so its staffed agents are NOT sprint-done.
    state['sprints'][sid] = {'status': 'active', 'teamIds': ['ada'],
                              'ownerId': 'ada', 'workerCount': 2}
    state['workQueue'].append({'title': 'Sprint card', 'sprintId': sid,
                                'room': 'pressoffice', 'taskType': 'code',
                                'priority': sim.WORK_PRIORITY['normal']})


def _one_off(**over):
    item = {'title': 'One-off card', 'room': 'pressoffice', 'taskType': 'code',
            'priority': sim.WORK_PRIORITY['normal'],
            'notBefore': int(time.time() * 1000) - 60_000}
    item.update(over)
    return item


def _assign(state, pick, now_ms=10_000):
    grid, doors = sim._load_outdoor_geometry()
    return sim._assign_due_item(state, pick, True, grid, doors, now_ms,
                                 task_id_holder=[0])


class OneOffRoutingApplies(unittest.TestCase):
    def test_fire_at_a_time_and_cadence_flags_apply(self):
        self.assertTrue(sim._one_off_routing_applies({'notBefore': 1}))
        self.assertTrue(sim._one_off_routing_applies({'research': True}))
        self.assertTrue(sim._one_off_routing_applies({'skillReview': True}))
        self.assertTrue(sim._one_off_routing_applies({'distill': True}))

    def test_ordinary_backlog_card_does_not_apply(self):
        self.assertFalse(sim._one_off_routing_applies({}))
        self.assertFalse(sim._one_off_routing_applies(
            {'title': 'Backlog', 'teamId': 'ada', 'room': 'pressoffice'}))


class AgentSprintWorkDone(unittest.TestCase):
    def _state(self, status='active', queued=True):
        return {
            'agentRoster': [
                _member('maya', 'maya', is_admin=True),
                _member('ada', 'maya'),
                _member('ben', 'ada'),
                _member('cora', 'ada'),
            ],
            'sprints': {'s1': {'status': status, 'teamIds': ['ada'],
                                'ownerId': 'ada', 'workerCount': 2}},
            'workQueue': ([{'title': 'Sprint card', 'sprintId': 's1'}]
                          if queued else []),
        }

    def test_vacuously_done_with_no_active_sprint(self):
        state = {'agentRoster': self._state()['agentRoster'],
                 'sprints': {}, 'workQueue': []}
        self.assertTrue(sim._agent_sprint_work_done(state, 'ben'))

    def test_not_done_while_active_sprint_has_queued_cards(self):
        state = self._state()
        self.assertFalse(sim._agent_sprint_work_done(state, 'ben'))
        self.assertFalse(sim._agent_sprint_work_done(state, 'cora'))

    def test_done_once_the_sprint_queue_drains(self):
        state = self._state(queued=False)
        self.assertTrue(sim._agent_sprint_work_done(state, 'ben'))

    def test_inactive_sprint_does_not_block(self):
        state = self._state(status='completed')
        self.assertTrue(sim._agent_sprint_work_done(state, 'ben'))

    def test_agent_outside_the_pool_is_done(self):
        state = self._state()
        self.assertTrue(sim._agent_sprint_work_done(state, 'ada'))


class OnItemTeam(unittest.TestCase):
    def _state(self):
        return {'agentRoster': [
            _member('maya', 'maya', is_admin=True),
            _member('ada', 'maya'),
            _member('ben', 'ada'),
            _member('dax', 'ida', loan={'teamId': 'ada'}),
        ]}

    def test_direct_report_is_on_team(self):
        self.assertTrue(sim._on_item_team(self._state(), 'ben', 'ada'))

    def test_loaned_agent_is_on_the_loan_team(self):
        self.assertTrue(sim._on_item_team(self._state(), 'dax', 'ada'))

    def test_unrelated_agent_is_not_on_team(self):
        self.assertFalse(sim._on_item_team(self._state(), 'ada', 'ida'))

    def test_no_team_means_not_on_team(self):
        self.assertFalse(sim._on_item_team(self._state(), 'ben', None))


class OneOffRouting(unittest.TestCase):
    def test_own_team_finisher_beats_other_team_off_duty(self):
        # Tier 1: ben (alpha) and dax (beta) are both off-duty and both
        # sprint-done (no active sprints exist) -- the alpha card goes
        # to the OWNING team's finisher, not the other team's.
        state = _state()
        for rid in ('ada', 'cora', 'ida', 'zia'):
            _on_duty(state, rid)
        task = _assign(state, _one_off(teamId='ada'))
        self.assertEqual(task['assignedTo'], 'ben')

    def test_borrows_other_team_when_own_team_is_mid_sprint(self):
        # Tier 2: ben (alpha) is off-duty but staffed on an active alpha
        # sprint that still has a queued card, so he is NOT sprint-done
        # -- the card borrows dax (beta, off-duty, sprint-done)
        # instead of disturbing ben's sprint.
        state = _state()
        for rid in ('ada', 'cora', 'ida', 'zia'):
            _on_duty(state, rid)
        _alpha_sprint(state)
        task = _assign(state, _one_off(teamId='ada'))
        self.assertEqual(task['assignedTo'], 'dax')

    def test_research_cadence_uses_the_same_rule(self):
        # A standing research sweep is a cadence card, not a fire-at-
        # a-time one-off, but the same two tiers apply.
        state = _state()
        for rid in ('ada', 'cora', 'ida', 'zia'):
            _on_duty(state, rid)
        _alpha_sprint(state)
        task = _assign(state, _one_off(teamId='ada', notBefore=None,
                                        research=True))
        self.assertEqual(task['assignedTo'], 'dax')

    def test_ordinary_backlog_card_ignores_the_rule(self):
        # No one-off/cadence flag: the rule never fires. ben is
        # off-duty mid-sprint and dax is off-duty sprint-done, yet
        # the generic SAME-TEAM preference still routes the ordinary
        # alpha card to ben (cora busy leaves him the only free alpha
        # member) -- exactly the pre-rule behavior.
        state = _state()
        for rid in ('ada', 'ida', 'zia'):
            _on_duty(state, rid)
        _on_duty(state, 'cora')
        _busy(state, 'cora')
        _alpha_sprint(state)
        item = {'title': 'Backlog card', 'room': 'pressoffice',
                'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal'],
                'teamId': 'ada'}
        task = _assign(state, item)
        self.assertEqual(task['assignedTo'], 'ben')

    def test_sprint_card_keeps_its_hard_pool(self):
        # A sprint card never reaches the one-off rule: its pool
        # restriction already narrowed candidates to the sprint's own
        # workers (ben, cora), so the off-duty sprint-done dax is
        # never eligible for it.
        state = _state()
        for rid in ('ada', 'ben', 'cora', 'ida', 'zia'):
            _on_duty(state, rid)
        # dax stays off-duty (and sprint-done: no beta sprint exists).
        state['sprints']['s1'] = {'status': 'active', 'teamIds': ['ada'],
                                   'ownerId': 'ada', 'workerCount': 2}
        item = {'title': 'Sprint card', 'sprintId': 's1',
                'room': 'pressoffice', 'taskType': 'code',
                'priority': sim.WORK_PRIORITY['normal'], 'teamId': 'ada'}
        state['workQueue'] = [item]
        task = _assign(state, item)
        self.assertEqual(task['assignedTo'], 'ben')

    def test_falls_back_to_generic_routing_when_nobody_is_off_duty(self):
        # Soft preference, never a hard lock: with every agent on duty
        # the rule has no off-duty candidate and the card still assigns
        # through the generic team preference.
        state = _state()
        for rid in ('ada', 'ben', 'cora', 'ida', 'dax', 'zia'):
            _on_duty(state, rid)
        task = _assign(state, _one_off(teamId='ada'))
        self.assertIsNotNone(task)
        self.assertEqual(task['assignedTo'], 'ben')

    def test_teamless_one_off_picks_any_sprint_done_finisher(self):
        # A teamless card has no owning team: any off-duty sprint-done
        # finisher qualifies. ben is off-duty but mid-sprint; dax takes
        # the card. (Without the rule the plain round-robin would pick
        # ada, the first eligible candidate.)
        state = _state()
        for rid in ('ada', 'cora', 'ida', 'zia'):
            _on_duty(state, rid)
        _alpha_sprint(state)
        task = _assign(state, _one_off())
        self.assertEqual(task['assignedTo'], 'dax')

    def test_loaned_agent_counts_as_on_the_borrowing_team(self):
        # zia (beta) is loaned to alpha: she counts as ON the item's
        # team, so tier 1 picks her over dax (beta, off-duty,
        # sprint-done, NOT loaned). Ignoring the loan, tier 2 would
        # hand the card to dax, who precedes zia in roster order.
        state = _state()
        for rid in ('ada', 'cora', 'ida'):
            _on_duty(state, rid)
        _alpha_sprint(state)
        for d in state['agentRoster']:
            if d['id'] == 'zia':
                d['loan'] = {'teamId': 'ada'}
        task = _assign(state, _one_off(teamId='ada'))
        self.assertEqual(task['assignedTo'], 'zia')


if __name__ == '__main__':
    unittest.main()
