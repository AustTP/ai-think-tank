"""One-shot cleanup of pending escalations and the wedged cora item
(world/cleanup_open_items.py).

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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cleanup_open_items as cleanup  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cleanup-open-items-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = os.path.join(self.tmp, 'think_tank.db')
        self.escalations = os.path.join(self.tmp, 'escalations.json')
        self._db_cm = unittest.mock.patch.object(cleanup, 'DB', self.db)
        self._esc_cm = unittest.mock.patch.object(cleanup, 'ESCALATIONS',
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
        return {
            'workQueue': [
                {'title': 'Act on mail (cora)', 'attempts': 10000},
                {'title': 'Read the new story', 'attempts': 1},
            ],
            'backlogRequests': [
                {'id': 'wrq-8', 'status': 'in_progress'},
                {'id': 'wrq-9', 'status': 'in_progress'},
            ],
            'tasks': {},
        }

    def _saved(self):
        with sqlite3.connect(self.db) as conn:
            row = conn.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
        return json.loads(row[0])


class Main(_Case):
    def test_resolves_pending_escalations_and_fixes_state(self):
        with open(self.escalations, 'w') as f:
            json.dump({
                'esc-1': {'status': 'pending'},
                'esc-2': {'status': 'resolved'},
            }, f)
        cleanup.main()
        with open(self.escalations) as f:
            esc = json.load(f)
        self.assertEqual(esc['esc-1']['status'], 'resolved')
        self.assertEqual(esc['esc-1']['resolvedBy'],
                         'manual-cleanup (auto-handling policy)')
        self.assertIn('resolvedAt', esc['esc-1'])
        self.assertEqual(esc['esc-2']['status'], 'resolved')
        self.assertNotIn('resolvedBy', esc['esc-2'])
        state = self._saved()
        self.assertEqual([it['title'] for it in state['workQueue']],
                         ['Read the new story'])
        wrq8 = [r for r in state['backlogRequests'] if r['id'] == 'wrq-8'][0]
        self.assertEqual(wrq8['status'], 'done')
        self.assertIn('doneAt', wrq8)
        self.assertIn('doneNote', wrq8)
        wrq9 = [r for r in state['backlogRequests'] if r['id'] == 'wrq-9'][0]
        self.assertEqual(wrq9['status'], 'in_progress')

    def test_no_escalations_file_is_fine(self):
        self.assertFalse(os.path.exists(self.escalations))
        cleanup.main()
        state = self._saved()
        self.assertEqual(len(state['workQueue']), 1)

    def test_empty_queue_and_backlog(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE kv_state SET blob = ? WHERE id = 1',
                         (json.dumps({'workQueue': [], 'backlogRequests': []}),))
        with open(self.escalations, 'w') as f:
            json.dump({}, f)
        cleanup.main()
        state = self._saved()
        self.assertEqual(state['workQueue'], [])
        self.assertEqual(state['backlogRequests'], [])


if __name__ == '__main__':
    unittest.main()
