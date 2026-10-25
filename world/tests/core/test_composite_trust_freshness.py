"""External-eval ports: composite multi-signal trust gate + HALF_OPEN
circuit-breaker probe.

From the codebase eval (memory: project_external_eval_sep2026): the think tank's
approval/escalation gates were single-signal and its circuit breaker had no
recovery probe. Two sharp, contained ideas were pulled in:

  1. serve._jes_directory_score / _escalation_floor -- the DIRECTOR auto-approval
     path now blends Jev confidence with a per-kind error-history penalty and a
     per-kind risk FLOOR, so a safety-critical escalation kind (a human's own
     "blocked" verdict on a command/pipeline step) is never auto-approved, and a
     kind that Jev keeps failing on needs stronger confidence to win the human's
     delegation. Fail-closed invariant preserved: a classifier failure leaves the
     escalation pending and raises the bar next time.

  2. serve.is_model_circuit_broken / record_model_result -- the model circuit
     breaker now has a HALF_OPEN recovery probe: once the cooldown elapses, a
     single probe is allowed through and only a SUCCESS closes the circuit; a
     probe failure re-opens it with a fresh cooldown.

Hermetic: pure functions + a mocked _call_openrouter_decision_sync; no live Jev,
no network. The "no state mutation beyond the module counters" claim was
wrong: is_model_circuit_broken/record_model_result log model_circuit_broken/
model_circuit_recovered via a real `log_action` call, which -- unless DB_PATH
is redirected below -- writes into whatever real think_tank.db sits at serve.py's
default path. Found: this file alone added 58 real rows (mostly
`model_circuit_broken` for a fake model "m") to a live production think_tank.db
during a routine test run.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402  (sys.path insert above is the repo test convention)
import sim  # noqa: E402
import content  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-composite-trust-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# Composite director trust gate
# ---------------------------------------------------------------------------

def decision_payload(choice, confidence):
    """A fake /api/alpha/decisions response shaped like what _jev_choice expects."""
    return {'answers': {'q': {'choice': choice, 'confidence': confidence}}, 'usage': {'cost': 0.0}}


def _stub_grade(g):
    def _f(*_a, **_k):
        return g
    return _f


class FreshnessProvenanceTests(unittest.TestCase):
    def _seed(self):
        return {
            'agents': {'ada': {'id': 'ada', 'name': 'Ada', 'role': 'engineer'}},
            'completedDeliverables': [],
            'growthPlans': {},
        }

    def test_graded_deliverable_carries_last_reviewed_date(self):
        state = self._seed()
        old = sim._grading_decider
        sim._grading_decider = _stub_grade(7.5)
        try:
            task = {'id': 't1', 'title': 'Build the parser', 'room': 'pressoffice',
                    'taskType': 'feature', 'instructions': 'make it parse'}
            sim._grade_completed_task(state, 'ada', task, 1000)
            self.assertEqual(len(state['completedDeliverables']), 1)
            d = state['completedDeliverables'][0]
            self.assertEqual(d['title'], 'Build the parser')
            self.assertEqual(d['grade'], 7.5)
            # The freshness/anti-rot signal: an in-document review date present
            # and ISO-formatted (ends with Z), NOT an epoch int.
            self.assertIn('lastReviewed', d)
            self.assertRegex(d['lastReviewed'], r'^\d{4}-\d{2}-\d{2}T.+Z$',
                             'lastReviewed must be an ISO-8601 UTC timestamp')
        finally:
            sim._grading_decider = old


