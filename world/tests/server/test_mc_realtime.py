"""Sealed live-query realtime: seal/unseal roundtrip, allowlist, expiry,
tamper rejection, projection diffing, and the map drift check."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import server.mc_realtime as mcr  # noqa: E402
import server.mc_projection as mcp  # noqa: E402

_QUERIES = {
    'demo': {'sql': 'SELECT 1 AS "one"', 'tables': frozenset({'mc_tasks'})},
    'param': {'sql': 'SELECT ? AS "p"', 'tables': frozenset()},
}


class SealTests(unittest.TestCase):
    def setUp(self):
        self._orig = mcr._QUERIES
        mcr._QUERIES = _QUERIES

    def tearDown(self):
        mcr._QUERIES = self._orig

    def test_roundtrip(self):
        sealed = mcr.seal('demo')
        name, params, fp = mcr.unseal(sealed['capability'])
        self.assertEqual(name, 'demo')
        self.assertEqual(params, [])
        self.assertEqual(fp, sealed['queryFingerprint'])
        self.assertGreater(sealed['expiresAt'], 0)

    def test_allowlist_enforced(self):
        with self.assertRaises(KeyError):
            mcr.seal('DROP TABLE users')

    def test_expired_rejected(self):
        sealed = mcr.seal('demo', now_ms=1_000_000)
        with self.assertRaises(ValueError):
            mcr.unseal(sealed['capability'], now_ms=2_000_000)

    def test_tamper_rejected(self):
        sealed = mcr.seal('demo')
        cap = sealed['capability']
        with self.assertRaises(ValueError):
            mcr.unseal(cap[:-6] + 'AAAAAA')
        with self.assertRaises(ValueError):
            mcr.unseal('mc2.' + cap.split('.', 1)[1])

    def test_param_fingerprint_binds_params(self):
        sealed = mcr.seal('param', ['abc'])
        name, params, _fp = mcr.unseal(sealed['capability'])
        self.assertEqual(params, ['abc'])
        # a capability sealed for other params must not pass this fp
        other = mcr.seal('param', ['xyz'])
        self.assertNotEqual(sealed['queryFingerprint'],
                            other['queryFingerprint'])

    def test_tables_header_declares_dependencies(self):
        self.assertEqual(mcr.query_tables('demo'), {'mc_tasks'})


class ProjectionTests(unittest.TestCase):
    def _conn(self):
        import db_backend
        conn = db_backend.connect()
        mcp.ensure_schema(conn)
        return conn

    def test_sync_upserts_and_notifies(self):
        state = {
            'tasks': {'t1': {'id': 't1', 'title': 'A', 'status': 'working',
                             'createdAt': 1}},
            'agents': {'ada': {'id': 'ada', 'name': 'Ada', 'role': 'dir'}},
            'programs': [{'id': 'p1', 'title': 'P', 'status': 'planning',
                          'pieceStatus': {'x': 'unminted'}}],
            'workQueue': [{'id': 'q1', 'title': 'Q'}],
            'issues': {'i1': {'id': 'i1', 'title': 'I', 'status': 'open'}},
            'playerInbox': [{'id': 'm1', 'agentId': 'ada', 'title': '?',
                             'createdAt': 5}],
        }
        conn = self._conn()
        try:
            changed = mcp.sync(state, conn=conn, notify_tables=False)
            self.assertIn('mc_tasks', changed)
            rows = conn.execute(
                'SELECT status FROM mc_tasks WHERE id=?', ('t1',)).fetchall()
            self.assertEqual(rows, [('working',)])
            inbox = conn.execute(
                'SELECT status FROM mc_inbox WHERE id=?', ('m1',)).fetchall()
            self.assertEqual(inbox, [('awaiting_input',)])
            # same state again: nothing changes
            self.assertEqual(mcp.sync(state, conn=conn,
                                      notify_tables=False), [])
            # status flips: the row updates
            state['tasks']['t1']['status'] = 'done'
            changed = mcp.sync(state, conn=conn, notify_tables=False)
            self.assertIn('mc_tasks', changed)
            row = conn.execute(
                'SELECT status FROM mc_tasks WHERE id=?', ('t1',)).fetchone()
            self.assertEqual(row, ('done',))
        finally:
            for t in ('mc_tasks', 'mc_agents', 'mc_programs', 'mc_work_queue',
                      'mc_issues', 'mc_inbox'):
                conn.execute(f'DELETE FROM {t}')
            conn.commit()
            conn.close()

    def test_sync_never_raises(self):
        conn = self._conn()
        try:
            changed = mcp.sync({'tasks': None, 'issues': 'not-a-dict'},
                               conn=conn, notify_tables=False)
            self.assertIsInstance(changed, list)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
