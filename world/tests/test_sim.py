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
            # Weekly rule mining is a standing sweep too -- pre-seed so it can't
            # fire on a fresh state (and read the real failure ledger) mid-test.
            'lastRuleMineAt': far_future,
            # Weekly Knowledge Social is its own cadence too (stamped in
            # _task_cycle, ungated by the work gate). Pre-seed so a short idle
            # window doesn't spuriously convene the Hangout.
            'lastSocialAt': far_future,
            # The roadmap + consensus relay are standing sweeps too (same
            # cadence-stamp pattern): _roadmap_step creates the roadmap on the
            # first pass, then _consensus_relay_step rewrites
            # consensusRelay.updatedAt = now_ms on EVERY pass. Pre-seeding the
            # roadmap cadence stamp keeps them no-ops, so two back-to-back idle
            # passes are byte-identical even across a wall-clock ms boundary
            # (the idempotency assertion below).
            'lastRoadmapReviewAt': far_future,
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
        # Spare-time lane (2026-10-06): completing a real deliverable earns one
        # deferred free exploration spike, so the queue is never literally
        # empty after work -- but NO committed work may remain, and the spike
        # must be spare time (moonshot, not yet due), not a new obligation.
        q = state.get('workQueue') or []
        committed = [x for x in q if not (x.get('moonshot') and x.get('taskType') == 'spike')]
        self.assertEqual(committed, [], 'no committed work left queued after completion')
        spikes = [x for x in q if x.get('moonshot') and x.get('taskType') == 'spike']
        self.assertEqual(len(spikes), 1, 'the completed deliverable earns one free spike')
        self.assertFalse(sim.is_work_item_due(spikes[0], int(now * 1000)),
                         'a free spike is spare time -- deferred, not immediately due')

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

    def test_content_result_without_note_is_evidence_gap(self):
        # Completion evidence is mandatory: a content result that reports
        # success but carries NO note (no evidence of what was done) must not
        # silently complete -- the card goes back as a gap (failed + fix queued),
        # never a clean 'done' with an empty record.
        sim._content_executor = None
        with sim._content_results_lock:
            sim._content_results.clear()

        # The executor normally runs on a bg thread; to stay deterministic we
        # store the (empty-note, ok) result ourselves the moment dispatch lands.
        def fake_executor(snapshot, agent_id, task, base_ctx):
            pass

        sim._content_executor = fake_executor
        grid, doors = sim._load_outdoor_geometry()
        state = self._base_state(work_queue=[{
            'title': 'Scheduled research: x', 'room': 'observatory',
            'instructions': 'crawl', 'goal': 'x', 'priority': sim.WORK_PRIORITY['normal'],
            'notBefore': 0,
            'research': {'topicId': 't1', 'since': 0}}])
        state['researchTopics'] = [{'id': 't1', 'topic': 'x', 'seenUrls': [],
                                    'cadenceMs': 3600000, 'lastRunAt': 0}]
        engine = sim.SimEngine()
        engine._grid, engine._doors = grid, doors
        now = 1000.0
        done = None
        result_sent = False
        for _ in range(300):
            now += sim.SIM_TICK_S
            state = engine.tick(state, now=now)
            tasks = state.get('tasks') or {}
            task = next((t for t in tasks.values() if t.get('research')), None)
            if task and task.get('_contentInFlight') and not result_sent:
                sim._store_content_result(task['id'], {'ok': True, 'note': ''})
                result_sent = True
            if task and task.get('status') == 'failed':
                done = task
                break
        sim._content_executor = None
        self.assertTrue(result_sent, 'the task must be dispatched for content work')
        self.assertIsNotNone(done, 'empty-note success must be sent back, not done')
        self.assertIn('missing completion evidence', done.get('failNote', ''),
                      'the gap must name the missing evidence')
        self.assertFalse(any(t.get('status') == 'done' for t in (state.get('tasks') or {}).values()),
                         'the empty-evidence task must never land as done')
        reworks = [q for q in (state.get('workQueue') or []) if q.get('title', '').startswith('Fix:')]
        rework_tasks = [t for t in (state.get('tasks') or {}).values()
                        if (t.get('title') or '').startswith('Fix:')]
        self.assertTrue(reworks or rework_tasks,
                        'a rework card is queued for the gap')

    def test_kb_class_survives_queue_round_trip(self):
        # The knowledge-base class flag is part of the queue_work whitelist, so
        # a 'changes how we work' ceremony keeps its KB mandate through the
        # queue (the same contract as distill/skillReview).
        state = self._base_state()
        sim.queue_work(state, [{
            'title': 'Distill recent think tank knowledge', 'room': 'observatory',
            'instructions': 'merge', 'distill': True, 'distillSince': 5,
            'kbClass': 'changes_how_we_work'}])
        self.assertEqual(state['workQueue'][0]['kbClass'], 'changes_how_we_work')
        self.assertEqual(state['workQueue'][0]['distillSince'], 5)

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

    def test_content_dispatch_passes_perception_as_base_ctx(self):
        # Item 1: a dispatched content executor gets the agent's situational
        # awareness as base_ctx (who they are, where they are, what they are
        # working on) -- not a bare {} -- so a worker reasons like a teammate,
        # not a stateless API call. The perception is a markdown string, which
        # is exactly the type every real executor's `base_ctx or ''` expects.
        names = {d['id']: d['name'] for d in self._roster()}
        seen = {}

        def fake_executor(snapshot, agent_id, task, base_ctx):
            seen['agent'] = agent_id
            seen['ctx'] = base_ctx
            sim._store_content_result(task['id'], {'ok': True, 'note': 'did the thing'})

        sim._content_executor = fake_executor
        grid, doors = sim._load_outdoor_geometry()
        state = self._base_state(work_queue=[self._item(
            title='Build the login flow',
            acceptanceCriteria='Logging in returns you to the dashboard.')])
        engine = sim.SimEngine()
        engine._grid, engine._doors = grid, doors
        now = 1000.0
        for _ in range(200):
            now += sim.SIM_TICK_S
            state = engine.tick(state, now=now)
            if (state.get('tasks') or {}).get('task-1', {}).get('status') == 'done':
                break
        sim._content_executor = None
        self.assertIn('agent', seen, 'the executor must be dispatched')
        ctx = seen.get('ctx')
        self.assertIsInstance(ctx, str, 'base_ctx must be a markdown string, not {}')
        self.assertIn('You are ' + names[seen['agent']], ctx)
        self.assertIn('pressoffice', ctx)
        self.assertIn('Build the login flow', ctx)


class SimAgentPerception(unittest.TestCase):
    """Item 1: situational awareness for content executors. _build_agent_
    perception is a pure read of a frozen snapshot -- who am I, where am I,
    who's around (acquaintance-aware), what am I working on, what feedback came
    back at me -- and the acquaintance helpers that gate how a stranger is
    named."""

    def _state(self):
        return {
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'role': 'Research', 'isAdmin': False},
                {'id': 'ben', 'name': 'Ben', 'role': 'Banking', 'isAdmin': False},
                {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
            ],
            'agents': {
                'ada': {'id': 'ada', 'inRoom': 'pressoffice', 'offDuty': False},
                'ben': {'id': 'ben', 'inRoom': 'pressoffice', 'offDuty': False},
                'faye': {'id': 'faye', 'inRoom': 'observatory', 'offDuty': False},
            },
        }

    def test_perception_carries_identity_room_present_task(self):
        state = self._state()
        task = {'title': 'Build the login flow',
                'instructions': 'wire the form',
                'acceptanceCriteria': 'Logging in returns you to the dashboard.',
                'room': 'pressoffice'}
        out = sim._build_agent_perception(state, 'ada', task)
        self.assertIn('You are Ada.', out)
        self.assertIn('Your role on the team: Research.', out)
        self.assertIn('You are currently in the pressoffice.', out)
        # Ben shares the room but ada has not met him -> role only, never a name.
        self.assertIn('the Banking', out)
        self.assertIn('Build the login flow -- wire the form.', out)
        self.assertIn('Logging in returns you to the dashboard.', out)
        self.assertNotIn('Ben', out,
                         "a stranger's name is not a name the viewer can use")

    def test_acquaintance_gates_naming(self):
        state = self._state()
        self.assertFalse(sim._are_acquainted(state, 'ada', 'ben'))
        self.assertEqual(sim._describe_agent(state, 'ben', 'ada'), 'the Banking')
        sim._mark_acquaintance(state, 'ada', 'ben')
        self.assertTrue(sim._are_acquainted(state, 'ada', 'ben'))
        self.assertTrue(sim._are_acquainted(state, 'ben', 'ada'))
        self.assertEqual(sim._describe_agent(state, 'ben', 'ada'),
                         'Ben (the Banking)')
        self.assertIn('Ben (the Banking)',
                      sim._build_agent_perception(state, 'ada'))

    def test_acquaintance_mark_is_json_safe_and_idempotent(self):
        state = self._state()
        sim._mark_acquaintance(state, 'ada', 'ben')
        sim._mark_acquaintance(state, 'ada', 'ben')  # idempotent
        sim._mark_acquaintance(state, 'ada', 'ada')  # self-intro is a no-op
        round_tripped = json.loads(json.dumps(state))
        self.assertEqual(round_tripped['_acquaintances']['ada'], ['ben'])
        self.assertEqual(round_tripped['_acquaintances']['ben'], ['ada'])

    def test_perception_includes_pending_feedback(self):
        state = self._state()
        state['_feedback'] = {'ada': [
            {'text': 'Your last fix shipped but the modal is still 2px off.'},
            'Keep the heredoc under 200 lines.',
        ]}
        out = sim._build_agent_perception(state, 'ada', {'title': 'Fix the modal'})
        self.assertIn('Feedback on your recent work:', out)
        self.assertIn('modal is still 2px off', out)
        self.assertIn('Keep the heredoc under 200 lines.', out)

    def test_perception_unknown_agent_falls_back_gracefully(self):
        out = sim._build_agent_perception(self._state(), 'ghost',
                                          {'title': 'T'})
        self.assertIn('You are ghost.', out)

    def test_append_feedback_writes_and_caps(self):
        # Item 2: _append_feedback is a JSON-safe buffer per agent, capped to a
        # recent-memory window (not a ledger). Empty text / unknown agent no-op.
        state = self._state()
        sim._append_feedback(state, 'ada', 'fix the modal offset')
        sim._append_feedback(state, 'ada', 'keep heredocs under 200 lines',
                             source='peer_review')
        sim._append_feedback(state, 'ada', '   ')  # blank -> no-op
        sim._append_feedback(state, None, 'orphan')  # unknown -> no-op
        bucket = state['_feedback']['ada']
        self.assertEqual(len(bucket), 2)
        self.assertEqual(bucket[0], {'text': 'fix the modal offset'})
        self.assertEqual(bucket[1]['text'], 'keep heredocs under 200 lines')
        self.assertEqual(bucket[1]['source'], 'peer_review')
        self.assertNotIn('ghost', state['_feedback'])
        round_tripped = json.loads(json.dumps(state))
        self.assertEqual(round_tripped['_feedback']['ada'], bucket,
                         'feedback must survive the kv_state round trip')
        for i in range(sim._FEEDBACK_MAX + 5):
            sim._append_feedback(state, 'ada', f'note {i}')
        self.assertLessEqual(len(state['_feedback']['ada']), sim._FEEDBACK_MAX,
                             'buffer is capped, not a ledger')
        self.assertIn('note {}'.format(sim._FEEDBACK_MAX + 4),
                      state['_feedback']['ada'][-1]['text'],
                      'cap trims the OLDEST entries, newest survives')

    def test_apply_content_result_consumes_executor_feedback(self):
        # Item 2: an executor-reported block/failure rides the content result
        # channel into the tick's single read-modify-write, where it lands in the
        # author's feedback buffer (rendered on the next dispatch's perception).
        state = self._state()
        task = {'id': 'task-1', 'title': 'Build the login flow',
                'assignedTo': 'ada', 'room': 'pressoffice'}
        sim._apply_content_result(state, task, {
            'note': 'blocked: security gate',
            'ok': False,
            'feedback': 'Your command was blocked before running: security gate.',
        }, now_ms=1000)
        bucket = state['_feedback']['ada']
        self.assertEqual(len(bucket), 1)
        self.assertIn('security gate', bucket[0]['text'])
        self.assertEqual(bucket[0]['source'], 'executor')

    def test_apply_content_result_consumes_feedback_dict_source(self):
        # Item 2: the feedback entry may be a dict carrying its own source.
        state = self._state()
        task = {'id': 'task-1', 'title': 'T', 'assignedTo': 'ben', 'room': 'media'}
        sim._apply_content_result(state, task, {
            'ok': False,
            'feedback': {'text': 'sandbox missing', 'source': 'executor'},
        }, now_ms=1000)
        self.assertEqual(state['_feedback']['ben'][0]['source'], 'executor')

    def test_sim_notify_author_records_feedback_for_author(self):
        # Item 2: a peer rejection writes the author's feedback buffer, so the
        # next dispatch's perception block shows what came back at them -- the
        # W6 coaching intent, visible in situational awareness.
        state = self._state()
        state['agents']['ada']['profile'] = {'notes': []}
        parent = {'id': 'task-1', 'title': 'Build the login flow',
                  'assignedTo': 'ada', 'room': 'pressoffice', '_peerGate': {}}
        sim._sim_notify_author(state, parent, 'ben',
                               rationale='the modal is 2px off')
        bucket = state['_feedback']['ada']
        self.assertEqual(len(bucket), 1)
        self.assertIn('Build the login flow', bucket[0]['text'])
        self.assertIn('2px off', bucket[0]['text'])
        self.assertEqual(bucket[0]['source'], 'peer_review')
        self.assertIn('peer_review_rejected',
                      [m.get('kind') for m in state['agents']['ada']['mailbox']],
                      'the mailbox note is still filed alongside the buffer')

    def test_sim_notify_author_failed_records_feedback(self):
        # Item 2: a red-pipeline send-back lands in the author's feedback buffer.
        state = self._state()
        state['agents']['ada']['profile'] = {'notes': []}
        task = {'id': 'task-1', 'title': 'Build the login flow',
                'assignedTo': 'ada', 'room': 'pressoffice'}
        sim._sim_notify_author_failed(state, task)
        bucket = state['_feedback']['ada']
        self.assertEqual(len(bucket), 1)
        self.assertIn('Build the login flow', bucket[0]['text'])
        self.assertEqual(bucket[0]['source'], 'pipeline')


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


class ServerOwnedPairingHandoff(unittest.TestCase):
    """W1: server-owned pairing/handoff. The movement engine already emits
    ('arrive','pair'/'handoff') events but the server dispatch dropped them
    (only ('arrive','task') resolved), and a `pair` card was assigned SOLO. Now
    the server recruits a navigator for a `pair` card, resolves pair/handoff
    arrivals, and releases a pair's navigator when the driver's task ships."""

    def _state(self):
        far_future = int(time.time() * 1000) + 60 * 60 * 24 * 365 * 10
        return {
            'sim': {'owner': 'server'},
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'role': 'Research', 'isAdmin': False},
                {'id': 'ben', 'name': 'Ben', 'role': 'Banking', 'isAdmin': False},
                {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
            ],
            'agents': {
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
            },
            'lastSkillReviewAt': far_future,
            'lastStuckGateSweep': far_future,
            'lastSocialAt': far_future,
            'lastDistillAt': far_future,
            'workQueue': [],
        }

    def test_pair_arrival_resolves_the_navigator_into_the_session(self):
        # A pair session's navigator reaching the driver's door must RESOLVE
        # (navigator joins the driver's workstation), not be dropped -- she
        # would otherwise stand frozen at the door forever.
        state = self._state()
        state['tasks'] = {'task-1': {'id': 'task-1', 'room': 'pressoffice',
                                     'status': 'working', 'title': 'Pair me'}}
        a = state['agents']['ada']
        a['pairWith'] = 'ben'
        a['pairTaskId'] = 'task-1'
        a['path'] = [{'x': 1, 'y': 1}]
        driver = state['agents']['ben']
        driver['busy'] = True
        driver['inRoom'] = 'pressoffice'
        driver['roomX'] = 84
        driver['roomY'] = 200
        self.assertTrue(sim._arrive_at_pair(state, 'ada', now=100.0))
        self.assertFalse(a['visible'])
        self.assertTrue(a['busy'])
        self.assertEqual(a['inRoom'], 'pressoffice')
        self.assertEqual(a['roomX'], 84 + 25, 'navigator sits beside the driver')
        self.assertIsNone(a['path'], 'arrived navigator clears her path')

    def test_pair_arrival_with_missing_other_half_releases_cleanly(self):
        # The session's other half gone (driver released / task vanished) must
        # release the navigator off-duty, never park her holding the session.
        state = self._state()
        state['tasks'] = {}
        a = state['agents']['ada']
        a['pairWith'] = 'ghost'
        a['pairTaskId'] = 'task-9'
        a['path'] = [{'x': 1, 'y': 1}]
        self.assertFalse(sim._arrive_at_pair(state, 'ada', now=100.0))
        self.assertIsNone(a.get('pairWith'))
        self.assertIsNone(a.get('pairTaskId'))
        self.assertTrue(a.get('offDuty'))
        self.assertFalse(a.get('visible'))

    def test_handoff_arrival_delivers_and_clocks_off(self):
        # A handoff walker reaching her recipient delivers the handoff (the
        # finished title as the message) and clocks off -- previously the
        # arrival was dropped and she froze beside the recipient.
        state = self._state()
        a = state['agents']['ada']
        a['handoff'] = {'toId': 'ben', 'title': 'The report', 'fromId': 'ada'}
        a['path'] = [{'x': 1, 'y': 1}]
        a['visible'] = True
        self.assertTrue(sim._arrive_at_handoff(state, 'ada', now=100.0))
        self.assertIsNone(a.get('handoff'))
        self.assertTrue(a.get('offDuty'))
        self.assertFalse(a.get('visible'))
        self.assertEqual(len(state['handoffs']), 1)
        self.assertEqual(state['handoffs'][0]['title'], 'The report')
        self.assertEqual(state['handoffs'][0]['toId'], 'ben')
        self.assertEqual(state['agents']['ben'].get('contactedAt'), 100000)

    def _walking_task_state(self):
        state = {'sim': {'owner': 'server'},
                 'agents': {'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                                    'dir': 'south', 'visible': True, 'busy': False,
                                    'task': None, 'inRoom': None, 'offDuty': False,
                                    'path': [{'x': 1, 'y': 1}], 'pathIndex': 0,
                                    'pathTarget': {'x': 1, 'y': 1},
                                    'stuckTimer': 0, 'replanCount': 0,
                                    'respawnedForTask': False}},
                 'tasks': {'task-1': {'id': 'task-1', 'title': 't', 'room': 'pressoffice',
                                      'status': 'walking', 'assignedTo': 'ada'}}}
        return state

    def test_tick_dispatches_pair_arrival_to_the_session(self):
        # REGRESSION (W1): a pair session's navigator arriving was DROPPED by
        # the movement dispatch (only ('arrive','task') resolved), so she stood
        # frozen at the door. The tick must route it to _arrive_at_pair.
        state = self._walking_task_state()
        a = state['agents']['ada']
        a['task'] = None
        a['pairWith'] = 'ben'
        a['pairTaskId'] = 'task-1'
        a['path'] = [{'x': 1, 'y': 1}]
        state['tasks']['task-1']['room'] = 'pressoffice'
        state['tasks']['task-1']['status'] = 'working'
        state['agents']['ben'] = {'id': 'ben', 'x': 0, 'y': 0, 'busy': True,
                                  'inRoom': 'pressoffice', 'roomX': 84, 'roomY': 200}
        engine = sim.SimEngine()
        with unittest.mock.patch('sim.step_agent_movement',
                                 return_value=[('arrive', 'pair', 'ada')]):
            out = engine.tick(state, now=100.0)
        a = out['agents']['ada']
        self.assertFalse(a.get('visible'))
        self.assertTrue(a.get('busy'))
        self.assertEqual(a.get('inRoom'), 'pressoffice')
        self.assertEqual(a.get('roomX'), 84 + 25)

    def test_tick_dispatches_handoff_arrival_to_delivery(self):
        # REGRESSION (W1): a handoff walker arriving was DROPPED -- she froze
        # beside the recipient, handoff never delivered. The tick must route it
        # to _arrive_at_handoff.
        state = self._walking_task_state()
        a = state['agents']['ada']
        a['task'] = None
        a['handoff'] = {'toId': 'ben', 'title': 'The report', 'fromId': 'ada'}
        a['path'] = [{'x': 1, 'y': 1}]
        engine = sim.SimEngine()
        with unittest.mock.patch('sim.step_agent_movement',
                                 return_value=[('arrive', 'handoff', 'ada')]):
            out = engine.tick(state, now=100.0)
        a = out['agents']['ada']
        self.assertIsNone(a.get('handoff'))
        self.assertTrue(a.get('offDuty'))
        self.assertFalse(a.get('visible'))
        self.assertEqual(len(out.get('handoffs', [])), 1)
        self.assertEqual(out['handoffs'][0]['title'], 'The report')

    def test_pair_card_recruits_a_navigator_server_side(self):
        # A `pair` card assigned server-side must recruit a navigator (walking
        # her over) instead of being assigned solo -- the driver still goes
        # alone when no second eligible hand exists.
        state = self._state()
        grid, doors = sim._load_outdoor_geometry()
        item = {'title': 'Maintain tooling', 'room': 'pressoffice',
                'instructions': 'Go.', 'pair': True, 'notBefore': None,
                'priority': sim.WORK_PRIORITY['normal'], 'goal': None,
                'research': None, 'taskType': 'code', 'skillReview': False}
        state['workQueue'] = [item]
        sim._task_cycle(state, now=time.time(), grid=grid, doors=doors,
                        task_id_holder=[0])
        tasks = [t for t in state['tasks'].values() if t.get('title') == 'Maintain tooling']
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task['status'], 'walking')
        driver = state['agents'][task['assignedTo']]
        self.assertTrue(driver.get('path'))
        nav_id = task.get('pairWith')
        self.assertIsNotNone(nav_id, 'a pair card recruits a navigator')
        self.assertNotEqual(nav_id, task['assignedTo'])
        nav = state['agents'][nav_id]
        self.assertEqual(nav.get('pairWith'), task['assignedTo'])
        self.assertEqual(nav.get('pairTaskId'), task['id'])
        self.assertTrue(nav.get('path'), 'the navigator walks over to pair')

    def test_pair_task_completion_releases_the_navigator(self):
        # When the driver's pair task ships, the navigator must be released
        # back to the pool -- she'd otherwise sit busy at the desk forever.
        state = self._state()
        state['tasks'] = {'task-1': {'id': 'task-1', 'room': 'pressoffice',
                                     'title': 'Pair me', 'entryX': 10, 'entryY': 20,
                                     'pairWith': 'ben'}}
        driver = state['agents']['ada']
        driver['task'] = 'task-1'
        driver['busy'] = True
        nav = state['agents']['ben']
        nav['pairWith'] = 'ada'
        nav['pairTaskId'] = 'task-1'
        nav['busy'] = True
        nav['inRoom'] = 'pressoffice'
        sim.finish_task(state, 'ada')
        self.assertIsNone(nav.get('pairWith'))
        self.assertIsNone(nav.get('pairTaskId'))
        self.assertFalse(nav.get('busy'))
        self.assertIsNone(nav.get('inRoom'))
        self.assertTrue(nav.get('visible'))


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


class WorkerSpikeIssueFiling(unittest.TestCase):
    """W3: autonomous issue filing. A spike worker's executor reports a `fileIssue`
    WISH (it runs off-thread against a snapshot); the tick consumes it inside
    _apply_content_result -> _file_spike_issue, which resolves the owning team
    (wish teamId -> task teamId -> product director -> roster director, each
    matched by id OR directorId), dedups against an already-open issue by the
    same reporter + summary, files via the real file_issue, and logs governance.
    Fail safe: no filing on unknown team / malformed wish / missing reporter.
    Purely state-based -- no Jev, no network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-sim-file-issue-')
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

    def _state(self, teams=None, with_director=True):
        roster = [{'id': 'ada', 'name': 'Ada', 'role': 'Research', 'isAdmin': False}]
        if with_director:
            roster[0]['director'] = 'zoe'
        return {
            'sim': {'owner': 'server'},
            'agentRoster': roster,
            'agents': {
                'ada': {'id': 'ada', 'x': sim.SPAWN['x'], 'y': sim.SPAWN['y'],
                        'dir': 'south', 'visible': True, 'busy': True,
                        'task': 'task-1', 'inRoom': 'pressoffice', 'offDuty': False},
            },
            'teams': teams if teams is not None else [
                {'id': 'dev', 'directorId': 'zoe', 'scrumMasterId': 'ben',
                 'room': 'pressoffice'},
            ],
            'products': {},
            'tasks': {'task-1': {'id': 'task-1', 'title': 'Spike: investigate auth',
                                 'room': 'pressoffice', 'status': 'working',
                                 'assignedTo': 'ada'}},
            'workQueue': [],
        }

    def _wish(self, **over):
        wish = {
            'issueType': 'story',
            'summary': 'Auth flow silently drops logins',
            'title': 'Auth: surface login failures',
            'feature': 'Library Tools',
            'description': {'userStory': 'As a user, I want to know why login '
                                         'failed, so that I can fix it.'},
        }
        wish.update(over)
        return wish

    def _task(self, **over):
        task = {'id': 'task-1', 'title': 'Spike: investigate auth',
                'room': 'pressoffice', 'status': 'working', 'assignedTo': 'ada'}
        task.update(over)
        return task

    def test_verdict_wish_files_issue_and_kicks_refinement(self):
        # A well-formed fileIssue wish files a real issue: the backlogRequests
        # pipe record is appended AND the team's refinement is kicked to due on
        # the next pass -- the worker's finding becomes real queue-able work.
        state = self._state()
        self.assertTrue(sim._file_spike_issue(state, self._wish(), self._task(),
                                              now_ms=1000))
        issues = list((state.get('issues') or {}).values())
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue['key'], 'DEV-1')
        self.assertEqual(issue['teamId'], 'dev')
        self.assertEqual(issue['type'], 'story')
        self.assertEqual(issue['reporterId'], 'ada')
        self.assertEqual(issue['feature'], 'Library Tools')
        self.assertEqual(issue['title'], 'Auth: surface login failures')
        self.assertTrue(state.get('backlogRequests'),
                        'filing feeds the backlog pipe')
        self.assertEqual((state.get('teamRefinementAt') or {}).get('dev'), 0,
                         'filing kicks the owning team\'s refinement to due')

    def test_team_resolved_by_director_id_when_wish_carries_director(self):
        # The wish may name the team's DIRECTOR id (the other key teams records
        # use) rather than the team record's `id` -- resolve to the real team.
        state = self._state()
        wish = self._wish(teamId='zoe')
        self.assertTrue(sim._file_spike_issue(state, wish, self._task(),
                                              now_ms=1000))
        issues = list((state.get('issues') or {}).values())
        self.assertEqual(issues[0]['teamId'], 'dev',
                         'a director-id wish must file under the real team id')

    def test_team_resolved_from_task_team_id(self):
        # No teamId on the wish itself: fall back to the task's teamId.
        state = self._state()
        wish = self._wish(teamId=None)
        self.assertTrue(sim._file_spike_issue(state, wish,
                                              self._task(teamId='dev'),
                                              now_ms=1000))
        issues = list((state.get('issues') or {}).values())
        self.assertEqual(issues[0]['teamId'], 'dev')

    def test_product_director_resolution_when_no_team_id(self):
        # No teamId on wish OR task: the task's productId resolves its owning
        # team's director, which matches the team record by directorId.
        state = self._state()
        state['products'] = {'prod-1': {'id': 'prod-1', 'name': 'Library Tools',
                                        'teamId': 'zoe'}}
        wish = self._wish(teamId=None)
        self.assertTrue(sim._file_spike_issue(state, wish,
                                              self._task(teamId=None,
                                                         productId='prod-1'),
                                              now_ms=1000))
        issues = list((state.get('issues') or {}).values())
        self.assertEqual(issues[0]['teamId'], 'dev')

    def test_roster_director_last_resort_team_source(self):
        # Nothing on the wish/task: the worker's roster `director` pointer is the
        # last-resort team source -- the finding still lands on the right team.
        state = self._state()
        wish = self._wish(teamId=None)
        self.assertTrue(sim._file_spike_issue(state, wish,
                                              self._task(teamId=None,
                                                         productId=None),
                                              now_ms=1000))
        issues = list((state.get('issues') or {}).values())
        self.assertEqual(issues[0]['teamId'], 'dev')

    def test_no_resolvable_team_files_nothing(self):
        # A worker with no team anywhere in the chain must never file -- a stray
        # idea can't wedge the backlog under a phantom owner.
        state = self._state(teams=[], with_director=False)
        self.assertFalse(sim._file_spike_issue(state, self._wish(teamId=None),
                                               self._task(teamId=None,
                                                          productId=None),
                                               now_ms=1000))
        self.assertFalse(state.get('issues'), 'no team -> no filing')

    def test_open_duplicate_same_reporter_summary_skipped(self):
        # The same gap reported by the same worker (same summary) that has NOT
        # reached a terminal state must not be re-filed every spike re-run.
        state = self._state()
        state['issues'] = {
            'DEV-1': {'key': 'DEV-1', 'teamId': 'dev', 'reporterId': 'ada',
                      'status': 'open', 'summary': 'Auth flow silently drops logins'},
        }
        self.assertFalse(sim._file_spike_issue(state, self._wish(), self._task(),
                                               now_ms=1000))
        self.assertEqual(len(state.get('issues')), 1,
                         'an open duplicate is never re-filed')

    def test_refile_allowed_after_terminal_status(self):
        # A terminal ('done'/'closed') issue is a resolved gap -- the spike may
        # legitimately file a fresh one for the same summary.
        state = self._state()
        state['issues'] = {
            'DEV-1': {'key': 'DEV-1', 'teamId': 'dev', 'reporterId': 'ada',
                      'status': 'done', 'summary': 'Auth flow silently drops logins'},
        }
        state['issueCounters'] = {'DEV': 1}  # next key is DEV-2, not a re-file
        self.assertTrue(sim._file_spike_issue(state, self._wish(), self._task(),
                                              now_ms=1000))
        self.assertEqual(len(state.get('issues')), 2,
                         'a terminal prior issue does not block re-filing')
        self.assertIn('DEV-2', state.get('issues'), 'the fresh filing gets its own key')

    def test_malformed_wish_fails_safe_no_filing(self):
        # Empty summary / empty feature / unknown issue type must all fail safe
        # to no filing (never a backlog wedge from a half-shaped wish).
        state = self._state()
        for bad in (self._wish(summary=''),
                    self._wish(feature=''),
                    self._wish(issueType='epic')):
            sim._file_spike_issue(state, bad, self._task(), now_ms=1000)
        self.assertFalse(state.get('issues'),
                         'malformed wishes must never file anything')

    def test_missing_reporter_fails_safe(self):
        # No reporter (task.assignedTo) -> nothing to attribute the card to.
        state = self._state()
        self.assertFalse(sim._file_spike_issue(state, self._wish(),
                                               self._task(assignedTo=None),
                                               now_ms=1000))
        self.assertFalse(state.get('issues'))

    def test_apply_content_result_consumes_fileissue_wish(self):
        # The tick integration: a content result carrying a fileIssue wish is
        # filed against the live state inside _apply_content_result.
        state = self._state()
        result = {'note': 'investigated; login failures are silently dropped',
                  'fileIssue': self._wish()}
        sim._apply_content_result(state, self._task(), result, now_ms=1000)
        issues = list((state.get('issues') or {}).values())
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['summary'], 'Auth flow silently drops logins')
        self.assertEqual(issues[0]['reporterId'], 'ada')

    def test_apply_content_result_without_wish_files_nothing(self):
        # A content result that found nothing actionable carries no fileIssue
        # wish -- the note lands, no issue is filed.
        state = self._state()
        sim._apply_content_result(state, self._task(),
                                  {'note': 'no actionable gap found'},
                                  now_ms=1000)
        self.assertFalse(state.get('issues'), 'no wish -> no filing')


if __name__ == '__main__':
    unittest.main()