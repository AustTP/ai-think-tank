"""Hermetic tests for the epistemic benchmark (core/epistemic_benchmark.py).

The scenario is the synthetic security investigation: can an AI
application's prompt-injection defenses be monitored operationally, from
attempted attack through downstream tool/action and incident response?
The seed carries deliberately incomplete and conflicting evidence. The
grader reads the epistemic SNAPSHOT against an answer key -- never prose.
These tests patch every ledger seam, so no DB and no network is touched.
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.evidence as evidence  # noqa: E402
import core.epistemics as epistemics  # noqa: E402
import core.epistemic_benchmark as benchmark  # noqa: E402


def _patch_all():
    stores = {'epistemic': {}, 'evidence': {}, 'coverage': {}}
    patchers = [
        unittest.mock.patch('core.epistemics._ledger_read',
                            lambda: stores['epistemic']),
        unittest.mock.patch('core.epistemics._ledger_write',
                            lambda blob: stores.__setitem__('epistemic', blob)),
        unittest.mock.patch('core.evidence._evidence_read',
                            lambda: stores['evidence']),
        unittest.mock.patch('core.evidence._evidence_write',
                            lambda rows: stores.__setitem__('evidence', rows)),
        unittest.mock.patch('core.evidence._coverage_read',
                            lambda: stores['coverage']),
        unittest.mock.patch('core.evidence._coverage_write',
                            lambda rows: stores.__setitem__('coverage', rows)),
        unittest.mock.patch('core.evidence.append_audit', return_value=None),
    ]
    for p in patchers:
        p.start()
    return stores


class ScenarioShape(unittest.TestCase):
    def setUp(self):
        self.stores = _patch_all()

    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_same_source_repeated_never_counts_independent(self):
        expected, ids = benchmark.build_scenario()
        repost = ids['gatewayRepost']
        self.assertTrue(repost['corroborated'])
        self.assertFalse(repost['claim']['independent'])
        item = epistemics.get_item(ids['gateway'])
        self.assertEqual(epistemics.independent_source_count(item), 1)

    def test_repeated_assertion_stays_unknown(self):
        expected, ids = benchmark.build_scenario()
        pager = epistemics.get_item(ids['pager'])
        self.assertEqual(len(pager['asked_by']), 2,
                         'two agents, one item')
        self.assertEqual(pager['status'], 'unknown')
        self.assertEqual(epistemics.classify_item(pager),
                         'Unsupported Assumption')

    def test_contradiction_stays_open_and_item_disputed(self):
        expected, ids = benchmark.build_scenario()
        cx = epistemics.get_contradiction(ids['contradiction'])
        self.assertEqual(cx['status'], 'open')
        self.assertEqual(epistemics.get_item(ids['filter'])['status'],
                         'disputed')

    def test_stale_rate_limit_doc_lost_trusted_status_but_stays_readable(self):
        expected, ids = benchmark.build_scenario()
        rate = epistemics.get_item(ids['rateLimit'])
        self.assertEqual(epistemics.effective_status(rate), 'stale')
        self.assertIsNotNone(epistemics.get_item(ids['rateLimit'])['text'])


class Grading(unittest.TestCase):
    def setUp(self):
        self.stores = _patch_all()

    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_seeded_snapshot_grades_full_score(self):
        expected, _ids = benchmark.build_scenario()
        report = benchmark.grade_snapshot(benchmark.epistemic_snapshot(),
                                          expected)
        self.assertEqual(report['score'], 1.0,
                         report['categories'])
        for cat, row in report['categories'].items():
            self.assertEqual(row['matchedCount'], row['expectedCount'],
                             f'{cat}: missed {row["missed"]}')

    def test_wrong_state_fails_the_grade(self):
        expected, _ids = benchmark.build_scenario()
        snapshot = benchmark.epistemic_snapshot()
        # Silently pretend the contradiction was never seen: the grade must
        # catch the missing Contradiction and the (still disputed) item.
        snapshot['openContradictions'] = []
        snapshot['disputed'] = []
        report = benchmark.grade_snapshot(snapshot, expected)
        self.assertLess(report['score'], 1.0)
        self.assertTrue(report['categories']['Contradiction']['missed'])

    def test_run_benchmark_end_to_end(self):
        report = benchmark.run_benchmark()
        self.assertEqual(report['score'], 1.0)


if __name__ == '__main__':
    unittest.main()
