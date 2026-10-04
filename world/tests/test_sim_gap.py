"""Coverage-push tests for world/sim.py residual branches: the movement
cluster (find_path edge cases, stalled-walker/interaction repairs, stranded
reconciliation), the tick/loop-pass error paths, and the task-cycle branches.
Uses the temp-DB isolation pattern from test_sim.py so the real think_tank.db
is never touched; serve network/DB seams are mocked at the module attribute.
"""
import builtins
import io
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


class SimIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-sim-gap-')
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
        sim._content_results.clear()

    @staticmethod
    def _free_grid(cols=8, rows=8):
        return {'cols': cols, 'rows': rows, 'cell': 8,
                'grid': [[0] * cols for _ in range(rows)]}

    @staticmethod
    def _blocked_grid(cols, rows, blocked):
        g = {'cols': cols, 'rows': rows, 'cell': 8,
             'grid': [[0] * cols for _ in range(rows)]}
        for (gx, gy) in blocked:
            g['grid'][gy][gx] = 1
        return g

    @staticmethod
    def _task_cycle_quiet(now_ms):
        quiet = [
            '_check_schedules', '_governance_pass', '_sweep_stuck_gates',
            '_reclaim_orphaned_walking_tasks', '_stale_work_step',
            '_coaching_loop_step', '_refinement_step', '_breakdown_step',
            '_retro_step', '_roadmap_step', '_rule_mine_step', '_escalation_step',
            '_block_step',
            '_mail_action_step', '_park_idle_wanderers',
            '_auto_close_completed_sprints', '_sprint_staffing_step',
            '_awake_idle_count', 'can_activate_another',
        ]
        return unittest.mock.patch.multiple(
            sim, **{name: unittest.mock.DEFAULT for name in quiet},
            _assign_due_item=unittest.mock.Mock(return_value=False))

    def _worker(self, aid, task_id, room='hangout'):
        return {'id': aid, 'x': 0, 'y': 0, 'task': task_id, 'busy': True,
                'inRoom': room, 'visible': True, 'offDuty': False}

    def _task_cycle_state(self, now_ms, agents, tasks, work_queue=()):
        return {
            'sim': {'owner': 'server'},
            'agents': agents,
            'tasks': tasks,
            'workQueue': list(work_queue),
            'lastHireAt': now_ms,
            'lastFiringReviewAt': now_ms,
        }

    def _run_cycle(self, state, now, grid=None, doors=None, holder=None):
        with unittest.mock.patch.object(sim, 'SPAWN', {'x': 100, 'y': 100}):
            return sim._task_cycle(state, now=now, grid=grid, doors=doors,
                                   task_id_holder=holder)


class MovementResidual(SimIsolation):
    def test_load_outdoor_geometry_missing_doors(self):
        tmp = tempfile.mkdtemp(prefix='sim-gap-geom-')
        try:
            with open(os.path.join(tmp, 'collision_grid.json'), 'w') as f:
                json.dump({'cols': 4, 'rows': 4, 'cell': 8,
                           'grid': [[0] * 4 for _ in range(4)]}, f)
            with unittest.mock.patch.object(sim, '_WORLD_DIR', tmp):
                grid, doors = sim._load_outdoor_geometry()
            self.assertIsNotNone(grid)
            self.assertIsNone(doors)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_find_path_start_out_of_bounds(self):
        grid = self._free_grid()
        agents = {}
        self.assertIsNone(sim.find_path(-50, 0, 100, 100, 'me', agents, grid))

    def test_find_path_start_equals_target_returns_single_waypoint(self):
        grid = self._free_grid()
        agents = {}
        path = sim.find_path(0, 0, 5, 5, 'me', agents, grid)
        self.assertEqual(len(path), 1)
        self.assertEqual(path[0], sim._cell_world_pos(0, 0, 8))

    def test_find_path_relaxes_blocked_target_to_nearby_free_cell(self):
        # Target cell (0,0) blocked; the ring search hits out-of-bounds cells
        # (line 244) before finding free cell (1,0).
        grid = self._blocked_grid(4, 4, [(0, 0)])
        agents = {}
        path = sim.find_path(30, 40, 0, 0, 'me', agents, grid)
        self.assertIsNotNone(path)
        self.assertEqual(path[-1], sim._cell_world_pos(0, 1, 8))

    def test_find_path_target_fully_blocked_returns_none(self):
        # 4x4 corner block: no free cell within the relax radius.
        blocked = [(gx, gy) for gy in range(4) for gx in range(4)]
        grid = self._blocked_grid(8, 8, blocked)
        agents = {}
        self.assertIsNone(sim.find_path(100, 100, 0, 0, 'me', agents, grid))

    def test_find_path_unreachable_returns_none(self):
        # A full-height wall at col 4 splits the grid.
        grid = self._blocked_grid(8, 8, [(4, gy) for gy in range(8)])
        agents = {}
        self.assertIsNone(sim.find_path(0, 0, 120, 120, 'me', agents, grid))

    def test_is_on_door_tile_no_doors(self):
        self.assertFalse(sim._is_on_door_tile(5, 5, None))

    def test_is_on_door_tile_overlap(self):
        doors = {'office': {'x': 0, 'y': 0, 'w': 10, 'h': 10}}
        self.assertTrue(sim._is_on_door_tile(5, 5, doors))
        self.assertFalse(sim._is_on_door_tile(50, 50, doors))

    def test_agent_blocked_at_skips_door_tile_agent(self):
        agents = {'ben': {'x': 5, 'y': 5, 'visible': True}}
        doors = {'office': {'x': 0, 'y': 0, 'w': 10, 'h': 10}}
        box = {'x': 100, 'y': 100, 'w': sim.AGENT_W, 'h': sim.AGENT_H}
        self.assertFalse(sim.agent_blocked_at(box, agents, doors=doors))

    def test_agent_blocked_at_overlap_returns_true(self):
        agents = {'ben': {'x': 5, 'y': 5, 'visible': True}}
        box = {'x': 0, 'y': 0, 'w': sim.AGENT_W, 'h': sim.AGENT_H}
        self.assertTrue(sim.agent_blocked_at(box, agents))

    def test_compute_reachable_mask_blocked_spawn(self):
        grid = self._blocked_grid(4, 4, [(0, 0), (1, 0)])
        mask = sim.compute_reachable_mask(grid, spawn={'x': 0, 'y': 0})
        self.assertFalse(any(any(row) for row in mask))

    def test_is_reachable_out_of_bounds(self):
        grid = self._free_grid(8, 8)
        mask = sim.compute_reachable_mask(grid, spawn={'x': 100, 'y': 100})
        self.assertTrue(sim.is_reachable(100, 100, grid, mask))
        self.assertFalse(sim.is_reachable(0, 1000, grid, mask))
        self.assertFalse(sim.is_reachable(1000, 0, grid, mask))

    def test_pick_free_spot_falls_back_to_spawn(self):
        grid = self._blocked_grid(4, 4, [(0, 0)])
        spot = sim.pick_free_spot(grid, rnd=lambda: 0.0)
        self.assertEqual(spot, {'x': sim.SPAWN['x'], 'y': sim.SPAWN['y']})

    def test_step_movement_arrive_handoff(self):
        agents = {'ada': {'id': 'ada', 'x': 0, 'y': 0, 'visible': True,
                          'path': [{'x': 0, 'y': 0}], 'pathIndex': 0,
                          'handoff': {'with': 'ben'}}}
        events = sim.step_agent_movement(1.0, agents, self._free_grid())
        self.assertEqual(events, [('arrive', 'handoff', 'ada')])

    def test_step_movement_arrive_pair(self):
        agents = {'ada': {'id': 'ada', 'x': 0, 'y': 0, 'visible': True,
                          'path': [{'x': 0, 'y': 0}], 'pathIndex': 0,
                          'pairWith': 'ben'}}
        events = sim.step_agent_movement(1.0, agents, self._free_grid())
        self.assertEqual(events, [('arrive', 'pair', 'ada')])

    def test_reconcile_stranded_empty_agents(self):
        self.assertEqual(sim._reconcile_stranded_agents({'agents': {}}, self._free_grid()), 0)
        self.assertEqual(sim._reconcile_stranded_agents({'agents': 'x'}, self._free_grid()), 0)

    def test_reconcile_stranded_skips_missing_coords(self):
        state = {'agents': {'ada': {'id': 'ada', 'visible': True, 'x': None, 'y': None}}}
        with unittest.mock.patch.object(sim, 'SPAWN', {'x': 100, 'y': 100}):
            self.assertEqual(sim._reconcile_stranded_agents(state, self._free_grid(8, 8)), 0)

    def test_reconcile_stranded_moves_unreachable_agent(self):
        grid, _ = sim._load_outdoor_geometry()
        grid['grid'] = [row[:] for row in grid['grid']]
        for gy in range(grid['rows']):
            grid['grid'][gy][43] = 1
        pos = sim._cell_world_pos(70, 30, grid['cell'])
        state = {'agents': {'ada': {'id': 'ada', 'x': pos['x'], 'y': pos['y'], 'visible': True}}}
        moved = sim._reconcile_stranded_agents(state, grid)
        self.assertEqual(moved, 1)

    def test_repair_stalled_walkers_skips_non_dict(self):
        state = {'agents': {'ada': 'not-a-dict'}, 'tasks': {}}
        sim._repair_stalled_walkers(state, self._free_grid(), {})
        self.assertEqual(state['agents']['ada'], 'not-a-dict')

    def test_repair_stalled_walkers_no_door(self):
        state = {'agents': {'ada': {'id': 'ada', 'x': 10, 'y': 10, 'task': 'task-1',
                                    'replanCount': 0, 'path': []}},
                 'tasks': {'task-1': {'id': 'task-1', 'status': 'walking', 'room': 'ghostroom'}}}
        sim._repair_stalled_walkers(state, self._free_grid(), {})
        self.assertTrue(state['agents']['ada']['visible'])

    def test_release_stalled_interaction_non_dict(self):
        self.assertFalse(sim._release_stalled_interaction({'agents': {'ada': None}}, 'ada'))

    def test_release_pair_navigator_non_dict(self):
        self.assertFalse(sim._release_pair_navigator({'agents': {'ada': None}}, 'ada', 'task-1'))

    def test_release_pair_navigator_wrong_task(self):
        state = {'agents': {'ada': {'id': 'ada', 'pairTaskId': 'task-2'}}}
        self.assertFalse(sim._release_pair_navigator(state, 'ada', 'task-1'))

    def test_repair_stalled_interactions_releases_handoff(self):
        state = {'agents': {'ada': {'id': 'ada', 'visible': True, 'path': [],
                                    'handoff': {'with': 'ben'}}}}
        released = sim._repair_stalled_interactions(state)
        self.assertEqual(released, 1)
        self.assertTrue(state['agents']['ada']['offDuty'])

    def test_reclaim_orphaned_log_action_raises(self):
        state = {'agents': {},
                 'tasks': {'task-1': {'id': 'task-1', 'status': 'walking',
                                      'assignedTo': 'ada', 'attempts': 3, 'title': 't'}}}
        with unittest.mock.patch('serve.log_action', side_effect=RuntimeError('boom')):
            reclaimed = sim._reclaim_orphaned_walking_tasks(state)
        self.assertEqual(reclaimed, 1)
        self.assertNotIn('task-1', state['tasks'])

    def test_tick_non_dict_returns_input(self):
        engine = sim.SimEngine()
        self.assertEqual(engine.tick('not-a-dict'), 'not-a-dict')

    def test_tick_snapshot_skips_non_dict_agent(self):
        engine = sim.SimEngine()
        with unittest.mock.patch('serve._heal_agent_identity'):
            out = engine.tick({'agents': {'ada': 'not-a-dict'}}, now=1.0)
        self.assertEqual(out['sim']['agents'], {})

    def test_sim_loop_pass_apply_ask_raises(self):
        serve.save_state_to_db({'sim': {'tick': 0}})
        with unittest.mock.patch('serve._apply_pending_ask_results', side_effect=RuntimeError('ask')), \
             unittest.mock.patch('serve._peer_review_tick'):
            state = sim._sim_loop_pass()
        self.assertIsNotNone(state)

    def test_sim_loop_pass_peer_review_raises(self):
        serve.save_state_to_db({'sim': {'tick': 0}})
        with unittest.mock.patch('serve._apply_pending_ask_results'), \
             unittest.mock.patch('serve._peer_review_tick', side_effect=RuntimeError('peer')):
            state = sim._sim_loop_pass()
        self.assertIsNotNone(state)

    def test_drain_emails_import_fails(self):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == 'serve':
                raise ImportError('nope')
            return real_import(name, *args, **kwargs)

        with unittest.mock.patch('builtins.__import__', side_effect=fake_import):
            self.assertEqual(sim._drain_emails_from_db(), [])


class TaskCycle(SimIsolation):
    def test_owner_not_server_returns_unchanged(self):
        state = {'sim': {'owner': 'client'}, 'agents': {}, 'workQueue': []}
        out = sim._task_cycle(state, now=1000.0)
        self.assertIs(out, state)

    def test_no_work_returns_after_idle_gate(self):
        now_ms = int(time.time() * 1000)
        state = self._task_cycle_state(
            now_ms, {}, {},
            work_queue=[{'title': 'x', 'room': 'hangout', 'notBefore': now_ms + 999999}])
        with self._task_cycle_quiet(now_ms):
            out = self._run_cycle(state, now_ms / 1000.0)
        self.assertIs(out, state)

    def test_timeout_completion_loads_geometry_and_finishes(self):
        now_ms = int(time.time() * 1000)
        now = now_ms / 1000.0
        state = self._task_cycle_state(
            now_ms,
            {'ada': self._worker('ada', 'task-1')},
            {'task-1': {'id': 'task-1', 'title': 't', 'room': 'hangout',
                        'status': 'working', 'workUntil': now - 1,
                        'assignedTo': 'ada'}})
        with self._task_cycle_quiet(now_ms):
            out = self._run_cycle(state, now, grid=None, doors=None, holder=None)
        self.assertEqual(out['tasks']['task-1']['status'], 'done')
        self.assertTrue(out['agents']['ada']['offDuty'])

    def test_completion_loop_skips_non_dict_agent(self):
        now_ms = int(time.time() * 1000)
        now = now_ms / 1000.0
        state = self._task_cycle_state(
            now_ms, {'ada': 'not-a-dict'}, {},
            work_queue=[{'title': 'x', 'room': 'hangout', 'notBefore': now_ms - 1}])
        with self._task_cycle_quiet(now_ms):
            self._run_cycle(state, now)
        self.assertEqual(state['agents']['ada'], 'not-a-dict')

    def test_shadow_task_with_content_result_completes_shadow(self):
        now_ms = int(time.time() * 1000)
        now = now_ms / 1000.0
        state = self._task_cycle_state(
            now_ms,
            {'ada': self._worker('ada', 'task-1', 'observatory')},
            {'task-1': {'id': 'task-1', 'title': 't', 'room': 'observatory',
                        'status': 'working', 'workUntil': now + 1000,
                        'assignedTo': 'ada', '_contentInFlight': True,
                        'shadow': True}})
        sim._content_results['task-1'] = {'note': 'done', 'ok': True}
        with self._task_cycle_quiet(now_ms):
            self._run_cycle(state, now, grid=self._free_grid(), doors={},
                            holder=sim._TASK_ID_HOLDER)
        self.assertEqual(state['tasks']['task-1']['status'], 'done')
        self.assertTrue(state['agents']['ada']['offDuty'])
        self.assertEqual(len(state.get('shadowLedger') or []), 1)

    def test_review_subtask_vote_closes_parent(self):
        now_ms = int(time.time() * 1000)
        now = now_ms / 1000.0
        state = self._task_cycle_state(
            now_ms,
            {'ada': self._worker('ada', 'task-2', 'observatory'),
             'ben': {'id': 'ben', 'x': 0, 'y': 0, 'visible': True}},
            {
                'task-1': {'id': 'task-1', 'title': 'parent', 'room': 'observatory',
                           'status': 'needs_review', 'assignedTo': 'ben',
                           '_peerGate': {'approvals': 1, 'approvers': ['ben'],
                                         'reviewerIds': ['ada', 'ben'], 'enteredMs': 0}},
                'task-2': {'id': 'task-2', 'title': 'review', 'room': 'observatory',
                           'status': 'working', 'workUntil': now + 1000,
                           'assignedTo': 'ada', 'reviewOf': 'task-1',
                           'taskType': 'review', '_contentInFlight': True},
            })
        sim._content_results['task-2'] = {'note': 'clean', 'ok': True,
                                          'peerVerdict': 'clean', 'pipelineOk': True}
        with self._task_cycle_quiet(now_ms):
            self._run_cycle(state, now, grid=self._free_grid(), doors={},
                            holder=sim._TASK_ID_HOLDER)
        self.assertEqual(state['tasks']['task-1']['status'], 'done')

    def test_fix_subtask_rearms_gate(self):
        now_ms = int(time.time() * 1000)
        now = now_ms / 1000.0
        state = self._task_cycle_state(
            now_ms,
            {'ada': self._worker('ada', 'task-2', 'observatory'),
             'ben': {'id': 'ben', 'x': 0, 'y': 0, 'visible': True}},
            {
                'task-1': {'id': 'task-1', 'title': 'parent', 'room': 'observatory',
                           'status': 'failed', 'assignedTo': 'ben',
                           '_peerGate': {'approvals': 0, 'approvers': [],
                                         'reviewerIds': ['ada', 'ben'], 'enteredMs': 0,
                                         'escalated': False}},
                'task-2': {'id': 'task-2', 'title': 'fix', 'room': 'observatory',
                           'status': 'working', 'workUntil': now + 1000,
                           'assignedTo': 'ada', 'reviewOf': 'task-1',
                           'taskType': 'fix', '_contentInFlight': True},
            })
        sim._content_results['task-2'] = {'note': 'fixed', 'ok': True,
                                          'peerVerdict': 'clean', 'pipelineOk': True}
        with self._task_cycle_quiet(now_ms):
            self._run_cycle(state, now, grid=self._free_grid(), doors={},
                            holder=sim._TASK_ID_HOLDER)
        self.assertEqual(state['tasks']['task-1']['status'], 'needs_review')

    def test_fix_subtask_clears_stale_gate_on_ungated_parent(self):
        now_ms = int(time.time() * 1000)
        now = now_ms / 1000.0
        state = self._task_cycle_state(
            now_ms,
            {'ada': self._worker('ada', 'task-2', 'hangout')},
            {
                'task-1': {'id': 'task-1', 'title': 'parent', 'room': 'hangout',
                           'status': 'needs_review', 'assignedTo': 'ada',
                           '_peerGate': {'approvals': 0, 'approvers': [],
                                         'reviewerIds': ['ada'], 'enteredMs': 0}},
                'task-2': {'id': 'task-2', 'title': 'fix', 'room': 'hangout',
                           'status': 'working', 'workUntil': now + 1000,
                           'assignedTo': 'ada', 'reviewOf': 'task-1',
                           'taskType': 'fix', '_contentInFlight': True},
            })
        sim._content_results['task-2'] = {'note': 'fixed', 'ok': True,
                                          'peerVerdict': 'clean', 'pipelineOk': True}
        with self._task_cycle_quiet(now_ms):
            self._run_cycle(state, now, grid=self._free_grid(), doors={},
                            holder=sim._TASK_ID_HOLDER)
        self.assertIsNone(state['tasks']['task-1'].get('_peerGate'))
        self.assertEqual(state['tasks']['task-1']['status'], 'working')

    def test_gated_content_result_no_reviewers_finishes(self):
        now_ms = int(time.time() * 1000)
        now = now_ms / 1000.0
        state = self._task_cycle_state(
            now_ms,
            {'ada': self._worker('ada', 'task-1', 'observatory')},
            {'task-1': {'id': 'task-1', 'title': 't', 'room': 'observatory',
                        'status': 'working', 'workUntil': now + 1000,
                        'assignedTo': 'ada', '_contentInFlight': True}})
        sim._content_results['task-1'] = {'note': 'done', 'ok': True}
        with self._task_cycle_quiet(now_ms):
            self._run_cycle(state, now, grid=self._free_grid(), doors={},
                            holder=sim._TASK_ID_HOLDER)
        self.assertEqual(state['tasks']['task-1']['status'], 'done')

    def test_timeout_gated_no_reviewers_finishes(self):
        now_ms = int(time.time() * 1000)
        now = now_ms / 1000.0
        state = self._task_cycle_state(
            now_ms,
            {'ada': self._worker('ada', 'task-1', 'observatory')},
            {'task-1': {'id': 'task-1', 'title': 't', 'room': 'observatory',
                        'status': 'working', 'workUntil': now - 1,
                        'assignedTo': 'ada'}})
        with self._task_cycle_quiet(now_ms):
            self._run_cycle(state, now, grid=self._free_grid(), doors={},
                            holder=sim._TASK_ID_HOLDER)
        self.assertEqual(state['tasks']['task-1']['status'], 'done')


class TaskLifecycle(SimIsolation):
    def test_finish_task_missing_agent(self):
        sim.finish_task({'agents': {}}, 'ada', self._free_grid())

    def test_finish_task_steps_aside_at_entry(self):
        state = {'agents': {
            'ada': {'id': 'ada', 'x': 0, 'y': 0, 'task': 'task-1', 'busy': True,
                    'inRoom': 'observatory', 'visible': True,
                    'approvedCount': 0, 'weekApprovals': 0},
            'ben': {'id': 'ben', 'x': 100, 'y': 100, 'visible': True},
        }, 'tasks': {'task-1': {'id': 'task-1', 'title': 't', 'room': 'observatory',
                                'status': 'working', 'entryX': 100, 'entryY': 100,
                                'assignedTo': 'ada'}}}
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch.object(sim, '_revoke_task_access'), \
             unittest.mock.patch.object(sim, '_note_completed_room'), \
             unittest.mock.patch.object(sim, '_maybe_file_followup'), \
             unittest.mock.patch.object(sim, '_auto_clear_dependency_blocks'):
            sim.finish_task(state, 'ada', self._free_grid())
        self.assertEqual(state['agents']['ada']['x'], 100 + sim.AGENT_W + 8)

    def test_send_agent_off_duty_missing_agent(self):
        sim.send_agent_off_duty({'agents': {}}, 'ada', {}, self._free_grid())

    def test_complete_shadow_task_log_raises(self):
        state = {'agents': {'ada': {'id': 'ada', 'task': 'task-1', 'busy': True,
                                    'inRoom': 'observatory', 'visible': True}},
                 'tasks': {'task-1': {'id': 'task-1', 'title': 't'}}}
        task = state['tasks']['task-1']
        with unittest.mock.patch('serve.log_action', side_effect=RuntimeError('boom')):
            entry = sim._complete_shadow_task(state, 'ada', task, 1234, self._free_grid())
        self.assertEqual(task['status'], 'done')
        self.assertIn(entry, state['shadowLedger'])

    def test_arrive_at_task_missing_agent(self):
        sim._arrive_at_task({'agents': {}}, 'ada', now=1.0)

    def test_arrive_at_pair_missing_agent(self):
        self.assertFalse(sim._arrive_at_pair({'agents': {}}, 'ada', now=1.0))

    def test_arrive_at_pair_log_raises(self):
        state = {'agents': {
            'ada': {'id': 'ada', 'pairWith': 'ben', 'pairTaskId': 'task-1'},
            'ben': {'id': 'ben', 'roomX': 10, 'roomY': 20},
        }, 'tasks': {'task-1': {'id': 'task-1', 'room': 'observatory'}}}
        with unittest.mock.patch('serve.log_action', side_effect=RuntimeError('boom')):
            ok = sim._arrive_at_pair(state, 'ada', now=1.0)
        self.assertTrue(ok)
        self.assertTrue(state['agents']['ada']['busy'])

    def test_arrive_at_handoff_missing_agent(self):
        self.assertFalse(sim._arrive_at_handoff({'agents': {}}, 'ada', now=1.0))

    def test_arrive_at_handoff_log_raises(self):
        state = {'agents': {
            'ada': {'id': 'ada', 'handoff': {'toId': 'ben', 'title': 't'}},
            'ben': {'id': 'ben'},
        }}
        with unittest.mock.patch('serve.log_action', side_effect=RuntimeError('boom')):
            ok = sim._arrive_at_handoff(state, 'ada', now=1.0)
        self.assertTrue(ok)
        self.assertEqual(len(state['handoffs']), 1)

    def test_revoke_task_access_no_task_id(self):
        sim._revoke_task_access(None)

    def test_revoke_task_access_raises(self):
        with unittest.mock.patch('serve.revoke_task_access', side_effect=RuntimeError('db')):
            sim._revoke_task_access('task-1')


class StuckGate(SimIsolation):
    def test_sweep_stuck_gates_reenter_log_raises(self):
        now_ms = 2000000
        state = {
            'sim': {'owner': 'server'},
            'agents': {'ada': {'id': 'ada', 'x': 0, 'y': 0, 'visible': True,
                               'offDuty': False},
                       'ben': {'id': 'ben', 'x': 0, 'y': 0, 'visible': True,
                               'offDuty': False}},
            'tasks': {'task-1': {'id': 'task-1', 'title': 't', 'room': 'observatory',
                                 'status': 'needs_review', 'assignedTo': 'ada',
                                 '_peerGate': {'approvals': 0, 'approvers': [],
                                               'reviewerIds': ['ada', 'ben'],
                                               'enteredMs': 0}}},
            'workQueue': [],
        }
        with unittest.mock.patch('serve.log_action', side_effect=RuntimeError('boom')):
            sim._sweep_stuck_gates(state, now_ms)
        self.assertEqual(len(state['workQueue']), 2)
        self.assertEqual(state['tasks']['task-1']['_peerGate']['stuckRescueTs'], now_ms)

    def test_sweep_stuck_gates_widens_pair(self):
        now_ms = 2000000
        state = {
            'sim': {'owner': 'server'},
            'agents': {
                'ada': {'id': 'ada', 'x': 0, 'y': 0, 'visible': True, 'offDuty': False},
                'dan': {'id': 'dan', 'x': 0, 'y': 0, 'visible': True, 'offDuty': False},
            },
            'agentRoster': [{'id': 'ada', 'name': 'Ada'}, {'id': 'dan', 'name': 'Dan'}],
            'tasks': {'task-1': {'id': 'task-1', 'title': 't', 'room': 'observatory',
                                 'status': 'needs_review', 'assignedTo': 'ada',
                                 '_peerGate': {'approvals': 0, 'approvers': [],
                                               'reviewerIds': ['eve', 'cat'],
                                               'enteredMs': 0}}},
            'workQueue': [],
        }
        sim._sweep_stuck_gates(state, now_ms)
        self.assertEqual(state['tasks']['task-1']['_peerGate']['reviewerIds'], ['dan'])

    def test_sweep_stuck_gates_widen_log_raises(self):
        now_ms = 2000000
        state = {
            'sim': {'owner': 'server'},
            'agents': {
                'ada': {'id': 'ada', 'x': 0, 'y': 0, 'visible': True, 'offDuty': False},
                'dan': {'id': 'dan', 'x': 0, 'y': 0, 'visible': True, 'offDuty': False},
            },
            'agentRoster': [{'id': 'ada', 'name': 'Ada'}, {'id': 'dan', 'name': 'Dan'}],
            'tasks': {'task-1': {'id': 'task-1', 'title': 't', 'room': 'observatory',
                                 'status': 'needs_review', 'assignedTo': 'ada',
                                 '_peerGate': {'approvals': 0, 'approvers': [],
                                               'reviewerIds': ['eve', 'cat'],
                                               'enteredMs': 0}}},
            'workQueue': [{'reviewOf': 'task-1', 'assignedTo': 'old', 'title': 'Review: t'}],
        }
        with unittest.mock.patch('serve.log_action', side_effect=RuntimeError('boom')):
            sim._sweep_stuck_gates(state, now_ms)
        self.assertEqual(state['workQueue'][0]['assignedTo'], 'dan')

    def test_release_agent_gated_missing_agent(self):
        sim._release_agent_gated({'agents': {}}, 'ada', self._free_grid())

    def test_release_agent_after_failure_missing_agent(self):
        sim._release_agent_after_failure({'agents': {}}, 'ada', None)

    def test_gate_reviewer_reachable_no_rid(self):
        self.assertFalse(sim._gate_reviewer_reachable({}, None, {}, 1))

    def test_gate_reviewer_reachable_via_queued_review(self):
        state = {'agents': {'ada': {'id': 'ada', 'busy': True, 'task': 'other'}},
                 'workQueue': [{'reviewOf': 'task-1', 'assignedTo': 'ada'}]}
        self.assertTrue(sim._gate_reviewer_reachable(state, 'ada', {}, 1))

    def test_reenter_gate_review_no_reviewers(self):
        state = {'agents': {}, 'workQueue': []}
        self.assertEqual(sim._reenter_gate_review(state, {'id': 'task-1'}, ['ghost']), [])

    def test_close_gated_story_log_raises(self):
        state = {'agents': {}}
        parent = {'id': 'task-1', 'title': 't',
                  '_peerGate': {'approvals': 1, 'approvers': ['ben']}}
        with unittest.mock.patch('serve.log_action', side_effect=RuntimeError('boom')), \
             unittest.mock.patch.object(sim, '_revoke_task_access'), \
             unittest.mock.patch.object(sim, '_queue_player_email'):
            ok = sim._close_gated_story(state, parent)
        self.assertTrue(ok)
        self.assertEqual(parent['status'], 'done')

    def test_parent_close_from_vote_no_gate(self):
        self.assertFalse(sim._parent_close_from_vote({'agents': {}}, {'id': 't'}, 1))

    def test_team_member_candidates_skips_missing_agent(self):
        state = {'agents': {'ada': {'id': 'ada'}}, 'agentRoster': []}
        out = sim._team_member_candidates(state, 'd1', ['ada', 'ghost'], 1)
        self.assertEqual(len(out), 1)

    def test_borrow_skips_loaned_agent(self):
        state = {'agents': {'ben': {'id': 'ben', 'offDuty': True}},
                 'agentRoster': [{'id': 'ben', 'name': 'Ben', 'director': 'd1',
                                  'loan': {'teamId': 'x'}}],
                 'teams': []}
        self.assertIsNone(sim._borrow_inactive_agent_for_team(state, 'd2'))

    def test_borrow_skips_same_director(self):
        state = {'agents': {'ben': {'id': 'ben', 'offDuty': True}},
                 'agentRoster': [{'id': 'ben', 'name': 'Ben', 'director': 'd2'}],
                 'teams': []}
        self.assertIsNone(sim._borrow_inactive_agent_for_team(state, 'd2'))

    def test_maybe_escalate_stuck_gate_create_raises(self):
        gate = {'cycleCount': sim.MAX_REVIEW_CYCLES - 1, 'escalated': False}
        with unittest.mock.patch('serve.create_escalation', side_effect=RuntimeError('boom')):
            escalated = sim._maybe_escalate_stuck_gate({'title': 't'}, gate, 'reason')
        self.assertTrue(escalated)
        self.assertTrue(gate['escalated'])


class ContentResult(SimIsolation):
    def test_apply_content_result_library_path_and_notes(self):
        state = {'agents': {'ada': {'id': 'ada', 'profile': {'notes': ['1', '2', '3', '4', '5', '6']}}},
                 'researchTopics': []}
        task = {'id': 'task-1', 'assignedTo': 'ada', 'research': {}}
        result = {'note': 'n', 'libraryPath': '/tmp/x.md', 'seenUrls': ['a']}
        sim._apply_content_result(state, task, result, now_ms=1)
        self.assertEqual(task['libraryPath'], '/tmp/x.md')
        self.assertEqual(len(state['agents']['ada']['profile']['notes']), 5)

    def test_apply_content_result_queue_fix(self):
        state = {'agents': {}, 'researchTopics': []}
        task = {'id': 'task-1', 'assignedTo': 'ada'}
        result = {'queueFix': {'title': 'fix', 'room': 'observatory'}}
        sim._apply_content_result(state, task, result, now_ms=1)
        self.assertEqual(len(state['workQueue']), 1)

    def test_apply_content_result_notify_player(self):
        state = {'agents': {}, 'researchTopics': []}
        task = {'id': 'task-1', 'assignedTo': 'ada'}
        result = {'notifyPlayer': {'kind': 'story_done', 'subject': 's', 'body': 'b'}}
        sim._apply_content_result(state, task, result, now_ms=1)
        self.assertTrue(state.get('emailOutbox'))

    def test_file_spike_issue_file_issue_returns_none(self):
        state = {'agents': {'ada': {'id': 'ada'}},
                 'agentRoster': [{'id': 'ada', 'director': 'd1'}],
                 'teams': [{'id': 'team-1', 'directorId': 'd1'}],
                 'issues': {}}
        wish = {'issueType': 'story', 'summary': 's', 'feature': 'f'}
        task = {'assignedTo': 'ada'}
        with unittest.mock.patch.object(sim, 'file_issue', return_value=None):
            self.assertFalse(sim._file_spike_issue(state, wish, task, 1))


class HiringCluster(SimIsolation):
    def _hire_state(self):
        return {
            'agentRoster': [
                {'id': 'admin', 'name': 'Admin', 'isAdmin': True},
                {'id': 'd1', 'name': 'Dana'},
                {'id': 'm1', 'name': 'Mia', 'director': 'd1'},
            ],
            'agents': {
                'admin': {'id': 'admin', 'name': 'Admin'},
                'd1': {'id': 'd1', 'name': 'Dana', 'busy': False, 'offDuty': False},
                'm1': {'id': 'm1', 'name': 'Mia', 'busy': False, 'offDuty': False},
            },
        }

    def test_start_auto_hire_success(self):
        state = self._hire_state()
        ok = sim._start_auto_hire(state, now_ms=1_000_000, grid=self._free_grid(),
                                  decider=lambda s, i, c: 'm1')
        self.assertTrue(ok)
        self.assertEqual(state['_pendingHire']['helpForId'], 'm1')
        self.assertEqual(state['_pendingHire']['directorId'], 'd1')
        self.assertTrue(state['agents']['d1']['busy'])

    def test_start_auto_hire_skips_director_with_no_live_members(self):
        state = {
            'agentRoster': [
                {'id': 'admin', 'name': 'Admin', 'isAdmin': True},
                {'id': 'd1', 'name': 'Dana'},
                {'id': 'ghost', 'name': 'Ghost', 'director': 'd1'},
            ],
            'agents': {
                'admin': {'id': 'admin', 'name': 'Admin'},
                'd1': {'id': 'd1', 'name': 'Dana', 'busy': False, 'offDuty': False},
            },
        }
        ok = sim._start_auto_hire(state, now_ms=1_000_000, grid=self._free_grid(),
                                  decider=lambda s, i, c: None)
        self.assertFalse(ok)
        self.assertEqual(state['lastHireAt'], 0)

    def test_start_auto_hire_decider_none_and_no_fallback(self):
        state = self._hire_state()

        def decider(s, instructions, candidates):
            s['agents'].pop('d1', None)
            s['agents'].pop('m1', None)
            return None

        ok = sim._start_auto_hire(state, now_ms=1_000_000, grid=self._free_grid(),
                                  decider=decider)
        self.assertFalse(ok)
        self.assertEqual(state['lastHireAt'], 0)

    def test_start_auto_hire_director_missing_at_pick(self):
        state = self._hire_state()

        def decider(s, instructions, candidates):
            s['agents'].pop('d1', None)
            return 'm1'

        ok = sim._start_auto_hire(state, now_ms=1_000_000, grid=self._free_grid(),
                                  decider=decider)
        self.assertFalse(ok)
        self.assertEqual(state['lastHireAt'], 0)

    def test_complete_auto_hire_creates_agent(self):
        state = {
            'agentRoster': [
                {'id': 'admin', 'name': 'Admin', 'isAdmin': True},
                {'id': 'd1', 'name': 'Dana'},
                {'id': 'm1', 'name': 'Mia', 'director': 'd1'},
            ],
            'agents': {
                'admin': {'id': 'admin', 'name': 'Admin'},
                'd1': {'id': 'd1', 'name': 'Dana', 'busy': True, 'visible': False,
                       'inRoom': 'commandcenter'},
                'm1': {'id': 'm1', 'name': 'Mia', 'role': 'Engineer'},
            },
        }
        pending = {
            'adminId': 'd1', 'adminName': 'Dana', 'directorId': 'd1',
            'helpForId': 'm1', 'helpForName': 'Mia', 'at': 1_000_000,
        }
        with unittest.mock.patch.object(sim, '_hire_name_chooser', return_value='maya'), \
             unittest.mock.patch.object(sim, '_free_outdoor_spot',
                                        return_value={'x': 10, 'y': 10}):
            new_id = sim._complete_auto_hire(state, pending, grid=self._free_grid(),
                                             now_ms=1_000_000)
        self.assertEqual(new_id, 'maya')
        self.assertIn('maya', state['agents'])
        self.assertNotIn('_pendingHire', state)
        self.assertEqual(state['_pendingOnboard']['coworkerIds'], ['m1'])

    def test_complete_auto_hire_appends_external_helpfor(self):
        state = {
            'agentRoster': [
                {'id': 'admin', 'name': 'Admin', 'isAdmin': True},
                {'id': 'd1', 'name': 'Dana'},
                {'id': 'm1', 'name': 'Mia', 'director': 'd1'},
                {'id': 'x1', 'name': 'Xia', 'director': 'd2'},
            ],
            'agents': {
                'd1': {'id': 'd1', 'name': 'Dana'},
                'm1': {'id': 'm1', 'name': 'Mia'},
                'x1': {'id': 'x1', 'name': 'Xia'},
            },
        }
        pending = {
            'adminId': 'd1', 'adminName': 'Dana', 'directorId': 'd1',
            'helpForId': 'x1', 'helpForName': 'Xia', 'at': 1_000_000,
        }
        with unittest.mock.patch.object(sim, '_hire_name_chooser', return_value='maya'), \
             unittest.mock.patch.object(sim, '_free_outdoor_spot',
                                        return_value={'x': 10, 'y': 10}):
            new_id = sim._complete_auto_hire(state, pending, grid=self._free_grid(),
                                             now_ms=1_000_000)
        self.assertEqual(new_id, 'maya')
        self.assertIn('x1', state['_pendingOnboard']['coworkerIds'])

    def test_complete_auto_hire_no_name(self):
        state = {
            'agentRoster': [{'id': 'd1', 'name': 'Dana'}],
            'agents': {'d1': {'id': 'd1', 'name': 'Dana'}},
            '_usedNames': ['maya', 'leo', 'zara', 'owen', 'lyra', 'ida', 'vela'],
        }
        pending = {
            'adminId': 'd1', 'adminName': 'Dana', 'directorId': 'd1',
            'helpForId': 'm1', 'helpForName': 'Mia', 'at': 1_000_000,
        }
        with unittest.mock.patch.object(sim, '_hire_name_chooser', return_value=None):
            new_id = sim._complete_auto_hire(state, pending, grid=self._free_grid(),
                                             now_ms=1_000_000)
        self.assertIsNone(new_id)
        self.assertNotIn('_pendingHire', state)

    def test_complete_auto_hire_team_at_cap(self):
        state = {
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana'},
                *[{'id': f'm{i}', 'name': f'M{i}', 'director': 'd1'} for i in range(6)],
            ],
            'agents': {'d1': {'id': 'd1', 'name': 'Dana'}},
        }
        pending = {
            'adminId': 'd1', 'adminName': 'Dana', 'directorId': 'd1',
            'helpForId': 'm0', 'helpForName': 'M0', 'at': 1_000_000,
        }
        with unittest.mock.patch.object(sim, '_hire_name_chooser', return_value='maya'):
            new_id = sim._complete_auto_hire(state, pending, grid=self._free_grid(),
                                             now_ms=1_000_000)
        self.assertIsNone(new_id)
        self.assertNotIn('_pendingHire', state)

    def test_next_hire_name_exhausted(self):
        state = {'_usedNames': ['maya', 'leo', 'zara', 'owen', 'lyra', 'ida', 'vela']}
        self.assertIsNone(sim._next_hire_name(state))

    def test_remember_name_blank(self):
        state = {}
        sim._remember_name(state, '   ')
        self.assertNotIn('_usedNames', state)

    def test_spawn_team_at_cap(self):
        state = {'agentRoster': [{'id': f'a{i}', 'name': f'A{i}'}
                                 for i in range(sim.MAX_TOTAL_AGENTS)]}
        self.assertIsNone(sim.spawn_new_team_for_request(state, 'goal', now_ms=1_000_000))

    def test_spawn_team_no_admin(self):
        state = {'agentRoster': [{'id': 'd1', 'name': 'Dana'}]}
        self.assertIsNone(sim.spawn_new_team_for_request(state, 'goal', now_ms=1_000_000,
                                                         admin_id=None))

    def test_spawn_team_no_director_name(self):
        state = {
            'agentRoster': [{'id': 'admin', 'name': 'Admin', 'isAdmin': True}],
            '_usedNames': list(sim._NEW_TEAM_NAME_POOL),
        }
        with unittest.mock.patch.object(sim, '_hire_name_chooser', return_value=None):
            self.assertIsNone(sim.spawn_new_team_for_request(state, 'goal', now_ms=1_000_000))

    def test_spawn_team_employee_pool_exhausted(self):
        state = {
            'agentRoster': [{'id': 'admin', 'name': 'Admin', 'isAdmin': True}],
            '_usedNames': list(sim._NEW_TEAM_NAME_POOL),
        }

        def chooser(s, name, used, role):
            return 'zed' if role == 'Director' else None

        team = sim.spawn_new_team_for_request(state, 'goal', now_ms=1_000_000, chooser=chooser)
        self.assertIsNotNone(team)
        self.assertEqual(team['members'], [])


class OnboardCeremony(SimIsolation):
    def test_onboard_coworker_defs_skips_missing(self):
        state = {
            'agents': {'m1': {'id': 'm1', 'name': 'Mia'}},
            'agentRoster': [{'id': 'm1', 'name': 'Mia'}, {'id': 'ghost', 'name': 'Ghost'}],
        }
        onboard = {'coworkerIds': ['ghost', 'm1']}
        out = sim._onboard_coworker_defs(state, onboard)
        self.assertEqual([c['id'] for c in out], ['m1'])

    def test_start_onboard_meeting_director_busy(self):
        state = {
            'agents': {'d1': {'id': 'd1', 'busy': True, 'offDuty': False}},
            'agentRoster': [],
        }
        onboard = {'directorId': 'd1', 'agentId': 'maya', 'coworkerIds': []}
        self.assertFalse(sim._start_onboard_meeting(state, onboard, now_ms=1_000_000))

    def test_onboard_hold_claimed_task_no_agent(self):
        state = {'agents': {}, 'tasks': {}}
        onboard = {'agentId': 'maya'}
        self.assertFalse(sim._onboard_hold_claimed_task(state, onboard))

    def test_onboard_hold_claimed_task_handoff(self):
        state = {
            'agents': {'maya': {'id': 'maya', 'task': 't1', 'handoff': {'to': 'm1'}}},
            'tasks': {'t1': {'id': 't1', 'status': 'walking'}},
        }
        onboard = {'agentId': 'maya'}
        self.assertFalse(sim._onboard_hold_claimed_task(state, onboard))

    def test_work_agreement_text_non_dict(self):
        self.assertIsNone(sim._work_agreement_text({}, 'not-a-dict'))

    def test_work_agreement_text_no_parts(self):
        agent = {'id': 'maya', 'role': '', 'profile': {}, 'accessGrant': ''}
        self.assertIsNone(sim._work_agreement_text({}, agent))

    def test_empower_work_agreement_no_agent(self):
        state = {'agents': {}}
        onboard = {'agentId': 'maya'}
        self.assertEqual(sim._empower_work_agreement(state, onboard, 1_000_000), (None, None))

    def test_empower_work_agreement_no_text(self):
        state = {'agents': {'maya': {'id': 'maya', 'role': '', 'profile': {},
                                     'accessGrant': ''}}}
        onboard = {'agentId': 'maya'}
        self.assertEqual(sim._empower_work_agreement(state, onboard, 1_000_000), (None, None))

    def test_resolve_onboard_meeting_vanished_hire(self):
        state = {
            'agents': {'d1': {'id': 'd1', 'busy': True, 'visible': False,
                              'inRoom': 'commandcenter'}},
            'agentRoster': [{'id': 'd1', 'name': 'Dana'}],
            '_pendingOnboard': {'agentId': 'maya', 'directorId': 'd1', 'coworkerIds': []},
        }
        onboard = state['_pendingOnboard']
        sim._resolve_onboard_meeting(state, onboard, now_ms=1_000_000)
        self.assertNotIn('_pendingOnboard', state)


if __name__ == '__main__':
    unittest.main()