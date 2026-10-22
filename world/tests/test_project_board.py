"""Tests for the read-only project board."""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import content
import serve


class ProjectBoard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='board-test-')
        self.library = os.path.join(self.tmp, 'library')
        os.makedirs(self.library)
        self.state = {'tasks': {
            't1': {'title': 'Open research', 'status': 'working', 'assignedTo': 'cora',
                   'teamId': 'nora', 'projectLabel': 'Atlas', 'priority': 'high',
                   'budgetUsd': 1.0, 'spentUsd': 0.2, 'reportPath': 'archive/a.md',
                   'acceptanceCriteria': 'A source', 'parentTaskId': 'sup1', 'updatedAt': 3},
            't2': {'title': 'Done report', 'status': 'done', 'assignedTo': 'leo',
                   'teamId': 'nora', 'projectLabel': 'Atlas', 'reportPath': 'archive/b.md',
                   'updatedAt': 2},
            't3': {'title': 'Other team', 'status': 'queued', 'teamId': 'ruth',
                   'projectLabel': 'Other', 'updatedAt': 1},
        }}
        open(os.path.join(self.library, 'archive'), 'w').close() if False else None
        os.makedirs(os.path.join(self.library, 'archive'), exist_ok=True)
        with open(os.path.join(self.library, 'archive', 'a.md'), 'w') as f:
            f.write('x')
        self.patcher = unittest.mock.patch.multiple(
            serve, LIBRARY_DIR=self.library,
            get_state_from_db=lambda: self.state,
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_open_filter_and_artifact(self):
        out = json.loads(content.project_board({'status': 'open'}))
        self.assertEqual(out['counts']['open'], 2)
        self.assertEqual([t['taskId'] for t in out['tasks']], ['t1', 't3'])
        t1 = out['tasks'][0]
        self.assertTrue(t1['artifactExists'])
        self.assertEqual(t1['parentTaskId'], 'sup1')
        self.assertEqual(t1['spentUsd'], 0.2)

    def test_team_and_project_filters(self):
        out = json.loads(content.project_board({'team': 'nora', 'project': 'atlas', 'status': 'all'}))
        self.assertEqual({t['taskId'] for t in out['tasks']}, {'t1', 't2'})
        done = json.loads(content.project_board({'status': 'done'}))
        self.assertEqual([t['taskId'] for t in done['tasks']], ['t2'])


if __name__ == '__main__':
    unittest.main()
