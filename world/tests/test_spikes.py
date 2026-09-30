"""Phase E2b: the SPIKE lane.

A spike is a time-boxed investigation with no committed deliverable. It's
lowest-priority, carries an advisory time budget, and -- because nothing ships
-- its completion does NOT open a peer gate (unlike a normal deliverable
story). This suite covers the pure lane helpers hermetically and the
queue_spike shaping. The gate-bypass is validated against _peer_gated_lane; the
spike continues to complete via finish_task (the non-deliverable branch).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim


def _state(**over):
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': [{'id': 'maya', 'name': 'Maya', 'role': 'director', 'isAdmin': True}],
        'agents': {'maya': {'id': 'maya', 'x': 0, 'y': 0, 'busy': True}},
        'workQueue': [],
        'tasks': {},
        'reports': [],
    }
    state.update(over)
    return state


class SpikeLane(unittest.TestCase):
    def test_queue_spike_shapes_low_priority_spike_item(self):
        state = _state()
        n = sim.queue_spike(state, 'Does WebAssembly work headless?', 'observatory',
                            budget_ms=45_000, goal='wasm', now_ms=1000,
                            project_label='research')
        self.assertEqual(n, 1)
        item = state['workQueue'][0]
        self.assertEqual(item['taskType'], 'spike')
        self.assertEqual(item['budgetMs'], 45_000)
        self.assertEqual(item['priority'], sim.WORK_PRIORITY['low'])
        self.assertEqual(item['room'], 'observatory')
        self.assertTrue(item['instructions'])

    def test_budget_ms_survives_queue_work_whitelist(self):
        state = _state()
        sim.queue_spike(state, 'Prototype the auth handshake', 'pressoffice', budget_ms=30_000)
        self.assertEqual(state['workQueue'][0]['budgetMs'], 30_000)

    def test_peer_gated_lane_false_for_spike_in_deliverable_room(self):
        # A spike filed in a deliverable room must NOT open a peer gate.
        spike = _task(taskType='spike', room='pressoffice')
        self.assertFalse(sim._peer_gated_lane(spike))
        # A normal deliverable story still gates.
        normal = _task(taskType='code', room='pressoffice')
        self.assertTrue(sim._peer_gated_lane(normal))
        # A bug (on-call incident) does not gate either.
        bug = _task(taskType='bug', room='pressoffice')
        self.assertFalse(sim._peer_gated_lane(bug))

    def test_non_deliverable_room_never_gates(self):
        # Chore rooms don't gate regardless of lane.
        for task_type in ('code', 'spike', 'bug', 'research'):
            t = _task(taskType=task_type, room='library')
            self.assertFalse(sim._peer_gated_lane(t))

    def test_scheduled_research_never_gates_even_in_a_deliverable_room(self):
        # Bug, confirmed: task['research'] (the marker
        # _check_schedules sets on a scheduled crawl) is NOT a taskType, and
        # observatory IS a deliverable room -- so a scheduled research task
        # slipped past the taskType-only NON_GATED_LANES check and entered
        # peer review like a real authored story. Its "review" always found
        # it "actionable" (a scheduled crawl has no passing flake8/mypy/
        # pytest-cov suite to fail cleanly), which queued a code fix for
        # something that was never a coding deliverable -- whose own review
        # found it just as unfixable, forever. One real "AI regulation news"
        # schedule spiraled into 3,917 task assignments and 26,687
        # escalations in a single evening before this fix.
        scheduled = _task(room='observatory', research={'topicId': 'topic-1', 'since': 0})
        self.assertFalse(sim._peer_gated_lane(scheduled))


def _task(**over):
    task = {'id': 'task-1', 'title': 't', 'room': 'pressoffice',
            'taskType': 'code', 'status': 'working', 'createdAt': 0,
            'assignedTo': 'ben'}
    task.update(over)
    return task


if __name__ == '__main__':
    unittest.main()