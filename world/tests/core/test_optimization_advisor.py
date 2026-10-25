import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.optimization_advisor as oa
import serve


def _healthy():
    return {
        'checked_at': 1000,
        'db_ok': True,
        'task_assigned_last_24h': 30,
        'task_completed_last_24h': 25,
        'task_fast_completion_rate': 0.9,
        'task_in_flight_last_24h': 1,
        'straight_through_rate': 0.95,
        'rework_loops_last_24h': 0,
        'loop_cost_usd_last_24h': 0.0,
        'work_queue_due_size': 0,
        'agents_count': 6,
        'aging_in_flight_work': 0,
        'open_escalations': 0,
        'red_pipeline_rejects_last_24h': 0,
        'red_pipeline_escapes_last_24h': 0,
        'missing_model_tier_bands': [],
        'bank_over_cap': [],
        'bank_used': 1.0,
        'bank_cap': 10.0,
        'openrouter_credits': {'totalCredits': 100.0, 'totalUsage': 10.0},
        'review_grader_samples': 0,
        'review_grader_kappa': None,
        'ceremony_imbalance_score': 0.0,
    }


class AdviceTests(unittest.TestCase):
    def test_healthy_snapshot_has_no_constraint(self):
        out = oa.build_optimization_advice(_healthy())
        self.assertEqual(out['recommendations'], [])
        self.assertEqual(out['bottleneck'], 'none')

    def test_rework_is_top(self):
        snap = _healthy()
        snap.update({'rework_loops_last_24h': 10,
                     'straight_through_rate': 0.3,
                     'loop_cost_usd_last_24h': 2.5})
        out = oa.build_optimization_advice(snap)
        self.assertEqual(out['bottleneck'], 'rework')
        self.assertEqual(out['recommendations'][0]['priority'], 'high')

    def test_quality_escape_is_critical(self):
        snap = _healthy()
        snap['red_pipeline_escapes_last_24h'] = 1
        out = oa.build_optimization_advice(snap)
        self.assertEqual(out['bottleneck'], 'quality')
        self.assertEqual(out['recommendations'][0]['priority'], 'critical')

    def test_budget_critical(self):
        snap = _healthy()
        snap['openrouter_credits'] = {'totalCredits': 10.0, 'totalUsage': 9.9}
        out = oa.build_optimization_advice(snap)
        self.assertEqual(out['bottleneck'], 'budget')
        self.assertEqual(out['recommendations'][0]['priority'], 'critical')

    def test_sparse_data_recommends_measurement(self):
        snap = _healthy()
        snap.update({'task_assigned_last_24h': 2,
                     'task_completed_last_24h': 1,
                     'task_fast_completion_rate': None,
                     'straight_through_rate': None})
        out = oa.build_optimization_advice(snap)
        self.assertEqual(out['bottleneck'], 'measurement')
        self.assertIn('baseline', out['headline'].lower())

    def test_metrics_and_limitations(self):
        out = oa.build_optimization_advice(_healthy())
        self.assertIn('task_assigned_last_24h', out['metrics'])
        self.assertTrue(out['limitations'])


class _Req:
    cookies = {}


class EndpointTests(unittest.TestCase):
    def test_requires_player_session(self):
        with mock.patch.object(serve, '_require_player_session', return_value=None):
            resp = asyncio.run(serve.optimization_advice(_Req()))
        self.assertEqual(resp.status_code, 401)

    def test_returns_advice(self):
        snap = _healthy()
        with mock.patch.object(serve, '_require_player_session',
                               return_value={'id': 's'}), \
                mock.patch.object(serve, 'compute_health_snapshot',
                                  return_value=snap):
            resp = asyncio.run(serve.optimization_advice(_Req()))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertIn('recommendations', data)
        self.assertIn('bottleneck', data)

    def test_digest_includes_optimization_focus(self):
        md = serve._health_digest_markdown(_healthy())
        self.assertIn('**Optimization focus:**', md)


if __name__ == '__main__':
    unittest.main()
