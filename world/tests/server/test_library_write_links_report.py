"""A report-lane card closes on task.reportPath + note; the Library write
endpoint used to log and vanish, so every finished spike/report run was
refused at the completion gate ('missing_saved_deliverable') and its card
re-issued (2026-11-07 shard-triage churn)."""
import json
import os
import unittest
from unittest import mock

import serve


class LinkLibraryWriteTests(unittest.TestCase):
    def _state(self):
        return {
            'agents': {'marcus': {'task': 'task-22339'}},
            'tasks': {'task-22339': {
                'id': 'task-22339', 'taskType': 'spike',
                'deliverable': 'report', 'title': 'shard 3/5'}},
        }

    def test_write_links_report_path_and_seeds_note(self):
        st = self._state()
        with mock.patch.object(serve, 'get_state_from_db', return_value=st), \
                mock.patch.object(serve, 'save_state_to_db',
                                  return_value=True) as saved:
            serve._link_library_write_to_task(
                'marcus', 'archive/1794073328929-spike-task-22339.md')
        self.assertTrue(saved.called, 'a linkage must persist')
        task = st['tasks']['task-22339']
        self.assertEqual(task['reportPath'],
                         'archive/1794073328929-spike-task-22339.md')
        self.assertTrue(task['note'].strip())

    def test_unrelated_path_does_not_link(self):
        st = self._state()
        with mock.patch.object(serve, 'get_state_from_db', return_value=st), \
                mock.patch.object(serve, 'save_state_to_db',
                                  return_value=True) as saved:
            serve._link_library_write_to_task(
                'marcus', 'archive/notes-something-else.md')
        self.assertFalse(saved.called)
        self.assertIsNone(st['tasks']['task-22339'].get('reportPath'))

    def test_code_lane_write_does_not_claim_report(self):
        st = self._state()
        st['tasks']['task-22339']['deliverable'] = 'code'
        st['tasks']['task-22339']['taskType'] = 'task'
        with mock.patch.object(serve, 'get_state_from_db', return_value=st), \
                mock.patch.object(serve, 'save_state_to_db',
                                  return_value=True) as saved:
            serve._link_library_write_to_task(
                'marcus', 'archive/1794073328929-spike-task-22339.md')
        self.assertFalse(saved.called)

    def test_existing_note_and_report_are_preserved(self):
        st = self._state()
        st['tasks']['task-22339']['reportPath'] = 'archive/orig.md'
        st['tasks']['task-22339']['note'] = 'already recorded'
        with mock.patch.object(serve, 'get_state_from_db', return_value=st), \
                mock.patch.object(serve, 'save_state_to_db',
                                  return_value=True) as saved:
            serve._link_library_write_to_task(
                'marcus', 'archive/1794073328929-spike-task-22339.md')
        self.assertFalse(saved.called, 'nothing to change, nothing to save')
        self.assertEqual(st['tasks']['task-22339']['reportPath'], 'archive/orig.md')
        self.assertEqual(st['tasks']['task-22339']['note'], 'already recorded')

    def test_idle_agent_is_a_no_op(self):
        st = self._state()
        st['agents']['marcus']['task'] = None
        with mock.patch.object(serve, 'get_state_from_db', return_value=st), \
                mock.patch.object(serve, 'save_state_to_db',
                                  return_value=True) as saved:
            serve._link_library_write_to_task(
                'marcus', 'archive/1794073328929-spike-task-22339.md')
        self.assertFalse(saved.called)


if __name__ == '__main__':
    unittest.main()
