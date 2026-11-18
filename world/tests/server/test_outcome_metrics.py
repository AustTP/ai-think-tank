"""Outcome metrics tests (coherence Priority 5): the readout is computed
from already-recorded data, missing metrics stay honestly 'unavailable',
and the route-shadow agreement rate (the promotion signal for the foundry
net) is counted from action_log. Hermetic: ledger + spend + DB seams
patched in-memory."""

import json
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import serve  # noqa: E402
import core.evidence as evidence  # noqa: E402
import core.epistemics as epistemics  # noqa: E402


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _sql, _params=None):
        return self

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _seed_and_patch(shadow_rows=(), spend=None):
    """Patch every seam FIRST (in-memory ledger), then seed through the
    real APIs. Ordering matters: seeding through the live DB seam would
    pollute across tests (the ledger dedupes by text and its kv row
    persists), so each test would see the previous test's validations."""
    stores = {'ledger': {}, 'evidence': {}, 'coverage': {}}
    patchers = [
        unittest.mock.patch('core.epistemics._ledger_read',
                            lambda: stores['ledger']),
        unittest.mock.patch('core.epistemics._ledger_write',
                            lambda blob: stores.__setitem__('ledger', blob)),
        unittest.mock.patch('core.evidence._evidence_read',
                            lambda: stores['evidence']),
        unittest.mock.patch('core.evidence._evidence_write',
                            lambda rows: stores.__setitem__(
                                'evidence', rows)),
        unittest.mock.patch('core.evidence._coverage_read',
                            lambda: stores['coverage']),
        unittest.mock.patch('core.evidence._coverage_write',
                            lambda rows: stores.__setitem__(
                                'coverage', rows)),
        unittest.mock.patch('core.evidence.append_audit',
                            return_value=None),
        unittest.mock.patch('serve._db',
                            return_value=_FakeConn(shadow_rows)),
        unittest.mock.patch('serve._spend_ledger_read',
                            return_value=spend or {}),
    ]
    for p in patchers:
        p.start()
    item = epistemics.record_assertion('The bus route 11 runs on Sundays',
                                       actor='ada')
    claim = evidence.record_source_claim(
        {'source_url': 'https://transit.example/11',
         'claim': 'Schedule shows Sunday service'}, actor='ada')
    v = epistemics.propose_validation(item['id'], 'check the schedule',
                                      actor='ada')
    epistemics.start_validation(v['id'], cost_ok=True, actor='ada')
    epistemics.complete_validation(v['id'], 'Sunday service confirmed',
                                   claim_id=claim['id'], actor='ben')
    epistemics.review_validation(v['id'], True, reviewer='cid')
    epistemics.record_unknown('Is route 22 ADA compliant?', actor='eli')
    return patchers


class OutcomeMetrics(unittest.TestCase):
    def test_review_gate_rates_and_unavailable_honesty(self):
        patchers = _seed_and_patch()
        try:
            out = serve._outcome_metrics()
            m = out['metrics']
            self.assertEqual(m['knowledge']['establishedCount'], 1)
            self.assertEqual(m['validation']['reviewGateRate'], 1.0)
            self.assertEqual(m['validation']['byStatus'].get('reviewed'), 1)
            # The three not-yet-derivable metrics are named, not faked.
            names = {u['metric'] for u in m['unavailable']}
            self.assertEqual(names, {'taskReopenRate', 'duplicateWorkRate',
                                     'humanAcceptanceRate'})
        finally:
            for p in patchers:
                p.stop()

    def test_shadow_agreement_counted_from_action_log(self):
        rows = [
            (json.dumps({'agree': True, 'liveLane': 'story',
                         'netLane': 'story'}),),
            (json.dumps({'agree': False, 'liveLane': 'ask',
                         'netLane': 'story'}),),
            (json.dumps({'agree': True, 'liveLane': 'spike',
                         'netLane': 'spike'}),),
        ]
        patchers = _seed_and_patch(shadow_rows=rows)
        try:
            out = serve._outcome_metrics()
            self.assertEqual(out['metrics']['routingShadow'],
                             {'shadowRows': 3, 'agree': 2,
                              'agreeRate': 0.667})
        finally:
            for p in patchers:
                p.stop()

    def test_no_shadow_rows_reported_as_unknown_not_zero(self):
        patchers = _seed_and_patch(shadow_rows=[])
        try:
            out = serve._outcome_metrics()
            self.assertEqual(out['metrics']['routingShadow'],
                             {'shadowRows': 0, 'agreeRate': None})
        finally:
            for p in patchers:
                p.stop()

    def test_spend_totals_from_the_ledger(self):
        patchers = _seed_and_patch(spend={
            'task:t-1': {'used': 0.125, 'calls': 3},
            'task:t-2': {'used': 0.375, 'calls': 7},
        })
        try:
            out = serve._outcome_metrics()
            self.assertEqual(out['metrics']['spend']['trackedSpendUsd'], 0.5)
            self.assertEqual(out['metrics']['spend']['pricedCalls'], 10)
        finally:
            for p in patchers:
                p.stop()


class EndpointGate(unittest.TestCase):
    def test_metrics_requires_operator(self):
        from starlette.testclient import TestClient
        response = TestClient(serve.app).get('/api/outcome-metrics')
        self.assertEqual(response.status_code, 401)
