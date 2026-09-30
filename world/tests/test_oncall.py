"""Phase E2d: round-robin on-call per team.

When a product breaks for a downstream team, the owning team (product.teamId ->
a director) must own a fix. One agent per owning team is the current sprint's
on-call; a QUEUE_BUG routes a breakage to them as a pinned, high-priority,
NON-GATED incident. This suite validates the routing hermetically:
  - on_call_agent: deterministic per-sprint rotation; scrum master + admins
    excluded; falls back to an active member; never empty for a real team.
  - queue_bug: shapes a high-priority, pinned, non-gated bug; resolves the
    owning team's on-call; honors a one-in-flight cap per product; returns None
    when no owner/on-call resolves.
  - pin-wake: _assign_due_item chooses the pinned on-call for a bug even when
    she is off-duty and the generic wake rule would skip her.

Not actually hermetic against the real DB despite the plain-dict states used
throughout: _assign_due_item's assignment path has an inline `from serve
import log_action` call, a real side effect that writes into whatever real
think_tank.db sits at serve.py's default path unless DB_PATH is redirected
below. Found via a live production think_tank.db that picked up a
test fixture row after a routine test run.
"""

import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-oncall-test-')
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


def _director(rid, is_admin=False, **over):
    d = {'id': rid, 'name': rid.title(), 'role': 'director' if is_admin else 'engineer',
         'isAdmin': is_admin, 'director': rid}
    d.update(over)
    return d


def _state(**over):
    roster = [
        _director('maya', is_admin=True),
        _director('ben', director='maya'),
        _director('cora', director='maya'),
        _director('dax', director='maya'),
        _director('zia', director='maya', isAdmin=False),
    ]
    # Add a scrum master: cora is the standing facilitator (not a worker).
    teams = [{'directorId': 'maya', 'scrumMasterId': 'dax'}]
    agents = {d['id']: {'id': d['id'], 'name': d['name'], 'x': 0, 'y': 0, 'busy': False,
                        'visible': True, 'offDuty': False, 'inRoom': None} for d in roster}
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': roster,
        'agents': agents,
        'teams': teams,
        'products': {},
        'sprints': {},
        'workQueue': [],
        'tasks': {},
        'reports': [],
    }
    state.update(over)
    return state


def _product(state, pid='prod-1', team_id='maya'):
    sim.create_product(state, pid, 'Widget API', 's', 'spec', 'maya', 'sandbox-1',
                       team_id=team_id)
    return state['products'][pid]


class OnCallRotation(unittest.TestCase):
    def test_excludes_scrum_master_and_admin(self):
        # cora/dax/zia are all reports of maya; dax is the scrum master so must
        # never be on-call; maya the admin is never a candidate either.
        state = _state()
        for _ in range(20):
            oc = sim.on_call_agent(state, 'maya', f'sprint-{_}')
            self.assertIn(oc, {'ben', 'cora', 'zia'})
            self.assertNotEqual(oc, 'dax')
            self.assertNotEqual(oc, 'maya')

    def test_rotates_across_sprints(self):
        state = _state()
        seen = {sim.on_call_agent(state, 'maya', f'sprint-{i}') for i in range(6)}
        self.assertGreater(len(seen), 1)  # the rotation actually moves

    def test_deterministic_per_sprint(self):
        state = _state()
        self.assertEqual(sim.on_call_agent(state, 'maya', 'sprint-7'),
                         sim.on_call_agent(state, 'maya', 'sprint-7'))
        self.assertNotEqual(sim.on_call_agent(state, 'maya', 'sprint-1'),
                            sim.on_call_agent(state, 'maya', 'sprint-2'))

    def test_no_team_returns_none(self):
        state = _state()
        self.assertIsNone(sim.on_call_agent(state, 'nobody'))
        # A team of only an admin + scrum master has no rousable member.
        state['agentRoster'] = [_director('maya', is_admin=True), _director('dax', director='maya')]
        state['teams'] = [{'directorId': 'maya', 'scrumMasterId': 'dax'}]
        self.assertIsNone(sim.on_call_agent(state, 'maya'))


class QueueBug(unittest.TestCase):
    def test_routes_to_owning_team_oncall_as_high_priority_gated_out(self):
        state = _state()
        _product(state)
        n = sim.queue_bug(state, 'prod-1', 'Cannot authenticate users', now_ms=1000,
                          reported_by='ben')
        self.assertEqual(n, 1)
        item = state['workQueue'][0]
        self.assertEqual(item['taskType'], 'bug')
        self.assertEqual(item['priority'], sim.WORK_PRIORITY['high'])
        self.assertTrue(item['incident'])
        self.assertEqual(item['room'], 'pressoffice')
        # Pinned to the owning team's on-call.
        self.assertEqual(item['assignedTo'], sim.on_call_agent(state, 'maya', None))
        # Non-gated: a bug never opens a peer gate.
        self.assertFalse(sim._peer_gated_lane(item))

    def test_one_in_flight_cap_per_product(self):
        state = _state()
        _product(state)
        sim.queue_bug(state, 'prod-1', 'Auth broken', now_ms=1000)
        second = sim.queue_bug(state, 'prod-1', 'Auth still broken', now_ms=2000)
        self.assertIsNone(second)
        self.assertEqual(len(state['workQueue']), 1)

    def test_no_owner_returns_none(self):
        state = _state()
        _product(state, team_id=None)
        self.assertIsNone(sim.queue_bug(state, 'prod-1', 'Breakage'))
        self.assertEqual(state['workQueue'], [])
        # Unknown product id.
        self.assertIsNone(sim.queue_bug(state, 'prod-zzz', 'Breakage'))

    def test_incident_survives_queue_whitelist_and_assign(self):
        # The incident/product markers must survive queue_work AND reach the
        # created task so the cap can scan the open task set too.
        state = _state()
        _product(state)
        sim.queue_bug(state, 'prod-1', 'Auth broken', now_ms=1000)
        item = state['workQueue'][0]
        self.assertTrue(item.get('incident'))
        self.assertEqual(item.get('productId'), 'prod-1')


class PinWakeBug(unittest.TestCase):
    def _assign(self, state, can_wake=True, now_ms=10_000):
        grid, doors = sim._load_outdoor_geometry()
        holder = {0: 0}
        # Call the due-item assignment for the first bug queue item.
        item = state['workQueue'][0]
        return sim._assign_due_item(state, item, can_wake, grid, doors, now_ms,
                                    task_id_holder=holder)

    def test_bug_pin_selects_offduty_oncall_over_round_robin(self):
        state = _state()
        _product(state)
        oc = sim.on_call_agent(state, 'maya', None)
        sim.queue_bug(state, 'prod-1', 'Auth broken', now_ms=1000)
        # The on-call is off-duty (resting); the generic wake rule might skip
        # her, but the incident pin must wake her and select her.
        state['agents'][oc]['offDuty'] = True
        state['sim']['rr'] = {'task': 0}  # pointer at first candidate (ben)
        task = self._assign(state, can_wake=True)
        self.assertIsNotNone(task)
        # The assignment honored the pin -> on-call was chosen, woken, and given
        # the incident task.
        self.assertEqual(task.get('assignedTo'), oc)
        self.assertEqual(task.get('taskType'), 'bug')
        self.assertTrue(task.get('incident'))
        self.assertEqual(state['agents'][oc]['offDuty'], False)


if __name__ == '__main__':
    unittest.main()