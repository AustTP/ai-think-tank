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

    def test_no_sprint_rotates_daily(self):
        # A question/incident NOT tied to a sprint has no natural advance point
        # (the sprint counter is static), so the slot shifts with the UTC day --
        # the pager actually moves even on a sprint-less team.
        state = _state()
        day_ms = 24 * 60 * 60 * 1000
        self.assertEqual(sim.on_call_agent(state, 'maya', None, now_ms=0),
                         sim.on_call_agent(state, 'maya', None, now_ms=0))  # deterministic within a day
        self.assertNotEqual(sim.on_call_agent(state, 'maya', None, now_ms=0),
                            sim.on_call_agent(state, 'maya', None, now_ms=day_ms))
        # Sprint-linked calls stay per-sprint even across days (restart-stable).
        self.assertEqual(sim.on_call_agent(state, 'maya', 'sprint-7', now_ms=0),
                         sim.on_call_agent(state, 'maya', 'sprint-7', now_ms=999 * day_ms))

    def test_busy_oncall_falls_through_to_backup_then_second_backup(self):
        state = _state()
        primary = sim.on_call_agent(state, 'maya', None, now_ms=1000)
        # The primary is mid-story: the pager must hand off to her backup --
        # the NEXT slot in rotation order, never double-booking her.
        state['agents'][primary]['busy'] = True
        backup = sim.on_call_agent(state, 'maya', None, now_ms=1000)
        self.assertNotEqual(backup, primary)
        self.assertFalse(state['agents'][backup]['busy'])
        # Backup busy too -> the second backup (the slot after the backup).
        state['agents'][backup]['busy'] = True
        second = sim.on_call_agent(state, 'maya', None, now_ms=1000)
        self.assertNotEqual(second, primary)
        self.assertNotEqual(second, backup)
        self.assertFalse(state['agents'][second]['busy'])
        # Whole team busy -> deadfall back to the primary slot (never empty).
        state['agents'][second]['busy'] = True
        self.assertEqual(sim.on_call_agent(state, 'maya', None, now_ms=1000), primary)


class OnCallRotationPersistence(unittest.TestCase):
    """WS-15: on-call rotation is now a PERSISTENT per-team order (state
    ['_oncallOrder']) instead of a pure re-derivation -- a backup who actually
    SERVED a sprint's pages is rotated to the END of the queue at sprint close
    (_rotate_served_oncalls) so she isn't paged again next sprint before the
    rotation catches up."""

    def test_order_is_persistent_and_roster_shaped(self):
        state = _state()
        order = sim._oncall_order(state, 'maya')
        self.assertEqual(order, ['ben', 'cora', 'zia'])  # roster order, SM+admin out
        # Second call is stable (no churn), still the same order.
        self.assertEqual(sim._oncall_order(state, 'maya'), order)

    def test_served_backup_tracked_only_for_sprint_anchored_pages(self):
        # A backup who actually SERVES a sprint-anchored page is recorded in
        # state['_oncallServed'] so the sprint-close ceremony can rotate her to
        # the END of the queue. Sprint-less pages (player questions) have no
        # sprint anchor, so they never mutate state.
        state = _state()
        sim.on_call_agent(state, 'maya', None, now_ms=1000)
        self.assertEqual(state.get('_oncallServed'), None)  # no sprint -> no record
        primary = sim.on_call_agent(state, 'maya', 'sprint-1', now_ms=1000)
        state['agents'][primary]['busy'] = True  # force the backup to serve
        sim.on_call_agent(state, 'maya', 'sprint-1', now_ms=1000)
        self.assertIsNotNone(state.get('_oncallServed'))  # sprint anchor -> tracked

    def test_served_backup_rotates_to_end_at_sprint_close(self):
        state = _state()
        # A sprint-anchored page while the primary is busy: the pager falls
        # through to the backup, who is recorded as having SERVED.
        primary = sim.on_call_agent(state, 'maya', 'sprint-1', now_ms=1000)
        state['agents'][primary]['busy'] = True
        served = sim.on_call_agent(state, 'maya', 'sprint-1', now_ms=1000)
        self.assertNotEqual(served, primary)
        self.assertEqual(state['_oncallServed']['maya'], [served])
        order = sim._oncall_order(state, 'maya')
        sim._rotate_served_oncalls(state, ['maya'])
        rotated = sim._oncall_order(state, 'maya')
        # The served backup moved to the END; everyone else's relative order kept.
        self.assertEqual(rotated, [a for a in order if a != served] + [served])
        # The served ledger is cleared so a later close doesn't re-rotate her.
        self.assertEqual(state['_oncallServed']['maya'], [])

    def test_primary_serving_does_not_rotate(self):
        state = _state()
        primary = sim.on_call_agent(state, 'maya', 'sprint-1', now_ms=1000)
        # Primary is available and chosen: NOT a backup, so NOT recorded as served.
        self.assertEqual(primary, sim.on_call_agent(state, 'maya', 'sprint-1', now_ms=1000))
        served = (state.get('_oncallServed') or {}).get('maya') or []
        self.assertEqual(served, [])
        before = sim._oncall_order(state, 'maya')
        sim._rotate_served_oncalls(state, ['maya'])
        self.assertEqual(sim._oncall_order(state, 'maya'), before)

    def test_rotate_only_touches_existing_agents(self):
        state = _state()
        state['teams'][0]['id'] = 'team-x'
        # A fired agent lingers in the recorded order; rotation drops them.
        sim._oncall_order(state, 'maya')
        state['_oncallOrder']['maya'] = ['ben', 'ghost', 'cora', 'zia']
        state.setdefault('_oncallServed', {})['maya'] = ['ghost']
        sim._rotate_served_oncalls(state, ['team-x'])
        order = sim._oncall_order(state, 'maya')
        self.assertNotIn('ghost', order)
        self.assertEqual(order, ['ben', 'cora', 'zia'])

    def test_new_hire_appends_to_end_as_backup(self):
        state = _state()
        sim._oncall_order(state, 'maya')
        state['agentRoster'].append(_director('yara', director='maya'))
        state['agents']['yara'] = {'id': 'yara', 'name': 'Yara', 'x': 0, 'y': 0,
                                   'busy': False, 'visible': True, 'offDuty': False,
                                   'inRoom': None}
        order = sim._oncall_order(state, 'maya')
        self.assertEqual(order, ['ben', 'cora', 'zia', 'yara'])

    def test_fired_agent_removed_from_order(self):
        state = _state()
        sim._oncall_order(state, 'maya')
        state['agentRoster'] = [d for d in state['agentRoster'] if d['id'] != 'zia']
        state['agents'].pop('zia', None)
        order = sim._oncall_order(state, 'maya')
        self.assertNotIn('zia', order)

    def test_sprint_close_wires_rotation_via_on_sprint_closed(self):
        state = _state()
        state['teams'][0]['id'] = 'team-x'
        # Set up an active sprint record + served ledger, then close it: the
        # rotation must run as part of the ceremony (before retro/pull/refinement).
        record = {'id': 'sprint-1', 'teamIds': ['team-x']}
        sim._oncall_order(state, 'maya')
        served = 'zia'
        state.setdefault('_oncallServed', {})['maya'] = [served]
        sim._on_sprint_closed(state, record, now_ms=10_000)
        order = sim._oncall_order(state, 'maya')
        self.assertEqual(order, ['ben', 'cora', 'zia'].remove(served) and None or
                                [a for a in ['ben', 'cora', 'zia'] if a != served] + [served])
        self.assertEqual(state['_oncallServed']['maya'], [])
        # Retro + refinement were kicked too (the ceremony still did its job).
        self.assertIn('sprint-1', state['pendingSprintRetros'])


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
        # Pinned to the owning team's on-call (same now_ms the bug used, so the
        # day-rotation base matches exactly).
        self.assertEqual(item['assignedTo'], sim.on_call_agent(state, 'maya', None, now_ms=1000))
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

    def _assign_first_bug(self, state, now_ms=10_000):
        grid, doors = sim._load_outdoor_geometry()
        holder = {0: 0}
        item = state['workQueue'][0]
        return sim._assign_due_item(state, item, True, grid, doors, now_ms,
                                    task_id_holder=holder)

    @staticmethod
    def _place_at_door(state, agent_id, room, doors):
        # The bare test grid has no walkable tile at the (0,0) spawn point, so
        # place the expected assignee EXACTLY on the door approach assign_task
        # targets (start == target -> trivial valid path). The live sim never
        # hits this: its agents already stand on walkable tiles.
        door = doors[room]
        a = state['agents'][agent_id]
        a['x'] = door['x'] + door['w'] / 2
        a['y'] = door['y'] + door['h'] + 4

    def test_busy_oncall_incident_repins_to_team_backup(self):
        # The on-call is mid-story by the time the incident lands: the pin must
        # re-derive the owning team's current on-call (her backup) instead of
        # leaking the incident to a global round-robin pick on another team.
        state = _state()
        _product(state)
        sim.queue_bug(state, 'prod-1', 'Auth broken', now_ms=1000)
        item = state['workQueue'][0]
        on_call = item['assignedTo']
        state['agents'][on_call]['busy'] = True  # starts work before assignment
        backup = sim.on_call_agent(state, 'maya', None, now_ms=1000)
        self.assertNotEqual(backup, on_call)
        grid, doors = sim._load_outdoor_geometry()
        self._place_at_door(state, backup, item['room'], doors)
        task = self._assign_first_bug(state)
        self.assertIsNotNone(task)
        self.assertEqual(task.get('assignedTo'), backup)  # the team's backup, same team
        self.assertEqual(task.get('taskType'), 'bug')

    def test_whole_team_busy_incident_lands_on_any_free_worker(self):
        # Last resort: the whole owning team is mid-story, so the re-derive has
        # nobody to hand off to -- the incident must still be picked up by SOME
        # eligible worker (generic round-robin) rather than dropped.
        state = _state()
        _product(state)
        state['agentRoster'] += [_director('nadia', is_admin=True),
                                 _director('omar', director='nadia')]
        state['agents']['omar'] = {'id': 'omar', 'name': 'Omar', 'x': 0, 'y': 0,
                                   'busy': False, 'visible': True, 'offDuty': False,
                                   'inRoom': None}
        state['teams'].append({'directorId': 'nadia', 'scrumMasterId': None})
        sim.queue_bug(state, 'prod-1', 'Auth broken', now_ms=1000)
        for aid in ('ben', 'cora', 'zia'):
            state['agents'][aid]['busy'] = True  # entire owning team mid-story
        grid, doors = sim._load_outdoor_geometry()
        room = state['workQueue'][0]['room']
        # Either free candidate may take it: the owning team's scrum master
        # (dax -- first eligible in roster order) or the foreign worker (omar).
        for free_id in ('dax', 'omar'):
            self._place_at_door(state, free_id, room, doors)
        task = self._assign_first_bug(state)
        self.assertIsNotNone(task)  # never dropped
        self.assertIn(task.get('assignedTo'), ('dax', 'omar'))  # some free worker took it


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
        oc = sim.on_call_agent(state, 'maya', None, now_ms=1000)
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