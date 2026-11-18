"""Tests for the harness optimizer: propose -> apply -> measure over the
epistemic ledger, with the Engine sweep as the diagnosis source.

Seams: epistemics/evidence in-memory (the standard harness), action_log in
a temp SQLite DB (the Seer/Engine harness). No real ledger, no Postgres
writes, no wiki state.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.epistemics as epistemics  # noqa: E402
import core.evidence as evidence  # noqa: E402
import core.harness_optimizer as harness_optimizer  # noqa: E402

from test_steal_list import _patch_epistemic_stores  # noqa: E402


class TempDb(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-harness-tests-')
        self._conn = sqlite3.connect(os.path.join(self._tmp, 'test.db'))
        self._conn.execute(
            'CREATE TABLE IF NOT EXISTS action_log ('
            ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
            ' ts REAL NOT NULL, agent_id TEXT, action TEXT,'
            ' details TEXT, authorized INTEGER DEFAULT 1)')
        self._conn.commit()

    def tearDown(self):
        self._conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def log(self, agent_id, action, details, age_s=0.0):
        self._conn.execute(
            'INSERT INTO action_log (ts, agent_id, action, details) '
            'VALUES (?, ?, ?, ?)',
            (time.time() - age_s, agent_id, action, json.dumps(details)))
        self._conn.commit()

    def clear_log(self):
        self._conn.execute('DELETE FROM action_log')
        self._conn.commit()

    def seed_cluster(self, n=2, agent='sam', reason='stale after timeouts'):
        for i in range(n):
            self.log(agent, 'task_requeued_stale', {'reason': reason},
                     age_s=900 * (i + 1))


class AnnotateAndStatus(unittest.TestCase):
    def setUp(self):
        self.stores = _patch_epistemic_stores()

    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_annotate_whitelist_and_status_guard(self):
        iid = epistemics.record_unknown('Is X true?', actor='m')['id']
        out = epistemics.annotate_item(iid, {'sourceKey': 'a:b',
                                             'status': 'evidenced'})
        self.assertEqual(out['fields'], ['sourceKey'],
                         'status is not annotatable')
        self.assertEqual(epistemics.get_item(iid)['status'], 'unknown')
        out = epistemics.set_status(iid, 'evidenced', actor='harness',
                                    note='applied')
        self.assertTrue(out['advanced'])
        self.assertEqual(epistemics.get_item(iid)['status'], 'evidenced')
        out = epistemics.set_status(iid, 'validated', actor='harness')
        self.assertFalse(out['advanced'],
                         'evidenced -> validated is the review gate, not '
                         'set_status')
        self.assertEqual(out['reason'], 'gated_transition')

    def test_transitions_enforced(self):
        iid = epistemics.record_unknown('Is Y true?', actor='m')['id']
        self.assertFalse(epistemics.set_status(
            iid, 'superseded', actor='m')['advanced'],
            'superseded is a gated transition (supersede_item only)')


class HarnessPropose(TempDb):
    def setUp(self):
        super().setUp()
        self.stores = _patch_epistemic_stores()

    def tearDown(self):
        unittest.mock.patch.stopall()
        super().tearDown()

    def test_propose_creates_hypothesis_with_registry_body(self):
        self.seed_cluster(2)
        out = harness_optimizer.propose(self._conn, actor='harness-opt')
        self.assertTrue(out['proposed'])
        self.assertTrue(out['pageId'].startswith('harness-'))
        self.assertIn('| Symptom | Known fix | Status |', out['body'])
        self.assertIn('2 failures', out['body'])
        item = epistemics.get_item(out['itemId'])
        self.assertEqual(item['status'], 'evidenced',
                         'the proposal cites sweep evidence, so the '
                         'hypothesis advances to evidenced on record')
        self.assertEqual(item['sourceKey'], out['symptomKey'])
        self.assertGreaterEqual(len(item.get('evidence') or []), 1,
                                'proposal cites the sweep as evidence')

    def test_propose_dedupes_open_hypothesis(self):
        self.seed_cluster(2)
        first = harness_optimizer.propose(self._conn)
        second = harness_optimizer.propose(self._conn)
        self.assertFalse(second['proposed'])
        self.assertEqual(second['reason'], 'already_open')
        self.assertEqual(second['itemId'], first['itemId'])

    def test_propose_no_clusters(self):
        out = harness_optimizer.propose(self._conn)
        self.assertFalse(out['proposed'])
        self.assertEqual(out['reason'], 'no_cluster')

    def test_mark_applied_records_decision_and_measure_validation(self):
        self.seed_cluster(3)
        proposal = harness_optimizer.propose(self._conn)
        page = {'version': 1}
        out = harness_optimizer.mark_applied(proposal, page, actor='harness')
        self.assertTrue(out['validationId'])
        self.assertTrue(out['decisionId'])
        item = epistemics.get_item(proposal['itemId'])
        self.assertEqual(item['status'], 'evidenced')
        self.assertEqual(item['measureBaseline']['clusterSize'], 3)
        self.assertEqual(item['pageId'], proposal['pageId'])
        self.assertIn(out['validationId'], item.get('validationIds') or [])
        self.assertIn(out['decisionId'], item.get('decisionIds') or [])


class HarnessMeasure(TempDb):
    def setUp(self):
        super().setUp()
        self.stores = _patch_epistemic_stores()

    def tearDown(self):
        unittest.mock.patch.stopall()
        super().tearDown()

    def _applied_revision(self):
        self.seed_cluster(2)
        proposal = harness_optimizer.propose(self._conn)
        harness_optimizer.mark_applied(proposal, {'version': 1},
                                       actor='harness')
        return proposal

    def test_improved_cluster_recommends_keep(self):
        self._applied_revision()
        self.clear_log()
        out = harness_optimizer.measure(self._conn, window_s=86400)
        revs = out['revisions']
        self.assertEqual(len(revs), 1)
        self.assertTrue(revs[0]['improved'])
        self.assertIn('keep', revs[0]['recommendation'])
        self.assertEqual(revs[0]['currentSize'], 0)

    def test_worse_cluster_recommends_supersede(self):
        self._applied_revision()
        self.seed_cluster(4)  # fresh, larger cluster in the measure window
        out = harness_optimizer.measure(self._conn, window_s=86400)
        revs = out['revisions']
        self.assertEqual(len(revs), 1)
        self.assertFalse(revs[0]['improved'])
        self.assertIn('supersede', revs[0]['recommendation'])
        item = epistemics.get_item(revs[0]['itemId'])
        self.assertEqual(item['status'], 'evidenced',
                         'measurement never auto-supersedes; the review '
                         'gate decides')

    def test_no_open_revisions(self):
        out = harness_optimizer.measure(self._conn)
        self.assertEqual(out['revisions'], [])


if __name__ == '__main__':
    unittest.main()
