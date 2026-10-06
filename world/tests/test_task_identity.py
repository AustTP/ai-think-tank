"""Task IDs survive JSON reloads and recover from historical assignments."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve
import sim


class TaskIdentity(unittest.TestCase):
    def test_assignment_after_restart_never_overwrites_existing_task(self):
        state = {'tasks': {'task-41': {'id': 'task-41', 'title': 'Original'}},
                 'agents': {'ada': {'id': 'ada', 'x': 1, 'y': 1, 'busy': False}},
                 'agentRoster': [{'id': 'ada'}]}
        doors = {'observatory': {'x': 0, 'y': 0, 'w': 10, 'h': 10}}
        with patch.object(sim, 'find_path', return_value=[{'x': 5, 'y': 14}]), \
                patch.object(serve, 'log_action'):
            task = sim.assign_task(state, 'ada', 'New', 'observatory', '', '', {}, {}, doors, 1000, [0])
            self.assertEqual(task['id'], 'task-42')
            self.assertEqual(state['tasks']['task-41']['title'], 'Original')
            state = json.loads(json.dumps(state))
            # Old task records may be archived; the counter must still survive.
            state['tasks'] = {}
            state['agents']['ada'].update(busy=False, task=None)
            task = sim.assign_task(state, 'ada', 'After restart', 'observatory', '', '', {}, {}, doors, 2000, [0])
            self.assertEqual(task['id'], 'task-43')

    def test_backfill_includes_overwritten_task_ids_in_assignment_log(self):
        with tempfile.TemporaryDirectory() as folder, patch.multiple(
                serve, DB_PATH=os.path.join(folder, 'test.db'), THINK_TANK_DIR=folder,
                AGENTS_DIR=os.path.join(folder, 'agents'),
                LIBRARY_DIR=os.path.join(folder, 'library'),
                PASSPORT_PATH=os.path.join(folder, 'passport.json')):
            serve.init_db()
            serve.save_state_to_db({'tasks': {'task-4': {'id': 'task-4'}}})
            serve.log_action('ada', 'task_assigned', {'taskId': 'task-99'})
            serve._backfill_task_counter_in_db()
            self.assertEqual(serve.get_state_from_db()['taskCounter'], 99)
            serve._backfill_task_counter_in_db()
            self.assertEqual(serve.get_state_from_db()['taskCounter'], 99)

    def test_legacy_failed_repairs_are_corrected_without_changing_review_verdicts(self):
        with tempfile.TemporaryDirectory() as folder, patch.multiple(
                serve, DB_PATH=os.path.join(folder, 'test.db'), THINK_TANK_DIR=folder,
                AGENTS_DIR=os.path.join(folder, 'agents'),
                LIBRARY_DIR=os.path.join(folder, 'library'),
                PASSPORT_PATH=os.path.join(folder, 'passport.json')):
            serve.init_db()
            serve.save_state_to_db({'tasks': {
                'task-1': {'status': 'done', 'reviewOf': 'parent', 'taskType': 'code',
                           'note': 'execution exited non-zero (code 125)'},
                'task-2': {'status': 'done', 'reviewOf': 'parent', 'taskType': 'review',
                           'note': 'blocked: historical review'},
                'task-3': {'status': 'done', 'reviewOf': 'parent', 'taskType': 'code',
                           'note': 'All quality checks passed'},
            }})
            serve._backfill_failed_repairs_in_db()
            tasks = serve.get_state_from_db()['tasks']
            self.assertEqual(tasks['task-1']['status'], 'failed')
            self.assertEqual(tasks['task-1']['previousStatus'], 'done')
            self.assertEqual(tasks['task-2']['status'], 'done')
            self.assertEqual(tasks['task-3']['status'], 'done')
