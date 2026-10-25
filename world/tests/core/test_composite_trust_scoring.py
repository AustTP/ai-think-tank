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


class EscalationFloorTests(unittest.TestCase):
    def test_known_kinds_have_expected_floors(self):
        self.assertEqual(serve._escalation_floor('blocked command'), 1.0)          # human's own blocked verdict -> never auto
        self.assertEqual(serve._escalation_floor('blocked pipeline step'), 1.0)    # same
        self.assertGreater(serve._escalation_floor('unsure safety decision'),       # uncertain-by-construction kind raised
                           serve.JEV_SAFETY_CONFIDENCE)
        self.assertEqual(serve._escalation_floor('unresolved review requirement'),
                         serve.JEV_SAFETY_CONFIDENCE)                               # routine kind keeps the old bar
        # Operator decision 2026-10-13: allowlist requests escalated to the
        # DIRECTORS (their #1 complaint was waiting on email) -- delegable at
        # the default floor, no longer human-only.
        self.assertEqual(serve._escalation_floor('allowlist request'),
                         serve.JEV_SAFETY_CONFIDENCE)

    def test_unknown_kind_falls_back_to_default(self):
        # A new escalation kind can't silently widen auto-approval authority.
        self.assertEqual(serve._escalation_floor('brand new kind'),
                         serve.JEV_SAFETY_CONFIDENCE)


class DirectoryScoreTests(unittest.TestCase):
    def setUp(self):
        serve._escalation_jev_errors.reset('ctx')
        serve._escalation_jev_errors.reset('errh')

    def test_no_errors_score_equals_confidence(self):
        self.assertEqual(serve._jev_directory_score('ctx', 0.9), 0.9)
        self.assertEqual(serve._jev_directory_score('ctx', 0.6), 0.6)

    def test_error_history_penalizes_weighted_by_step(self):
        serve._escalation_jev_errors.bump('errh')   # 1 error -> -0.10
        self.assertAlmostEqual(serve._jev_directory_score('errh', 0.9), 0.8)
        serve._escalation_jev_errors.bump('errh')   # 2 errors -> -0.20
        self.assertAlmostEqual(serve._jev_directory_score('errh', 0.9), 0.7)

    def test_penalty_capped_not_zeroed(self):
        for _ in range(20):
            serve._escalation_jev_errors.bump('errh')
        # Cap at 0.40: even a long bad streak leaves room for a very confident call.
        self.assertAlmostEqual(serve._jev_directory_score('errh', 1.0), 0.6)
        # And can't go below zero.
        self.assertEqual(serve._jev_directory_score('errh', 0.1), 0.0)

    def test_score_clamped_to_unit_interval(self):
        self.assertEqual(serve._jev_directory_score('ctx', 1.4), 1.0)
        self.assertEqual(serve._jev_directory_score('ctx', -0.2), 0.0)


