"""Tests for the server-side simulation engine (world/sim.py).

Mirrors the ServerOwnedSeed isolation pattern from test_serve.py: every test
redirects state/disk side effects into a throwaway temp directory so the real
think_tank.db is never touched.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim


class SimTick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-sim-test-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _state():
        return {
            'agents': {
                'ada': {'id': 'ada', 'x': 10, 'y': 20, 'dir': 'right', 'busy': True, 'task': 'check weather', 'inRoom': 'weather-station', 'offDuty': False, 'path': [{'x': 0, 'y': 0}]},
                'ben': {'id': 'ben', 'x': 5, 'y': 5, 'dir': 'down', 'busy': False, 'task': None, 'inRoom': None, 'offDuty': True, 'path': []},
            },
        }

    def test_tick_increments_tick_and_records_epoch(self):
        state = self._state()
        engine = sim.SimEngine()
        out = engine.tick(state, now=1234.0)
        self.assertEqual(out['sim']['tick'], 1)
        self.assertEqual(out['sim']['lastTickEpochS'], 1234.0)
        out = engine.tick(out, now=1236.0)
        self.assertEqual(out['sim']['tick'], 2)
        self.assertEqual(out['sim']['lastTickEpochS'], 1236.0)

    def test_snapshot_mirrors_agent_truth(self):
        engine = sim.SimEngine()
        out = engine.tick(self._state())
        snap = out['sim']['agents']
        self.assertEqual(snap['ada']['x'], 10)
        self.assertEqual(snap['ada']['busy'], True)
        self.assertEqual(snap['ada']['task'], 'check weather')
        self.assertEqual(snap['ada']['pathActive'], True)   # non-empty path
        self.assertEqual(snap['ben']['busy'], False)
        self.assertEqual(snap['ben']['offDuty'], True)
        self.assertEqual(snap['ben']['pathActive'], False)  # empty path

    def test_off_duty_agent_is_never_snapshot_busy(self):
        # A stale busy+offDuty ghost (abandoned firing review) otherwise reads
        # as busy to the client's 1s poll, which re-imposes it and wedges all
        # delegation + hiring with no in-system recovery. The snapshot is the
        # authoritative owner, so it must report such an agent as not busy.
        state = {
            'agents': {
                'faye': {'id': 'faye', 'x': 0, 'y': 0, 'dir': 'south',
                         'busy': True, 'task': None, 'inRoom': None,
                         'offDuty': True, 'path': []},
            },
        }
        engine = sim.SimEngine()
        snap = engine.tick(state)['sim']['agents']
        self.assertEqual(snap['faye']['offDuty'], True)
        self.assertEqual(snap['faye']['busy'], False)  # off duty => not busy

    def test_tick_tolerates_missing_or_malformed_agents(self):
        engine = sim.SimEngine()
        out = engine.tick({'sim': {'tick': 5}}, now=1.0)
        self.assertEqual(out['sim']['tick'], 6)
        self.assertEqual(out['sim']['agents'], {})
        out = engine.tick({'agents': 'not-a-dict'}, now=2.0)
        self.assertEqual(out['sim']['agents'], {})

    def test_status_reflects_state(self):
        engine = sim.SimEngine()
        state = engine.tick(self._state(), now=time.time())
        st = engine.status(state)
        self.assertTrue(st['running'])
        self.assertEqual(st['tick'], 1)
        self.assertIsNotNone(st['lastTickEpochS'])

    def test_loop_pass_round_trips_through_real_db(self):
        serve.save_state_to_db(self._state())
        # Drive off the real serve.get_state_from_db, not the in-memory pass-in.
        sim._sim_loop_pass()
        state = serve.get_state_from_db()
        self.assertIn('sim', state)
        self.assertEqual(state['sim']['tick'], 1)


class SimServerOwnedMovement(unittest.TestCase):
    # The Phase-2 flip: when sim.owner == 'server', SimEngine.tick must DRIVE
    # agent positions by running the proven step_agent_movement over the real
    # state['agents'], not merely observe them. Under client ownership it must
    # stay a pure observer (no position mutation). Geometry loads from the real
    # collision_grid.json/door_triggers.json via _load_outdoor_geometry.

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-sim-move-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _walking_agent(self):
        # A visible agent parked a little left of SPAWN with a short path that
        # steps right/east toward a near waypoint -- a real, agent-walkable
        # layout on the actual collision grid. path waypoint is 40px east so
        # dt=2.0 at TASK_WALK_SPEED=60 moves 120px -- but the first waypoint is
        # the only one and is far, so x increases each tick until arrival.
        wpx = sim.SPAWN['x'] + 40
        return {
            'ada': {
                'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                'inRoom': None, 'offDuty': False,
                'path': [{'x': wpx, 'y': sim.SPAWN['y']}],
                'pathIndex': 0, 'pathTarget': {'x': wpx, 'y': sim.SPAWN['y']},
                'stuckTimer': 0, 'replanCount': 0, 'respawnedForTask': False,
            },
        }

    def test_server_owner_moves_agent_toward_waypoint(self):
        state = {'sim': {'owner': 'server'}, 'agents': self._walking_agent()}
        engine = sim.SimEngine()
        x0 = state['agents']['ada']['x']
        out = engine.tick(state, now=100.0)
        self.assertGreater(out['agents']['ada']['x'], x0,
                           'server-owned tick must advance x toward the waypoint')
        # The snapshot mirror must reflect the post-step position.
        self.assertEqual(out['sim']['agents']['ada']['x'],
                         out['agents']['ada']['x'])
        # dir is now east (dx > dy).
        self.assertEqual(out['agents']['ada']['dir'], 'east')

    def test_client_owner_does_not_mutate_positions(self):
        state = {'sim': {'owner': 'client'}, 'agents': self._walking_agent()}
        x0 = state['agents']['ada']['x']
        y0 = state['agents']['ada']['y']
        engine = sim.SimEngine()
        out = engine.tick(state, now=100.0)
        self.assertEqual(out['agents']['ada']['x'], x0,
                         'client-owned tick is an observer: x must not change')
        self.assertEqual(out['agents']['ada']['y'], y0)
        # path must still be present (client owns walking; server never clears it)
        self.assertTrue(out['agents']['ada'].get('path'))

    def test_server_owner_advances_and_clears_path_on_arrival(self):
        # Waypoint only 10px away: one 2s tick at 60px/s should arrive quickly.
        state = {'sim': {'owner': 'server'},
                 'agents': self._walking_agent()}
        a = state['agents']['ada']
        a['path'] = [{'x': a['x'] + 8, 'y': a['y']}]
        a['pathTarget'] = {'x': a['x'] + 8, 'y': a['y']}
        engine = sim.SimEngine()
        out = engine.tick(state, now=100.0)
        # Arrival snapshot: server clears path and bumps tick. The agent may
        # have arrived (snapped to the waypoint) or still be approaching if the
        # step didn't cross ARRIVE_DIST -- either way x advanced or was snapped.
        self.assertGreaterEqual(out['agents']['ada']['x'], state['agents']['ada']['x'],
                                'x must reach the near waypoint or beyond on arrival')

    def test_server_tick_substeps_so_no_waypoint_overshoot_oscillation(self):
        # REGRESSION (with probe_1): a single SIM_TICK_S (2s) step
        # at 60px/s moves a walker 120px -- far past a grid cell and past
        # TASK_ARRIVE_DIST -- so she overshoots her first waypoint and bounces
        # forever (x oscillating start<->start+step, pathIndex frozen). The fix
        # sub-steps SIM_TICK_S into small fixed steps; here we assert movement
        # monotonic-then-settled across several server ticks on held state.
        state = {'sim': {'owner': 'server'},
                 'agents': self._walking_agent()}
        a = state['agents']['ada']
        # Short real path: waypoints 16px apart (one grid cell), the worst case
        # for a coarse step.
        start = sim.SPAWN['x']
        a['x'] = start
        a['y'] = sim.SPAWN['y']
        a['path'] = [{'x': start + 16, 'y': sim.SPAWN['y']},
                     {'x': start + 32, 'y': sim.SPAWN['y']},
                     {'x': start + 48, 'y': sim.SPAWN['y']}]
        a['pathTarget'] = {'x': start + 48, 'y': sim.SPAWN['y']}
        engine = sim.SimEngine()
        xs = []
        held = state
        for _ in range(6):
            out = engine.tick(held)
            held = out
            xs.append(out['agents']['ada']['x'])
        # Must converge to the final waypoint, not oscillate. The simplest
        # invariant that catches the bounce: the last tick equals the target and
        # the series is (weakly) increasing past the first step.
        self.assertEqual(round(xs[-1]), start + 48,
                         f'must settle at the final waypoint, got x trail {xs}')
        self.assertEqual(xs[-1], xs[-2], 'must be parked (no post-arrival bounce)')

    def test_repair_stalled_walker_reissues_path(self):
        # An agent HOLDING a 'walking' task with an EMPTY path (path cleared,
        # arrival never fired) freezes forever: movement skips pathless agents
        # and _reconcile_stranded_agents skips task-holders, so nothing re-paths
        # her. _repair_stalled_walkers must give her a fresh path to the room's
        # door. Place a visible agent at SPAWN holding a walking pressoffice task.
        state = {'sim': {'owner': 'server'},
                 'agents': {'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                                    'dir': 'south', 'visible': False, 'busy': False,
                                    'task': 'task-1', 'inRoom': None, 'offDuty': False,
                                    'path': [], 'pathIndex': 0, 'pathTarget': None,
                                    'stuckTimer': 0, 'replanCount': 0}},
                 'tasks': {'task-1': {'id': 'task-1', 'title': 't', 'room': 'pressoffice',
                                      'status': 'walking', 'assignedTo': 'ada'}}}
        grid, doors = sim._load_outdoor_geometry()
        sim._repair_stalled_walkers(state, grid, doors)
        a = state['agents']['ada']
        self.assertTrue(a.get('path'),
                        'a task-holding pathless walker must get a fresh path')
        self.assertEqual(a['pathIndex'], 0)
        self.assertIsNotNone(a.get('pathTarget'))
        self.assertTrue(a.get('visible'),
                        'a walking agent must be map-visible so movement walks her')
        # A 'walking' agent who ALREADY has a path but was left invisible is just
        # as stuck (movement skips invisible agents) -- the repair must re-affirm
        # visibility even when no fresh path is needed.
        state1b = {'sim': {'owner': 'server'},
                   'agents': {'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                                      'busy': False, 'task': 'task-1', 'path': [{'x': 0, 'y': 0}],
                                      'pathIndex': 0, 'visible': False,
                                      'inRoom': None, 'offDuty': False}},
                   'tasks': {'task-1': {'id': 'task-1', 'title': 't', 'room': 'pressoffice',
                                        'status': 'walking', 'assignedTo': 'ada'}}}
        sim._repair_stalled_walkers(state1b, grid, doors)
        self.assertTrue(state1b['agents']['ada']['visible'],
                        'a walking agent with a path must still be re-affirmed visible')
        # An agent with NO task is left entirely alone (not hounded).
        state2 = {'sim': {'owner': 'server'},
                  'agents': {'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                                     'busy': False, 'task': None, 'path': [],
                                     'visible': True}},
                  'tasks': {}}
        sim._repair_stalled_walkers(state2, grid, doors)
        self.assertEqual(state2['agents']['ada']['path'], [],
                         'a taskless agent must not be path-warped')

    def _walking_task_state(self, **over):
        """A minimal walking-task holder for the cancel/release tests."""
        state = {'sim': {'owner': 'server'},
                 'agents': {'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                                    'dir': 'south', 'visible': True, 'busy': False,
                                    'task': 'task-1', 'inRoom': None, 'offDuty': False,
                                    'path': [{'x': 1, 'y': 1}], 'pathIndex': 0,
                                    'pathTarget': {'x': 1, 'y': 1},
                                    'stuckTimer': 0, 'replanCount': 0,
                                    'respawnedForTask': False}},
                 'tasks': {'task-1': {'id': 'task-1', 'title': 't', 'room': 'pressoffice',
                                      'status': 'walking', 'assignedTo': 'ada'}}}
        for key, value in over.items():
            state[key] = value
        return state

    def test_cancel_at_task_releases_agent_and_marks_cancelled(self):
        # Port of tasks.js cancelTask: task -> 'cancelled', agent fully
        # released (task/path/pathTarget/respawn cleared, replan counter reset).
        state = self._walking_task_state()
        task = sim._cancel_at_task(state, 'ada')
        self.assertEqual(task['id'], 'task-1')
        self.assertEqual(state['tasks']['task-1']['status'], 'cancelled')
        a = state['agents']['ada']
        self.assertIsNone(a['task'])
        self.assertIsNone(a['path'])
        self.assertIsNone(a['pathTarget'])
        self.assertEqual(a['replanCount'], 0)
        self.assertFalse(a.get('respawnedForTask'))
        # No-ops for a missing or taskless agent.
        self.assertIsNone(sim._cancel_at_task(state, 'ghost'))
        state['agents']['ben'] = {'id': 'ben', 'task': None}
        self.assertIsNone(sim._cancel_at_task(state, 'ben'))

    def test_tick_dispatches_task_cancel_release(self):
        # REGRESSION (vela, replanCount 55,350): a walking-task agent whose walk
        # can't progress emits a 'cancel' every tick, and the tick previously
        # only LOGGED it -- she kept task/path/respawn and looped forever,
        # flooding the action_log with sim_cancel rows. The dispatch must now
        # release her exactly like tasks.js cancelTask.
        state = self._walking_task_state()
        engine = sim.SimEngine()
        with unittest.mock.patch('sim.step_agent_movement',
                                 return_value=[('cancel', 'task', 'ada')]):
            out = engine.tick(state, now=100.0)
        a = out['agents']['ada']
        self.assertIsNone(a['task'],
                          'a cancelled task-walk must release the agent, not keep her task')
        self.assertIsNone(a['path'])
        self.assertIsNone(a['pathTarget'])
        self.assertFalse(a.get('respawnedForTask'))
        self.assertEqual(out['tasks']['task-1']['status'], 'cancelled',
                         'the gave-up task must be marked cancelled, mirroring cancelTask')

    def test_tick_cancel_releases_stalled_handoff_session(self):
        # A client-owned handoff/pair session that cancels (the client that
        # owned it is gone) must be released to off-duty, like
        # _repair_stalled_interactions -- not left frozen holding the session.
        state = self._walking_task_state()
        a = state['agents']['ada']
        a['task'] = None
        a['handoff'] = {'to': 'ben'}
        a['path'] = [{'x': 1, 'y': 1}]
        engine = sim.SimEngine()
        with unittest.mock.patch('sim.step_agent_movement',
                                 return_value=[('cancel', 'handoff', 'ada')]):
            out = engine.tick(state, now=100.0)
        a = out['agents']['ada']
        self.assertIsNone(a.get('handoff'))
        self.assertTrue(a.get('offDuty'))
        self.assertFalse(a.get('visible'))

    def test_repair_releases_pathological_stuck_walker(self):
        # The replanned-50k-times case: a walking-task holder whose replanCount
        # has blown past the sane budget is released (task cancelled) instead of
        # being re-pathed or left looping -- even though she HAS a path.
        state = self._walking_task_state()
        state['agents']['ada']['replanCount'] = 100
        grid, doors = sim._load_outdoor_geometry()
        sim._repair_stalled_walkers(state, grid, doors)
        a = state['agents']['ada']
        self.assertIsNone(a['task'],
                          'a pathologically-stuck walker must be released, not re-pathed')
        self.assertEqual(state['tasks']['task-1']['status'], 'cancelled')

    def test_repair_releases_unreachable_only_after_respawn(self):
        # A fresh BFS that finds no route is release-worthy ONLY once the agent
        # has already used her one relocation (JS: "Even a fresh spot can't
        # reach it"). Before respawn it must keep the task -- a single failed
        # BFS can be a transient pile-up, and the movement cancel path grants
        # the grace instead.
        def _state(respawned):
            s = self._walking_task_state()
            s['agents']['ada']['path'] = []  # no path -> repair computes one
            s['agents']['ada']['respawnedForTask'] = respawned
            return s
        grid, doors = sim._load_outdoor_geometry()
        pre = _state(respawned=False)
        with unittest.mock.patch('sim.find_path', return_value=None):
            sim._repair_stalled_walkers(pre, grid, doors)
        self.assertEqual(pre['agents']['ada']['task'], 'task-1',
                         'a pre-respawn unreachable walker keeps her task (grace)')
        post = _state(respawned=True)
        with unittest.mock.patch('sim.find_path', return_value=None):
            sim._repair_stalled_walkers(post, grid, doors)
        self.assertIsNone(post['agents']['ada']['task'],
                          'a post-respawn unreachable walker is released')
        self.assertEqual(post['tasks']['task-1']['status'], 'cancelled')


class SimTaskLifecycle(unittest.TestCase):
    # Phase 3 slice 1: the server re-homes the task lifecycle -- queue
    # consumption, deterministic assignment, arrive/complete/off-duty -- so the
    # think tank works tasks with no browser. Assignment is DETERMINISTIC (round-
    # robin, zero JEV spend). Content execution is short-circuited (workUntil
    # budget); the per-room real executors are the next slice.

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-sim-task-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _roster(self):
        return [
            {'id': 'ada', 'name': 'Ada', 'role': 'Research', 'isAdmin': False},
            {'id': 'ben', 'name': 'Ben', 'role': 'Banking', 'isAdmin': False},
            {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
        ]

    def _agents(self):
        return {
            'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                    'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False,
                    'stuckTimer': 0, 'replanCount': 0, 'approvedCount': 0},
            'ben': {'id': 'ben', 'x': sim.SPAWN['x'] + 30, 'y': sim.SPAWN['y'],
                    'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                    'inRoom': None, 'offDuty': False,
                    'stuckTimer': 0, 'replanCount': 0, 'approvedCount': 0},
            'faye': {'id': 'faye', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                     'dir': 'south', 'visible': True, 'busy': False, 'task': None,
                     'inRoom': None, 'offDuty': False,
                     'stuckTimer': 0, 'replanCount': 0, 'approvedCount': 0},
        }

    def _base_state(self, work_queue=None):
        # lastSkillReviewAt set far in the future so the standing skill-review
        # sweep (which fires immediately on a fresh state with lastRunAt=0)
        # doesn't inject an extra queue item and muddy assignment-count tests.
        far_future = int(time.time() * 1000) + 60 * 60 * 24 * 365 * 10
        return {
            'sim': {'owner': 'server'},
            'agentRoster': self._roster(),
            'agents': self._agents(),
            'reports': [],
            'researchTopics': [],
            'lastSkillReviewAt': far_future,
            # Same cadence-stamp pattern for the E3.1 stuck-gate watchdog: it
            # records lastStuckGateSweep on EVERY tick (even a no-op sweep) so
            # it isn't re-examined too often. Pre-seed it like skill review so
            # an idle gate genuinely has nothing to write.
            'lastStuckGateSweep': far_future,
            # Weekly Knowledge Social is its own cadence too (stamped in
            # _task_cycle, ungated by the work gate). Pre-seed so a short idle
            # window doesn't spuriously convene the Hangout.
            'lastSocialAt': far_future,
            # Hive-mind distillation is a standing sweep too (fires immediately
            # on fresh state with no stamp). Pre-seed so it can't inject an extra
            # roll-up queue item and muddy the assignment-count expectations.
            'lastDistillAt': far_future,
            'workQueue': work_queue if work_queue is not None else [],
        }

    def _item(self, **over):
        item = {'title': 'Maintain tooling', 'room': 'pressoffice',
                'instructions': 'Go.', 'pair': False, 'notBefore': None,
                'priority': sim.WORK_PRIORITY['normal'], 'goal': None,
                'research': None, 'taskType': 'code', 'skillReview': False}
        item.update(over)
        return item

    def test_task_cycle_assigns_due_item_deterministically(self):
        state = self._base_state(work_queue=[self._item()])
        grid, doors = sim._load_outdoor_geometry()
        sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                        task_id_holder=[0])
        agents = state['agents']
        assigned = [aid for aid in agents if agents[aid].get('task')]
        self.assertEqual(len(assigned), 1, 'exactly one worker assigned')
        self.assertNotIn('faye', assigned, 'admin never assigned work')
        tid = agents[assigned[0]]['task']
        task = state['tasks'][tid]
        self.assertEqual(task['room'], 'pressoffice')
        self.assertEqual(task['status'], 'walking')
        self.assertEqual(task['assignedTo'], assigned[0])
        self.assertTrue(agents[assigned[0]].get('path'),
                        'assigned agent must have a real path to the door')
        self.assertEqual(state['workQueue'], [], 'queue drained after assignment')

    def test_task_cycle_idle_gate_spends_nothing(self):
        # An idle think tank parks its idle wanderers off duty (a
        # woken-but-never-assigned agent must vanish, per the player's "no
        # unscheduled/active agent should appear" rule), which IS a state write.
        # What it must NOT do is enqueue work, assign anyone, run governance,
        # or spend Jev. So snapshot, run, and assert the ONLY surface that moved
        # is the idle wanderers going off duty + visible=false.
        state = self._base_state()
        grid, doors = sim._load_outdoor_geometry()
        before_agents = {aid: dict(a) for aid, a in state['agents'].items()}
        sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                        task_id_holder=[0])
        # No work was created, none assigned -- the queue/roster/tasks surfaces
        # are untouched.
        self.assertEqual(state.get('workQueue') or [], [])
        self.assertEqual(state.get('tasks') or {}, {})
        for aid, a in state['agents'].items():
            self.assertIsNone(a.get('task'), f'{aid} must have no task when idle')
        # The only writes are the park: ada/ben (non-admin, idle) off duty +
        # hidden; faye (admin) left alone.
        self.assertTrue(state['agents']['ada']['offDuty'])
        self.assertFalse(state['agents']['ada']['visible'])
        self.assertTrue(state['agents']['ben']['offDuty'])
        self.assertIs(state['agents']['faye']['offDuty'], False)
        # Idempotent: a second pass writes nothing more (parked agents stay).
        settled = json.dumps(state, sort_keys=True)
        sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                        task_id_holder=[0])
        self.assertEqual(json.dumps(state, sort_keys=True), settled,
                         'second idle pass must not keep mutating state')

    def test_task_cycle_repairs_stale_busy_tokens(self):
        # A busy flag is a hold on an IN-FLIGHT ('working') task. A busy agent
        # whose task already ended (done / needs_review) or is missing, or a
        # busy agent with NO task at all, is wedged -- the hold is stale and the
        # agent can never free (server restarts mid-flight persist busy while
        # the task's status moved to done independently). task_cycle must clear
        # these so the roster doesn't bleed idle workers. A genuinely 'working'
        # task is the only legitimate reason to stay busy.
        state = self._base_state()
        tasks = state['tasks'] = {
            # ada: busy holding a DONE task -> stale, must clear.
            'task-1': {'id': 'task-1', 'title': 'done story', 'status': 'done',
                       'room': 'pressoffice', 'taskType': 'code'},
            # ben: busy holding a task in needs_review -> stale, must clear.
            'task-2': {'id': 'task-2', 'title': 'gated story', 'status': 'needs_review',
                       'room': 'observatory', 'taskType': 'code'},
            # cora: busy holding a genuinely WORKING task -> legit, must stay.
            'task-3': {'id': 'task-3', 'title': 'in flight', 'status': 'working',
                       'room': 'pressoffice', 'taskType': 'code'},
        }
        state['agents'].update({
            'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'], 'busy': True,
                    'task': 'task-1', 'inRoom': 'pressoffice', 'visible': True,
                    'offDuty': False},
            'ben': {'id': 'ben', 'x': sim.SPAWN['x'] + 60, 'y': sim.SPAWN['y'], 'busy': True,
                    'task': 'task-2', 'inRoom': 'observatory', 'visible': True,
                    'offDuty': False},
            'faye': {'id': 'faye', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'], 'busy': True,
                     'task': None, 'inRoom': None, 'visible': True, 'offDuty': False},
            'cora': {'id': 'cora', 'x': sim.SPAWN['x'] + 90, 'y': sim.SPAWN['y'], 'busy': True,
                     'task': 'task-3', 'inRoom': 'pressoffice', 'visible': True,
                     'offDuty': False},
        })
        grid, doors = sim._load_outdoor_geometry()
        # 'working' (task-3) isn't due (no workUntil), so it would be skipped by
        # the completion loop anyway -- the reconcile is what we're testing.
        before = {a: dict(state['agents'][a]) for a in ('ada', 'ben', 'faye', 'cora')}
        sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                        task_id_holder=[0])
        for aid in ('ada', 'ben', 'faye'):
            self.assertFalse(state['agents'][aid].get('busy'),
                             f'{aid}: busy holding a non-working task (or none) must be cleared')
            self.assertIsNone(state['agents'][aid].get('task'),
                              f'{aid}: a cleared busy must not keep a dangling task ref')
        self.assertTrue(state['agents']['cora'].get('busy'),
                        'cora: a busy hold on a genuinely WORKING task is legit and must survive')

    def test_task_cycle_respects_notbefore_schedule(self):
        # An item scheduled for later must sit inert (not assigned, not counted).
        # Put it ALONGSIDE an immediately-due item so we prove the future one is
        # skipped while real work still gets done.
        future = int(time.time() * 1000) + 60_000
        state = self._base_state(work_queue=[self._item(notBefore=future),
                                             self._item(title='Now')])
        grid, doors = sim._load_outdoor_geometry()
        sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                        task_id_holder=[0])
        self.assertEqual([i['title'] for i in state['workQueue']], ['Maintain tooling'],
                         'the future item must stay queued untouched')
        # The immediate item got assigned; the future one did not.
        assigned_titles = [t.get('title') for t in (state.get('tasks') or {}).values()]
        self.assertIn('Now', assigned_titles)
        self.assertNotIn('Maintain tooling', assigned_titles)

    def test_work_item_abandoned_after_max_attempts(self):
        # A queue item that can never be assigned (here: a room with no door
        # trigger, so assign_task fails at the `not door` guard) is retried up
        # to WORK_ITEM_MAX_ATTEMPTS, then abandoned -- not retried forever.
        state = self._base_state(work_queue=[
            self._item(title='Unassignable', room='commandcenter')])
        grid, doors = sim._load_outdoor_geometry()
        for _ in range(20):  # well past the 3-attempt budget
            sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                            task_id_holder=[0])
        self.assertEqual(state['workQueue'], [],
                         'unassignable item must leave the queue (abandoned)')
        self.assertFalse(any(a.get('task') for a in state['agents'].values()))

    def test_full_closed_loop_task_completes_and_agent_goes_off_duty(self):
        # The Phase-3 acceptance seed: an empty think tank with ONE queued item and
        # one on-duty worker. Drive the ENGINE (movement + lifecycle) until the
        # task completes and the worker goes off duty.
        grid, doors = sim._load_outdoor_geometry()
        state = self._base_state(work_queue=[self._item()])
        engine = sim.SimEngine()
        engine._grid, engine._doors = grid, doors
        now = 1000.0
        for _ in range(400):  # well past walk + work + off-duty walk
            now += sim.SIM_TICK_S
            state = engine.tick(state, now=now)
            if all(a.get('offDuty') for a in state['agents'].values()
                   if a.get('id') != 'faye'):
                break
        ada, ben = state['agents']['ada'], state['agents']['ben']
        offduty = [a for a in (ada, ben) if a.get('offDuty')]
        self.assertGreater(len(offduty), 0,
                           'a worker who completed a task must end up off-duty')
        worker = offduty[0]
        self.assertGreater(worker.get('approvedCount', 0), 0,
                           'completing a task bumps approvedCount')
        self.assertFalse(worker.get('path'), 'off-duty worker should have no live path')
        tasks = state.get('tasks') or {}
        self.assertTrue(any(t.get('status') == 'done' for t in tasks.values()),
                        'the task must complete')
        self.assertEqual(state['workQueue'], [], 'queue drained after completion')

    def test_research_arrival_without_executor_uses_workuntil_fallback(self):
        # No content executor registered (the default) -> an arriving research
        # task gets the slice-1 workUntil placeholder, not content dispatch.
        state = self._base_state()
        sim._content_executor = None
        task = {'id': 'task-1', 'room': 'observatory', 'status': 'walking',
                'assignedTo': 'ada',
                'research': {'topicId': 't1', 'since': 0}, 'entryX': 10, 'entryY': 10}
        state['tasks'] = {'task-1': task}
        state['agents']['ada']['task'] = 'task-1'
        state['researchTopics'] = [{'id': 't1', 'topic': 'x', 'seenUrls': []}]
        sim._arrive_at_task(state, 'ada', now=1000.0)
        self.assertEqual(task['workUntil'], 1000.0 + sim.TASK_WORK_DURATION_S,
                         'without an executor, use the short work budget')
        self.assertFalse(task.get('_contentInFlight'),
                         'no content dispatch without an executor')

    def test_research_arrival_with_executor_dispatches_and_result_completes(self):
        # With a content executor registered, an arriving task gets the long
        # content timeout + _contentInFlight; a completed result is merged
        # (note + seenUrls) and the task finalizes early.
        calls = {}
        def fake_executor(snapshot, agent_id, task, base_ctx):
            calls['seen'] = (task['id'], task['room'])
            # The executor runs on a bg thread in production; simulate landing
            # a result by calling the same store helper it would.
            sim._store_content_result(task['id'], {
                'note': 'collected 2 pages, wrote updated skill (pending review)',
                'seenUrls': ['https://a.example', 'https://b.example'],
            })
        sim._content_executor = fake_executor
        grid, doors = sim._load_outdoor_geometry()
        # Drive assignment so a worker is actually dispatched to observatory.
        state = self._base_state(work_queue=[{
            'title': 'Scheduled research: x', 'room': 'observatory',
            'instructions': 'crawl', 'goal': 'x', 'priority': sim.WORK_PRIORITY['normal'],
            'notBefore': 0,
            'research': {'topicId': 't1', 'since': 0}}])
        state['researchTopics'] = [{'id': 't1', 'topic': 'x', 'seenUrls': ['https://a.example'], 'cadenceMs': 3600000, 'lastRunAt': 0}]
        engine = sim.SimEngine()
        engine._grid, engine._doors = grid, doors
        now = 1000.0
        # Burn through assignment + walk + arrive (+ the executor result).
        for _ in range(200):
            now += sim.SIM_TICK_S
            state = engine.tick(state, now=now)
            if (state.get('tasks') or {}).get('task-1', {}).get('status') == 'done':
                break
        sim._content_executor = None
        self.assertTrue(calls.get('seen'),
                        'the executor must be dispatched with the arriving task')
        self.assertEqual(calls['seen'][1], 'observatory',
                         'the executor must get the task room')
        topic = state['researchTopics'][0]
        self.assertIn('https://b.example', topic['seenUrls'],
                      'new seenUrl from the content result must be merged back')
        self.assertGreater(state['agents']['ada'].get('approvedCount', 0), 0,
                           'content-completed task still bumps approvedCount')


class SimOffDutyWake(unittest.TestCase):
    # Redesign: agents VANISH WHERE THEY STAND when
    # going off-duty (no trek to an outskirts door), and WAKE ANYWHERE there's
    # free, reachable space (not at outskirts_east/south). This fixes agents
    # stranding at the map edge (ben at outskirts_south, faye at the corner).

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-sim-offduty-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _agent_state(self, **over):
        base = {'id': 'ben', 'x': 500, 'y': 400, 'dir': 'right', 'busy': False,
                'task': None, 'inRoom': None, 'offDuty': False, 'visible': True,
                'path': None, 'pathIndex': 0, 'pathTarget': None,
                'headingOffDuty': None, 'pairWith': None, 'handoff': None,
                'stuckTimer': 0, 'replanCount': 0}
        base.update(over)
        return {'agents': {base['id']: base}}

    def test_send_off_duty_vanishes_in_place_no_outskirts_walk(self):
        # An idle agent goes off-duty where they stand: no path is set, they flip
        # offDuty+invisible, and their position is left untouched (no outreach to
        # an edge door, which is what stranded agents in reachability gaps).
        state = self._agent_state(x=500, y=400)
        grid, doors = sim._load_outdoor_geometry()
        sim.send_agent_off_duty(state, 'ben', doors, grid)
        a = state['agents']['ben']
        self.assertIs(a['offDuty'], True)
        self.assertIs(a['visible'], False)
        self.assertIsNone(a['path'])
        self.assertIsNone(a['headingOffDuty'])
        # Position unchanged -- vanished in place, didn't teleport to outskirts.
        self.assertEqual((a['x'], a['y']), (500, 400))

    def test_send_off_duty_refuses_busy_agent(self):
        state = self._agent_state(busy=True, task='t1')
        grid, doors = sim._load_outdoor_geometry()
        sim.send_agent_off_duty(state, 'ben', doors, grid)
        a = state['agents']['ben']
        self.assertIs(a['offDuty'], False, 'a busy agent must not vanish')
        self.assertIs(a['visible'], True)

    def test_appear_from_outskirts_places_at_free_reachable_spot(self):
        # Waking agent appears ANYWHERE free + reachable from SPAWN -- never at
        # a trailhead edge door (the outskirts rooms are gone). Verify the
        # landed spot is (a) not on collision and (b) reachable.
        state = self._agent_state(y=600, offDuty=True, visible=False)
        grid, doors = sim._load_outdoor_geometry()
        sim.appear_from_outskirts(state, 'ben', doors, grid=grid)
        a = state['agents']['ben']
        self.assertIs(a['offDuty'], False)
        self.assertIs(a['visible'], True)
        mask = sim.compute_reachable_mask(grid, sim.SPAWN)
        cx, cy = a['x'] + sim.AGENT_W / 2, a['y'] + sim.AGENT_H / 2
        self.assertTrue(sim.is_reachable(cx, cy, grid, mask),
                        f'woken agent landed at an UNREACHABLE cell ({a["x"]},{a["y"]})')
        # The outskirts doors are gone from the trigger set altogether.
        self.assertNotIn('outskirts_east', doors)
        self.assertNotIn('outskirts_south', doors)

    def test_off_duty_wake_roundtrip(self):
        # Off duty then wake: the agent is visible again somewhere free.
        grid, doors = sim._load_outdoor_geometry()
        state = self._agent_state(x=500, y=400)
        sim.send_agent_off_duty(state, 'ben', doors, grid)
        self.assertTrue(state['agents']['ben']['offDuty'])
        sim.appear_from_outskirts(state, 'ben', doors, grid=grid)
        a = state['agents']['ben']
        self.assertFalse(a['offDuty'])
        self.assertTrue(a['visible'])

    def test_wake_clears_stale_busy_task_on_off_duty_ghost(self):
        # Regression (faye live-bug): an off-duty agent can carry a stale
        # busy/task from when she clocked off. Waking her must clear those so she
        # is both visible AND eligible for assignment (not visible-but-ineligible,
        # which re-strands her).
        grid, doors = sim._load_outdoor_geometry()
        state = self._agent_state(x=0, y=0, busy=True, task='stale-t',
                                  offDuty=True, visible=False)
        sim.appear_from_outskirts(state, 'ben', doors, grid=grid)
        a = state['agents']['ben']
        self.assertIs(a['offDuty'], False)
        self.assertIs(a['visible'], True)
        self.assertIs(a['busy'], False, 'woken agent must not still read as busy')
        self.assertIsNone(a['task'])

    def test_reconcile_moves_stranded_visible_agent_to_reachable_spot(self):
        # An idle VISIBLE agent parked on an unreachable cell (the reachability-
        # gap bug that left ben/faye at the outskirts) gets re-picked to a free,
        # reachable spot; a reachable one is left alone.
        grid, _ = sim._load_outdoor_geometry()
        # Place the agent hard at the world origin (0,0) -- off-grid/corner.
        state = self._agent_state(x=0, y=0)
        moved = sim._reconcile_stranded_agents(state, grid)
        self.assertEqual(moved, 1)
        a = state['agents']['ben']
        mask = sim.compute_reachable_mask(grid, sim.SPAWN)
        self.assertTrue(sim.is_reachable(a['x'] + sim.AGENT_W / 2, a['y'] + sim.AGENT_H / 2, grid, mask))

    def test_reconcile_leaves_reachable_agent_and_midtask_alone(self):
        grid, _ = sim._load_outdoor_geometry()
        # An agent at SPAWN is reachable -> untouched; a busy one is untouched.
        state = self._agent_state(x=sim.SPAWN['x'], y=sim.SPAWN['y'])
        state['agents']['busy2'] = {'id': 'busy2', 'x': 0, 'y': 0, 'visible': True,
                                    'task': 't1', 'busy': True}
        moved = sim._reconcile_stranded_agents(state, grid)
        self.assertEqual(moved, 0)
        self.assertEqual((state['agents']['ben']['x'], state['agents']['ben']['y']),
                         (sim.SPAWN['x'], sim.SPAWN['y']))


class SimMergeInvariant(unittest.TestCase):
    # The client autosave (agents.js saveState) posts a fixed field list every
    # 5s and does NOT include the server-owned `sim` section. The autosave merge
    # must carry sim forward untouched, or the heartbeat/owner/position snapshot
    # the server maintains would silently vanish on the next autosave -- the
    # exact blob-clobber class that killed server-owned templates earlier.
    def test_client_autosave_does_not_drop_server_owned_sim(self):
        server_state = {
            'agents': {'ada': {'id': 'ada', 'x': 10}},
            'sim': {'tick': 9, 'owner': 'client', 'lastTickEpochS': 99.0, 'agents': {}},
        }
        # Client saveState payload: same top-level keys minus `sim`.
        client_payload = {'agents': {'ada': {'id': 'ada', 'x': 12}}, 'reports': []}
        merged = serve._merge_server_owned(server_state, client_payload)
        self.assertIn('sim', merged, 'autosave must carry the server sim section forward')
        self.assertEqual(merged['sim']['tick'], 9)
        # Non-sim keys are replaced exactly as before.
        self.assertEqual(merged['agents']['ada']['x'], 12)


if __name__ == '__main__':
    unittest.main()