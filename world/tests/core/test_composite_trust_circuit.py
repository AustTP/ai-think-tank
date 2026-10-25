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


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        serve._model_circuit_state.clear()

    def test_closed_allows_all(self):
        self.assertFalse(serve.is_model_circuit_broken('m'))
        serve.record_model_result('m', success=True)
        self.assertFalse(serve.is_model_circuit_broken('m'))

    def test_trips_open_after_threshold_failures(self):
        for _ in range(serve.CIRCUIT_BREAKER_THRESHOLD - 1):
            serve.record_model_result('m', success=False)
        # Below threshold: still allowed.
        self.assertFalse(serve.is_model_circuit_broken('m'))
        serve.record_model_result('m', success=False)  # hits threshold
        self.assertTrue(serve.is_model_circuit_broken('m'), 'should block after threshold failures')

    def test_half_open_probe_allows_one_recovery_call_after_cooldown(self):
        # Drive it OPEN.
        for _ in range(serve.CIRCUIT_BREAKER_THRESHOLD):
            serve.record_model_result('m', success=False)
        self.assertTrue(serve.is_model_circuit_broken('m'))
        # Expire the cooldown.
        serve._model_circuit_state['m']['open_until'] = time.time() - 1
        # HALF_OPEN -> one probe allowed through.
        self.assertFalse(serve.is_model_circuit_broken('m'), 'probe should be allowed after cooldown')
        # Second call while probe unresolved is still rejected.
        self.assertTrue(serve.is_model_circuit_broken('m'), 'only one probe at a time')
        # Probe succeeds -> circuit CLOSED.
        serve.record_model_result('m', success=True)
        self.assertFalse(serve.is_model_circuit_broken('m'), 'successful probe closes the circuit')

    def test_probe_failure_reopens_with_fresh_cooldown(self):
        for _ in range(serve.CIRCUIT_BREAKER_THRESHOLD):
            serve.record_model_result('m', success=False)
        serve._model_circuit_state['m']['open_until'] = time.time() - 1
        serve.is_model_circuit_broken('m')  # start the probe
        serve.record_model_result('m', success=False)  # probe fails
        self.assertTrue(serve.is_model_circuit_broken('m'), 'failed probe re-opens the circuit')
        self.assertGreater(serve._model_circuit_state['m']['open_until'], time.time(),
                           're-open should set a fresh future cooldown')

    def test_streak_pinned_at_threshold_not_unbounded(self):
        for _ in range(serve.CIRCUIT_BREAKER_THRESHOLD):
            serve.record_model_result('m', success=False)
        # A bunch of failures while already open shouldn't grow the counter forever.
        for _ in range(50):
            serve.record_model_result('m', success=False)
        self.assertEqual(serve._model_circuit_state['m']['consecutive_failures'],
                         serve.CIRCUIT_BREAKER_THRESHOLD)


class JevFailoverTests(unittest.TestCase):
    """_call_openrouter_decision_sync multi-model failover: the
    old policy was 'no circuit breaker for Jev -- only one slug exists, so
    tripping it disables every decision'. That was a symptom of the single-slug
    config. With a comma-separated chain the breaker arms per slug and a dead
    leader FAILS OVER to the next candidate instead of silently degrading every
    decision to deterministic fallbacks. When only ONE slug is configured the
    legacy behavior is preserved exactly (no breaker, no failover)."""

    def setUp(self):
        serve._model_circuit_state.clear()
        serve._set_setting('jev_model', '')  # tests set their own chain
        with serve._db() as conn:
            conn.execute('DELETE FROM decision_tape')
            conn.execute('DELETE FROM decision_cache')
            conn.execute("DELETE FROM settings WHERE key = 'jev_model'")
        # The CLI-driven standby would append its loopback provider URL to
        # every chain here (and this class's _transport reads body['model']);
        # pin it off so every chain under test is exactly what the test set.
        self._stan_patch = unittest.mock.patch.object(
            serve, '_colab_standby_enabled', return_value=False)
        self._stan_patch.start()
        self.addCleanup(self._stan_patch.stop)

    def _set_chain(self, *slugs):
        serve._set_setting('jev_model', ','.join(slugs))

    @staticmethod
    def _ok_answer(choice='allow', confidence=0.95, cost=0.01):
        return {'answers': {'choice': {'choice': choice, 'confidence': confidence,
                                       'probabilities': {}}},
                'usage': {'cost': cost}, 'vendor_reply': True}

    def _transport(self, failures=()):
        """A fake `_urlopen_with_resilience`: raises for the given slugs,
        returns a canned decision for everyone else. Returns the mock."""
        called = []

        def handler(req, timeout):
            body = json.loads(req.data.decode())
            called.append(body['model'])
            if body['model'] in failures:
                raise ConnectionError(f'{body["model"]} down')
            return json.dumps(self._ok_answer()).encode()

        return unittest.mock.patch.object(serve, '_urlopen_with_resilience', side_effect=handler), called

    def _chain_tape_rows(self):
        with serve._db() as conn:
            return conn.execute('SELECT model, ok, raw FROM decision_tape ORDER BY ts').fetchall()

    def test_failover_serves_the_second_slug_when_the_primary_raises(self):
        self._set_chain('primary-x', 'fallback-y')
        patcher, _ = self._transport(failures=('primary-x',))
        with patcher:
            data = serve._call_openrouter_decision_sync(
                'primary-x', {'messages': []},
                {'choice': {'type': 'choice', 'instructions': 'go', 'criteria': {'a': 'A'}}})
        self.assertEqual(data['answers']['choice']['choice'], 'allow',
                         'the fallback slug answered, not the dead primary')
        rows = self._chain_tape_rows()
        self.assertEqual(len(rows), 1, 'one decision = exactly one tape row')
        self.assertEqual((rows[0][0], rows[0][1]), ('fallback-y', 1),
                         'a fallback-recovered decision tapes the ANSWERING slug as ok')
        self.assertEqual(serve._model_circuit_state.get('primary-x', {}).get('consecutive_failures'), 1,
                         'the primary accumulates a breaker failure; the fallback got it right')

    def test_cold_open_leader_is_skipped_and_never_called(self):
        self._set_chain('primary-x', 'fallback-y')
        serve._model_circuit_state['primary-x'] = {
            'consecutive_failures': serve.CIRCUIT_BREAKER_THRESHOLD,
            'open_until': time.time() + 1000, 'probing': False}
        patcher, called = self._transport()
        with patcher:
            data = serve._call_openrouter_decision_sync('primary-x', {}, {'choice': {}})
        self.assertEqual(called, ['fallback-y'],
                         'a cold-open leader must be skipped, not hammered')
        self.assertEqual(data['answers']['choice']['choice'], 'allow')

    def test_single_slug_configuration_keeps_the_legacy_no_breaker_path(self):
        self._set_chain('primary-x')
        patcher, called = self._transport(failures=('primary-x',))
        with patcher:
            with self.assertRaises(ConnectionError):
                serve._call_openrouter_decision_sync('primary-x', {}, {'choice': {}})
        self.assertNotIn('primary-x', serve._model_circuit_state,
                         'one slug = breaker NOT armed = nothing recorded')
        rows = self._chain_tape_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][0], rows[0][1]), ('primary-x', 0))
        raw = json.loads(rows[0][2])
        # The ok=0 row carries the concrete exception so the incident is
        # diagnosable from the tape (kind/status/detail), not just a generic
        # 'decision call raised'.
        self.assertEqual(raw['kind'], 'ConnectionError')

    def test_all_open_fails_closed_without_calling_any_slug(self):
        self._set_chain('primary-x', 'fallback-y')
        for slug in ('primary-x', 'fallback-y'):
            serve._model_circuit_state[slug] = {
                'consecutive_failures': serve.CIRCUIT_BREAKER_THRESHOLD,
                'open_until': time.time() + 1000, 'probing': False}
        patcher, called = self._transport()
        with patcher:
            with self.assertRaisesRegex(RuntimeError, 'circuit-broken'):
                serve._call_openrouter_decision_sync('primary-x', {}, {'choice': {}})
        self.assertEqual(called, [], 'no spend on slugs whose breakers are cold-open')
        rows = self._chain_tape_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 0, 'the lost decision is taped for the failure-rate signal')

    def test_chain_resolution_db_over_env_and_env_over_default(self):
        self._set_chain('db-a', 'db-b')
        self.assertEqual(serve._decision_model_chain(), ['db-a', 'db-b'])
        self.assertEqual(serve._jev_model(), 'db-a')
        # DB unset -> env JEV_MODELS wins.
        with serve._db() as conn:
            conn.execute("DELETE FROM settings WHERE key = 'jev_model'")
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={'JEV_MODELS': 'env-a, env-b'}):
            self.assertEqual(serve._decision_model_chain(), ['env-a', 'env-b'])
        # Neither DB nor env -> the single default constant (no failover).
        with unittest.mock.patch.object(serve, '_load_env', return_value={}):
            self.assertEqual(serve._decision_model_chain(), [serve.JEV_MODEL])


