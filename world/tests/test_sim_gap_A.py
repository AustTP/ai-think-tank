"""Coverage-push tests for world/sim.py residual branches, gap A: the pure
pathing/room-overflow/queue-whitelist/on-call/bug-routing/sprint-rollover/
product-wiki/staffing guards that the existing suites never drive to
execution. Uses the temp-DB isolation pattern from test_sim_gap.py so the
real think_tank.db and agent dirs are never touched; every function under
test here is pure state mutation (no serve network/DB seam required).
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
        self.tmp = tempfile.mkdtemp(prefix='think-tank-simgapA-')
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
    def _busy_agents(room, count):
        return {f'{room}-{i}': {'id': f'{room}-{i}', 'busy': True, 'inRoom': room}
                for i in range(count)}


class PathingResidual(SimIsolation):
    def test_find_path_transition_sample_blocked_by_agent(self):
        # An agent parked exactly one start-box edge away is NOT co_located
        # (strict overlap fails on the shared y edge) but its box clips the
        # 4-point transition samples between cells (1,1)->(2,1) -- so the
        # BFS's transition_is_free returns False and the move is skipped.
        grid = self._free_grid()
        agents = {'ben': {'x': 10, 'y': 24, 'visible': True}}
        path = sim.find_path(8, 8, 56, 16, 'me', agents, grid)
        self.assertIsNone(path)

    def test_pick_free_spot_skips_avoid_point_and_falls_back(self):
        # rnd=0.0 always proposes (0,0), which is free/reachable but sits
        # inside PLACEMENT_MIN_DIST of the avoid point -> too_close -> continue
        # for all 300 tries, then the SPAWN fallback wins.
        grid = self._free_grid()
        spot = sim.pick_free_spot(grid, avoid_points=[{'x': 0, 'y': 0}],
                                  spawn={'x': 0, 'y': 0}, rnd=lambda: 0.0)
        self.assertEqual(spot, {'x': 0, 'y': 0})


class RoomOverflow(SimIsolation):
    def test_room_occupancy_counts_busy_agents_in_room(self):
        state = {'agents': self._busy_agents('pressoffice', 3)}
        state['agents']['idle'] = {'id': 'idle', 'busy': False, 'inRoom': 'pressoffice'}
        state['agents']['other'] = {'id': 'other', 'busy': True, 'inRoom': 'observatory'}
        self.assertEqual(sim.room_occupancy(state, 'pressoffice'), 3)

    def test_resolve_room_with_overflow_redirects_when_target_free(self):
        # Press Office full (6/6 workstations) -> observatory has a free desk.
        state = {'agents': self._busy_agents('pressoffice', 6)}
        self.assertEqual(sim.resolve_room_with_overflow(state, 'pressoffice'), 'observatory')

    def test_resolve_room_with_overflow_stays_when_target_full(self):
        # Both pressoffice and its overflow target are at capacity.
        state = {'agents': {}}
        state['agents'].update(self._busy_agents('pressoffice', 6))
        state['agents'].update(self._busy_agents('observatory', 6))
        self.assertEqual(sim.resolve_room_with_overflow(state, 'pressoffice'), 'pressoffice')


class QueueResidual(SimIsolation):
    def test_queue_work_reinitializes_non_list_work_queue(self):
        state = {'workQueue': 'not-a-list'}
        length = sim.queue_work(state, [{'title': 'x', 'room': 'hangout'}])
        self.assertEqual(length, 1)
        self.assertIsInstance(state['workQueue'], list)

    def test_queue_work_skips_items_without_title(self):
        state = {}
        length = sim.queue_work(state, [None, {'room': 'hangout'}, {'title': 'real', 'room': 'hangout'}])
        self.assertEqual(length, 1)
        self.assertEqual(state['workQueue'][0]['title'], 'real')


class OnCallAndBugs(SimIsolation):
    def test_rotate_served_oncalls_skips_unknown_team(self):
        state = {'teams': [{'id': 't1', 'directorId': 'd1'}]}
        sim._rotate_served_oncalls(state, ['ghost-team'])
        self.assertEqual(state['_oncallOrder'], {})

    def test_rotate_served_oncalls_skips_team_without_director_id(self):
        # A team record with no id/directorId is found by the None key but has
        # no director id to rotate.
        state = {'teams': [{'name': 'Ghost team'}], '_oncallServed': {None: ['m1']}}
        sim._rotate_served_oncalls(state, [None])
        self.assertEqual(state['_oncallServed'][None], ['m1'])

    def test_queue_bug_no_on_call_returns_none(self):
        state = {'products': {'p1': {'id': 'p1', 'teamId': 'd1'}}, 'agentRoster': []}
        self.assertIsNone(sim.queue_bug(state, 'p1', 'broken'))

    def test_queue_bug_existing_task_caps(self):
        state = {
            'products': {'p1': {'id': 'p1', 'teamId': 'd1'}},
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': 'd1'},
                {'id': 'm1', 'name': 'Mia', 'director': 'd1'},
            ],
            'agents': {'m1': {'id': 'm1', 'offDuty': False, 'busy': False}},
            'tasks': {'t1': {'id': 't1', 'taskType': 'bug', 'productId': 'p1'}},
            'workQueue': [],
        }
        self.assertIsNone(sim.queue_bug(state, 'p1', 'broken'))

    def test_queue_bug_happy_path_queues_incident(self):
        state = {
            'products': {'p1': {'id': 'p1', 'teamId': 'd1'}},
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': 'd1'},
                {'id': 'm1', 'name': 'Mia', 'director': 'd1'},
            ],
            'agents': {'m1': {'id': 'm1', 'offDuty': False, 'busy': False}},
            'tasks': {},
            'workQueue': [],
        }
        length = sim.queue_bug(state, 'p1', 'broken')
        self.assertEqual(length, 1)
        item = state['workQueue'][0]
        self.assertEqual(item['taskType'], 'bug')
        self.assertEqual(item['assignedTo'], 'm1')
        self.assertTrue(item['incident'])


class ProductAndSprint(SimIsolation):
    def test_completing_agent_skips_non_matching_deliverables(self):
        state = {
            'completedDeliverables': [
                {'title': 'unrelated chore', 'room': 'observatory', 'gradedAt': 1, 'agentId': 'nobody'},
                {'title': 'Ship P1 build', 'room': 'pressoffice', 'gradedAt': 5, 'agentId': 'm1'},
            ],
        }
        self.assertEqual(sim._completing_agent_for_product(state, 'p1',
                                                           prod={'id': 'p1', 'name': 'P1'}), 'm1')

    def test_sprint_progress_unknown_sprint_returns_none(self):
        self.assertIsNone(sim.sprint_progress({'sprints': {}}, 'spr-x'))

    def test_unfinished_sprint_items_skips_other_sprints(self):
        state = {
            'workQueue': [
                {'title': 'a', 'room': 'r', 'sprintId': 'spr-9'},
                {'title': 'b', 'room': 'r', 'sprintId': 'spr-1'},
            ],
            'tasks': {},
        }
        self.assertEqual(sim._unfinished_sprint_items(state, 'spr-1'),
                         [state['workQueue'][1]])

    def test_carry_over_skips_non_overlapping_teams(self):
        state = {'sprints': {
            'spr-1': {'id': 'spr-1', 'status': 'closed', 'teamIds': ['t2'], 'createdAt': 0},
        }}
        self.assertEqual(sim._carry_over_sprint_work(state, {'id': 'spr-2'}, ['t1']), 0)

    def test_carry_over_empty_rolled_over_returns_zero(self):
        state = {'sprints': {
            'spr-1': {'id': 'spr-1', 'status': 'closed', 'teamIds': ['t1'], 'createdAt': 0},
        }}
        self.assertEqual(sim._carry_over_sprint_work(state, {'id': 'spr-2'}, ['t1']), 0)

    def test_auto_close_completed_sprints_uses_wall_clock_when_none(self):
        self.assertEqual(sim._auto_close_completed_sprints({'sprints': {}}), [])


class FeatureProductWiki(SimIsolation):
    def test_find_feature_blank_name_returns_none(self):
        self.assertIsNone(sim._find_feature({}, '   '))
        self.assertIsNone(sim._find_feature({}, None))

    def test_create_product_blank_name_returns_none(self):
        self.assertIsNone(sim.create_product({}, 'p1', '  ', 's', 'sp', 'o1', 's1'))

    def test_set_product_status_unknown_product_returns_none(self):
        self.assertIsNone(sim.set_product_status({}, 'p1', 'draft'))
        record = sim.set_product_status({'products': {'p1': {'id': 'p1'}}}, 'p1', 'in_progress')
        self.assertEqual(record['status'], 'in_progress')

    def test_wiki_write_page_rejects_too_long_body(self):
        state = {'wiki': {'categories': {'ops': {'name': 'ops'}}}}
        record, is_new = sim.wiki_write_page(state, 'p1', 't', 'ops', 'x' * 200_001, 'me',
                                             edited_at_ms=1)
        self.assertIsNone(record)
        self.assertFalse(is_new)

    def test_release_uses_handle_unknown_product_returns_none(self):
        self.assertIsNone(sim.release_uses_handle({}, 'p1'))
        self.assertEqual(sim.release_uses_handle({'products': {'p1': {'handles': ['abc']}}}, 'p1'), 'abc')


class Staffing(SimIsolation):
    def test_any_available_off_duty_at_ceiling_skips_agent(self):
        # 25 on-duty agents == MAX_ACTIVE_AGENTS, so the lone off-duty agent
        # cannot be woken and the whole roster reports unavailable.
        roster = [{'id': 'off'}]
        agents = {f'a{i}': {'id': f'a{i}'} for i in range(sim.MAX_ACTIVE_AGENTS)}
        agents['off'] = {'id': 'off', 'offDuty': True}
        state = {'agentRoster': roster, 'agents': agents}
        self.assertFalse(sim._any_available_including_off_duty(state))


if __name__ == '__main__':
    unittest.main()