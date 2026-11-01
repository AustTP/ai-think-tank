"""The band router trainer (tools/train_band_router.py).

Trains the spend/risk model off the tank's own operating history:
task_archive (features) + grade_outcomes (behavioral label) + kv_spend
(per-task model spend). The artifact is a hypothesis generator for the
operator, not a policy: bands are assigned deterministically today, so the
data has no counterfactuals, and the artifact says so.

Hermetic: a throwaway SQLite fixture DB seeded with synthetic history.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'tools')
sys.path.insert(0, TOOLS_DIR)

import train_band_router as tbr  # noqa: E402


def _seed_db(path, n_tasks=120, seed=7):
    """Synthetic history: two cells with very different rework rates and
    spend so both the risk model and the delta proposals have signal."""
    import random
    rng = random.Random(seed)
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE task_archive (id TEXT PRIMARY KEY, '
                 'blob TEXT NOT NULL, updated_at REAL)')
    conn.execute('CREATE TABLE grade_outcomes (tape_id INTEGER PRIMARY KEY, '
                 'task_ref TEXT, grade_ts REAL, grade TEXT, confidence REAL, '
                 'outcome TEXT, evidence TEXT, computed_at REAL)')
    conn.execute('CREATE TABLE kv_spend (id INTEGER PRIMARY KEY '
                 'CHECK (id = 1), blob TEXT NOT NULL, updated_at REAL)')
    spend_map = {}
    for i in range(n_tasks):
        hard = i % 2 == 0  # 'pipeline/observatory' fails more and costs more
        task_type = 'code' if hard else 'report'
        room = 'observatory' if hard else 'pressoffice'
        blob = json.dumps({
            'id': f'task-{i}', 'title': f'task {i}', 'taskType': task_type,
            'room': room, 'sizeEstimate': 'M' if hard else 'S',
            'priority': 'normal', 'budgetUsd': 0.5 if hard else None,
            'instructions': 'do the thing ' * (12 if hard else 3),
        })
        conn.execute('INSERT INTO task_archive (id, blob, updated_at) '
                     'VALUES (?, ?, ?)', (f'task-{i}', blob, i))
        spend = 0.12 if hard else 0.004
        spend_map[f'__task__/task-{i}'] = {
            'used': spend, 'calls': 30 if hard else 8}
        if i % 3:  # two thirds matured
            outcome = 'rework' if (hard and rng.random() < 0.8) else \
                      ('rework' if (not hard and rng.random() < 0.1) else 'held')
            conn.execute('INSERT INTO grade_outcomes (tape_id, task_ref, '
                         'grade_ts, grade, confidence, outcome, evidence, '
                         'computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         (i, f'task-{i}', 1000.0, '6', 0.9, outcome, None, 1001.0))
    conn.execute('INSERT INTO kv_spend (id, blob, updated_at) VALUES (1, ?, ?)',
                 (json.dumps(spend_map), 0))
    conn.commit()
    conn.close()


class Loader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-router-test-')
        self.db = os.path.join(self.tmp, 'fixture.db')
        _seed_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rows_join_features_spend_and_outcome(self):
        rows = tbr._load_rows(self.db)
        self.assertEqual(len(rows), 120)
        row = rows[0]
        self.assertIn('taskType', row)
        self.assertGreater(row['spend'], 0)
        self.assertIn(row['outcome'], ('rework', 'held', None))

    def test_pending_outcomes_excluded_from_matured(self):
        rows = tbr._load_rows(self.db)
        matured = [r for r in rows if r['outcome'] in ('rework', 'held')]
        self.assertEqual(len(matured), 80)  # two thirds of 120


class Train(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='think-tank-router-train-')
        cls.db = os.path.join(cls.tmp, 'fixture.db')
        _seed_db(cls.db, n_tasks=240)
        cls.rows = tbr._load_rows(cls.db)
        cls.artifact = tbr.train(cls.rows)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_artifact_shape(self):
        self.assertEqual(self.artifact['schema'], 'band-router-artifact')
        self.assertIn('limits', self.artifact)
        self.assertGreater(self.artifact['samples']['matured'], 0)
        self.assertIn('riskModel', self.artifact)
        self.assertIn('spendTable', self.artifact)

    def test_risk_model_beats_the_base_rate(self):
        rm = self.artifact['riskModel']
        auc = rm['eval']['testAuc']
        self.assertIsNotNone(auc)
        self.assertGreater(auc, 0.6)  # the synthetic split is learnable
        self.assertTrue(rm['topDrivers'])

    def test_spend_table_has_both_cells(self):
        table = self.artifact['spendTable']
        self.assertTrue(any(k.startswith('code/') for k in table))
        self.assertTrue(any(k.startswith('report/') for k in table))
        code_cell = next(c for k, c in table.items() if k.startswith('code/'))
        self.assertGreater(code_cell['medianSpendUsd'], 0.05)

    def test_delta_proposals_fire_on_the_synthetic_split(self):
        proposals = {p['cell']: p['direction']
                     for p in self.artifact['bandDeltaProposals']}
        self.assertTrue(proposals)
        for direction in proposals.values():
            self.assertIn(direction, ('raise-tier', 'candidate-cheaper-tier'))


class Limits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-router-limits-')
        self.db = os.path.join(self.tmp, 'fixture.db')
        _seed_db(self.db, n_tasks=240)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_too_few_rows_skips_the_risk_model(self):
        tmp = tempfile.mkdtemp(prefix='think-tank-router-small-')
        try:
            db = os.path.join(tmp, 'small.db')
            _seed_db(db, n_tasks=20)
            artifact = tbr.train(tbr._load_rows(db))
            self.assertNotIn('riskModel', artifact)
            self.assertIn('spendTable', artifact)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_honest_limits_are_recorded_in_the_artifact(self):
        artifact = tbr.train(tbr._load_rows(self.db))
        self.assertTrue(any('no counterfactuals' in line
                            for line in artifact['limits']))


if __name__ == '__main__':
    unittest.main()
