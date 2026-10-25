"""One-shot thaw of the frozen wrq-8 peer-review gates
(world/reset_wrq8_gates.py).

Hermetic: DB and ESCALATIONS are rebound to a throwaway temp dir, so
the real think_tank.db and escalations.json are never touched.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tools.reset_wrq8_gates as reset  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='reset-wrq8-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = os.path.join(self.tmp, 'think_tank.db')
        self.escalations = os.path.join(self.tmp, 'escalations.json')
        self._db_cm = unittest.mock.patch.object(reset, 'DB', self.db)
        self._esc_cm = unittest.mock.patch.object(reset, 'ESCALATIONS',
                                                      self.escalations)
        self._db_cm.start()
        self._esc_cm.start()
        self.addCleanup(self._db_cm.stop)
        self.addCleanup(self._esc_cm.stop)
        conn = sqlite3.connect(self.db)
        conn.execute('CREATE TABLE kv_state '
                     '(id INTEGER PRIMARY KEY, blob TEXT, updated_at REAL)')
        conn.execute('INSERT INTO kv_state (id, blob, updated_at) VALUES (1, ?, 0)',
                     (json.dumps(self._state()),))
        conn.commit()
        conn.close()

    def _state(self):
        return {'tasks': {
            't1': {'title': 'Build the boids simulation', 'status': 'failed',
                   'assignedTo': 'cora',
                   '_peerGate': {'escalated': True, 'cycleCount': 3}},
            't2': {'title': 'Write the planner docs', 'status': 'failed',
                   'assignedTo': 'ben',
                   '_peerGate': {'escalated': True, 'cycleCount': 2}},
            't3': {'title': 'Sir epidemic model', 'status': 'needs_review',
                   'assignedTo': 'maya',
                   '_peerGate': {'escalated': False, 'cycleCount': 0}},
            't4': 'not a dict',
            't5': {'title': 'Build the lab', 'projectLabel': 'simulation engine',
                   'status': 'failed', 'assignedTo': 'cora',
                   '_peerGate': {'escalated': True, 'cycleCount': 1}},
        }}

    def _saved(self):
        with sqlite3.connect(self.db) as conn:
            row = conn.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
        return json.loads(row[0])


class IsWrq8(unittest.TestCase):
    def test_markers_match_case_insensitively(self):
        self.assertTrue(reset._is_wrq8('Build the SIR EPIDEMIC model'))
        self.assertTrue(reset._is_wrq8('headless mode'))
        self.assertFalse(reset._is_wrq8('Write the planner docs'))
        self.assertFalse(reset._is_wrq8(None))


class Main(_Case):
    def test_thaws_only_failed_or_escalated_wrq8_gates(self):
        with open(self.escalations, 'w') as f:
            json.dump({
                'esc-1': {'kind': 'stuck review loop', 'status': 'pending',
                          'question': 'The boids simulation review loop is stuck'},
                'esc-2': {'kind': 'stuck review loop', 'status': 'pending',
                          'question': 'The planner docs review loop is stuck'},
                'esc-3': {'kind': 'stuck review loop', 'status': 'resolved',
                          'question': 'The boids simulation review loop is stuck'},
                'esc-4': {'kind': 'other kind', 'status': 'pending',
                          'question': 'The boids simulation review loop is stuck'},
            }, f)
        reset.main()
        state = self._saved()
        t1 = state['tasks']['t1']
        self.assertEqual(t1['status'], 'needs_review')
        self.assertFalse(t1['_peerGate']['escalated'])
        self.assertEqual(t1['_peerGate']['cycleCount'], 0)
        # A non-wrq8 task keeps its failed status.
        self.assertEqual(state['tasks']['t2']['status'], 'failed')
        # An already-thawed task is skipped.
        self.assertEqual(state['tasks']['t3']['status'], 'needs_review')
        # A non-dict task is skipped without crashing.
        self.assertEqual(state['tasks']['t4'], 'not a dict')
        # The projectLabel matches even though the title does not.
        t5 = state['tasks']['t5']
        self.assertEqual(t5['status'], 'needs_review')
        self.assertEqual(t5['_peerGate']['cycleCount'], 0)
        with open(self.escalations) as f:
            esc = json.load(f)
        self.assertEqual(esc['esc-1']['status'], 'resolved')
        self.assertEqual(esc['esc-1']['resolvedBy'],
                         'manual-gate-reset (reset_wrq8_gates.py)')
        self.assertIn('resolvedAt', esc['esc-1'])
        self.assertEqual(esc['esc-2']['status'], 'pending')
        self.assertEqual(esc['esc-3']['status'], 'resolved')
        self.assertEqual(esc['esc-4']['status'], 'pending')

    def test_no_escalations_file_is_fine(self):
        self.assertFalse(os.path.exists(self.escalations))
        reset.main()
        self.assertEqual(self._saved()['tasks']['t1']['status'], 'needs_review')


if __name__ == '__main__':
    unittest.main()
