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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

class EscalationFloorTests(unittest.TestCase):
    def test_known_kinds_have_expected_floors(self):
        self.assertEqual(serve._escalation_floor('blocked command'), 1.0)          # human's own blocked verdict -> never auto
        self.assertEqual(serve._escalation_floor('blocked pipeline step'), 1.0)    # same
        self.assertGreater(serve._escalation_floor('unsure safety decision'),       # uncertain-by-construction kind raised
                           serve.JEV_SAFETY_CONFIDENCE)
        self.assertEqual(serve._escalation_floor('unresolved review requirement'),
                         serve.JEV_SAFETY_CONFIDENCE)                               # routine kind keeps the old bar
        self.assertEqual(serve._escalation_floor('allowlist request'), 1.0)   # permanent capability grant -> always human

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


class DirectorAutoApprovalTests(unittest.TestCase):
    def _run_resolver(self, esc, decision=None, side_effect=None):
        """Call _resolve_pending_escalations_sync with a single escalation, mocking
        the Jev decision call and the director/state plumbing. `decision` lets a
        test supply a custom /api/alpha/decisions payload; `side_effect` overrides
        it with a raised exception (fail-closed path). The esc dict is mutated in
        place by the resolver.

        Gap caught: this used to assign serve.get_state_from_db /
        _load_escalations / _save_escalations directly (`serve.x = Mock(...)`),
        with NO restoration afterward -- once any test in this class ran, those
        three stayed permanently mocked for the rest of the process, breaking any
        LATER test (in this file or another) that needed the real functions.
        Seen via PeerReviewWorkerPickerTests failing only when run as part
        of the whole file, never in isolation. Now properly scoped + auto-restored
        via addCleanup, same as every other mock in this codebase."""
        pending_id = 'esc-test'
        for name, mock_value in (
            ('_load_escalations', unittest.mock.Mock(return_value={pending_id: esc})),
            ('_save_escalations', unittest.mock.Mock()),
            ('get_state_from_db', unittest.mock.Mock(return_value={
                'agentRoster': [{'id': 'a', 'name': 'Ada', 'isDirector': True, 'isAdmin': False}]})),
        ):
            patcher = unittest.mock.patch.object(serve, name, mock_value)
            patcher.start()
            self.addCleanup(patcher.stop)
        if side_effect is not None:
            patch = unittest.mock.patch('serve._call_openrouter_decision_sync', side_effect=side_effect)
        else:
            patch = unittest.mock.patch('serve._call_openrouter_decision_sync',
                                        return_value=decision or decision_payload('approve', 0.95))
        with patch:
            serve._resolve_pending_escalations_sync()
        return esc

    def test_blocked_command_never_auto_approved(self):
        # A human's own "blocked command" verdict is 1.0 floor: the director is
        # not permitted to resolve it AT ALL. It's skipped before
        # any Jev call -- stays pending for the human, and no auto-approval
        # (or even an unsure-decision log) happens for it.
        esc = {'status': 'pending', 'kind': 'blocked command', 'question': 'run rm -rf'}
        with unittest.mock.patch('serve.log_action') as log, \
             unittest.mock.patch('serve._call_openrouter_decision_sync') as jev:
            self._run_resolver(esc, decision=decision_payload('approve', 0.99))
        self.assertEqual(esc['status'], 'pending', 'blocked command must not be auto-approved')
        jev.assert_not_called()  # skipped entirely -- no Jev round trip wasted

    def test_routine_kind_auto_approved_at_high_confidence(self):
        esc = {'status': 'pending', 'kind': 'unresolved review requirement', 'question': 'docs?', 'resolvedBy': '', 'resolvedAt': 0}
        self._run_resolver(esc)
        self.assertEqual(esc['status'], 'approved', 'routine kind at high confidence should auto-approve')

    def test_low_confidence_routine_kind_stays_pending(self):
        esc = {'status': 'pending', 'kind': 'unresolved review requirement', 'question': 'docs?'}
        self._run_resolver(esc, decision=decision_payload('approve', 0.5))
        self.assertEqual(esc['status'], 'pending', 'low-confidence routine should stay for the human')

    def test_classifier_failure_fails_closed_and_bumps_error(self):
        esc = {'status': 'pending', 'kind': 'unresolved review requirement', 'question': 'docs?'}
        self._run_resolver(esc, side_effect=RuntimeError('jev down'))
        self.assertEqual(esc['status'], 'pending', 'Jev outage must never auto-decide')
        self.assertGreaterEqual(serve._escalation_jev_errors.get('unresolved review requirement'), 1,
                                'a Jev failure should raise the error-history bar')

    def _assert_escalation_unsure(self, log):
        kinds = [c[0][1] for c in log.call_args_list if c[0] and c[0][1] == 'escalation_unsure']
        self.assertTrue(kinds, 'should have logged an escalation_unsure decision')


def decision_payload(choice, confidence):
    """A fake /api/alpha/decisions response shaped like what _jev_choice expects."""
    return {'answers': {'q': {'choice': choice, 'confidence': confidence}}, 'usage': {'cost': 0.0}}


# ---------------------------------------------------------------------------
# Half-open circuit breaker probe
# ---------------------------------------------------------------------------

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


class ColabStandbyTests(unittest.TestCase):
    """The CLI-managed Colab/Laya standby: a dedicated CPU Colab
    session ('think-tank-standby') owned via the colab CLI, laya-serve booted over
    `colab exec`, reached through a LOCALHOST-only ssh forward at
    127.0.0.1:8939, driven by the 15-min _colab_failover_loop. No sentinel
    notebook, no /api/colab/register, no pairing. Hermetic: _colab_cli /
    _colab_session_exists / subprocess.Popen are mocked; the only real call is
    the loopback /health check under fake urlopen."""

    def setUp(self):
        serve._model_circuit_state.clear()
        with serve._db() as conn:
            conn.execute('DELETE FROM decision_tape')

    def _seed_jev(self, failures, successes):
        for _ in range(failures):
            serve._append_decision_tape('probe', 'typesafe/jev-1.13', '', '', None, None, None, {}, False)
        for _ in range(successes):
            serve._append_decision_tape('probe', 'typesafe/jev-1.13', '', '', None, None, None, {}, True)

    def test_provider_chain_entry_appends_the_standby_url_last(self):
        serve._set_setting('jev_model', 'primary-x')
        self.assertTrue(serve.COLAB_STANDBY_URL.startswith('http://127.0.0.1:'),
                        'the standby is loopback-only by construction')
        with unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', True):
            self.assertEqual(serve._decision_model_chain(),
                             ['primary-x', serve.COLAB_STANDBY_URL + serve.COLAB_STANDBY_DECISION_PATH],
                             'the standby trails the chain as the LAST fallback')
        with unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', False):
            self.assertEqual(serve._decision_model_chain(), ['primary-x'],
                             'standby disabled -> no provider in the chain')
        self.assertEqual(serve._jev_model(), 'primary-x',
                         'the primary slug still leads; _jev_model is unchanged')

    def test_chain_dedupes_a_manually_configured_provider(self):
        provider = serve.COLAB_STANDBY_URL + serve.COLAB_STANDBY_DECISION_PATH
        serve._set_setting('jev_model', 'primary-x,' + provider)
        with unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', True):
            self.assertEqual(serve._decision_model_chain(), ['primary-x', provider],
                             'an already-configured standby URL is not appended twice')

    def test_provider_request_is_direct_not_openrouter(self):
        provider = serve.COLAB_STANDBY_URL + serve.COLAB_STANDBY_DECISION_PATH
        req = serve._decision_request(provider, {'messages': []}, {'choice': {}})
        self.assertEqual(req.full_url, provider)
        self.assertIsNone(req.get_header('Authorization'),
                          'the loopback standby needs no bearer key')
        body = json.loads(req.data.decode())
        self.assertNotIn('model', body, 'laya routes internally; no slug is sent')
        self.assertIn('state', body)
        self.assertIn('questions', body)

    def test_jev_is_degraded_reuses_the_health_predicate(self):
        self._seed_jev(failures=6, successes=6)  # 12 attempts at exactly 50%
        self.assertTrue(serve._jev_is_degraded())
        with serve._db() as conn:
            conn.execute('DELETE FROM decision_tape')
        self._seed_jev(failures=6, successes=12)  # 33% -> healthy
        self.assertFalse(serve._jev_is_degraded())
        with serve._db() as conn:
            conn.execute('DELETE FROM decision_tape')
        self._seed_jev(failures=4, successes=0)  # all failed but sparse
        self.assertFalse(serve._jev_is_degraded(),
                         'a handful of attempts never trips the standby')

    def test_ensure_session_provisions_when_missing(self):
        calls = []
        with unittest.mock.patch.object(serve, '_colab_session_exists', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_cli',
                                        side_effect=lambda *a, **k: calls.append(a) or (0, '')):
            ok, msg = serve._colab_standby_ensure_session()
        self.assertTrue(ok, msg)
        self.assertEqual(calls, [('new', '-s', serve.COLAB_STANDBY_SESSION)],
                         'a missing standby session is created via the CLI')

    def test_ensure_session_reuses_an_existing_one(self):
        with unittest.mock.patch.object(serve, '_colab_session_exists', return_value=True), \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            ok, _ = serve._colab_standby_ensure_session()
        self.assertTrue(ok)
        cli.assert_not_called(), 'an existing session is not recreated'

    def test_boot_code_installs_and_starts_laya(self):
        calls = []
        with unittest.mock.patch.object(serve, '_colab_cli',
                                        side_effect=lambda *a, **k: calls.append((a, k)) or (0, '')):
            serve._colab_standby_ensure_service()
        args, kwargs = calls[0]
        self.assertEqual(args, ('exec', '-s', serve.COLAB_STANDBY_SESSION, '--timeout', '600'))
        payload = kwargs['input']
        self.assertIn('laya[serve]', payload)
        self.assertIn('laya-serve', payload)
        self.assertIn('__STANDBY_UP__', payload)

    def test_teardown_kills_the_laya_process_only(self):
        calls = []
        with unittest.mock.patch.object(serve, '_colab_cli',
                                        side_effect=lambda *a, **k: calls.append((a, k)) or (0, '')):
            serve._colab_standby_teardown_service()
        payload = calls[0][1]['input']
        self.assertIn('pkill', payload)
        self.assertIn('laya-serve', payload)

    def test_forward_spawned_once_and_reused_for_the_same_session(self):
        fake = unittest.mock.Mock(pid=111)
        with unittest.mock.patch.object(serve.subprocess, 'Popen', return_value=fake) as popen, \
             unittest.mock.patch('os.kill', return_value=None):
            serve._COLAB_STANDBY_FORWARD.clear()
            first = serve._colab_standby_ensure_forward('think-tank-standby')
            second = serve._colab_standby_ensure_forward('think-tank-standby')
        self.assertIs(first, second)
        self.assertEqual(popen.call_count, 1, 'a live forward is reused, not respawned')
        cmd = popen.call_args[0][0]
        self.assertIn('ssh', cmd)
        self.assertTrue(any(c == '-L' and n == f'127.0.0.1:{serve.COLAB_STANDBY_PORT}:localhost:8000'
                            for c, n in zip(cmd, cmd[1:])), 'forward targets the loopback port')
        self.assertTrue(any('ProxyCommand' in c and '--proxy-mode' in c for c in cmd),
                        'the forward rides the colab CLI proxy-mode bridge')
        self.assertIn('-N', cmd)
        self.assertTrue(any('User=root' in c for c in cmd))
        serve._colab_standby_stop_forward()

    def test_forward_respawns_when_the_old_one_died(self):
        fake = unittest.mock.Mock(pid=999)
        with unittest.mock.patch.object(serve.subprocess, 'Popen', return_value=fake) as popen, \
             unittest.mock.patch('os.kill', return_value=None):      # alive: reuse
            serve._COLAB_STANDBY_FORWARD.clear()
            serve._colab_standby_ensure_forward('think-tank-standby')
            serve._colab_standby_ensure_forward('think-tank-standby')
            self.assertEqual(popen.call_count, 1)
        with unittest.mock.patch.object(serve.subprocess, 'Popen', return_value=fake) as popen, \
             unittest.mock.patch('os.kill', side_effect=ProcessLookupError('gone')):  # dead: respawn
            serve._colab_standby_ensure_forward('think-tank-standby')
            self.assertEqual(popen.call_count, 1, 'a dead forward is recognised and respawned')
        serve._colab_standby_stop_forward()

    def test_standby_reachable_checks_the_loopback_health(self):
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen') as u:
            u.return_value.__enter__.return_value.status = 200
            self.assertTrue(serve._colab_standby_reachable())
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=OSError('nothing on the port')):
            self.assertFalse(serve._colab_standby_reachable())
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen') as u:
            u.return_value.__enter__.return_value.status = 503
            self.assertFalse(serve._colab_standby_reachable(),
                             'a non-200 /health is not a standing Laya')

    def test_standby_enabled_gates_on_cli_switch_and_opt_in(self):
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', True):
            self.assertTrue(serve._colab_standby_enabled())
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, 'COLAB_ENABLED', False), \
             unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', True):
            self.assertFalse(serve._colab_standby_enabled(),
                             'COLAB_ENABLED=0 switches the whole standby off')
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', False), \
             unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', True):
            self.assertFalse(serve._colab_standby_enabled(),
                             'no CLI -> no standby, no chain entry')
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', False):
            self.assertFalse(serve._colab_standby_enabled(),
                             'COLAB_STANDBY_ENABLED is a deliberate opt-in')


class ColabComputeTests(unittest.TestCase):
    """run_on_colab agent compute: the dedicated 'think-tank-gpu'
    T4 session, provisioned/ran/stood-down through the colab CLI, metered as
    monthly compute units in the Bank. Hermetic: _colab_cli/_provision are
    mocked, so no CLI, no session, no network ever actually runs."""

    def setUp(self):
        serve._model_circuit_state.clear()

    def test_operator_switch_off_refuses_runs(self):
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', False), \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            result = serve._colab_compute_run('agent-0', 'print(1)', 'probe', [], 60)
        self.assertIn('disabled by the operator', result['error'])
        cli.assert_not_called()

    def test_drive_mount_code_is_refused(self):
        with unittest.mock.patch.object(serve, '_colab_cli') as cli:
            result = serve._colab_compute_run(
                'agent-0', 'from google.colab import drive\ndrive.mount("/content/drive")\n',
                'numeric job', [], 60)
        self.assertIn('refusing to run', result['error'])
        self.assertIn('off-limits', result['error'])
        cli.assert_not_called()

    def test_gcloud_and_mining_code_are_refused(self):
        for bad in ('gsutil cp local gs://bucket/x', 'nicehash miner loop', 'gdown 1abc'):
            with unittest.mock.patch.object(serve, '_colab_cli') as cli:
                result = serve._colab_compute_run('agent-0', bad, 'job', [], 60)
            self.assertIn('refusing to run', result['error'], bad)
            cli.assert_not_called()

    def test_exfil_package_is_refused(self):
        with unittest.mock.patch.object(serve, '_colab_cli') as cli:
            result = serve._colab_compute_run(
                'agent-0', 'print(1)', 'job', ['gdown', 'requests'], 60)
        self.assertIn('package', result['error'])
        self.assertIn('refusing to run', result['error'])
        cli.assert_not_called()

    def test_plain_numeric_code_passes_the_guard(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', True), \
             unittest.mock.patch.object(serve, '_colab_account_usage', return_value=None), \
             unittest.mock.patch.object(serve, '_colab_compute_provision', return_value=(True, 'ok')), \
             unittest.mock.patch.object(serve, '_colab_cli', return_value=(0, '42\n__COLAB_DONE__\n')):
            result = serve._colab_compute_run('agent-0', 'a = 41\nprint(a + 1)', 'sum', [], 60)
        self.assertEqual(result['stdout'], '42')

    def test_budget_gate_refuses_when_gated_out(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 20.0), \
             unittest.mock.patch.object(serve, '_colab_spend_this_month', return_value=25.0), \
             unittest.mock.patch.object(serve, '_colab_account_usage', return_value=None), \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            result = serve._colab_compute_run('agent-0', 'print(1)', 'probe', [], 60)
        self.assertIn('COLAB_MONTHLY_UNITS', result['error'])
        cli.assert_not_called(), 'gated out means no provisioning attempt at all'

    def test_exhausted_paid_account_balance_refuses_run(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', False), \
             unittest.mock.patch.object(serve, '_colab_account_usage',
                                        return_value={'balance': 0.0, 'rate': 1.15, 'assignments': 1}), \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            result = serve._colab_compute_run('agent-0', 'print(1)', 'probe', [], 60)
        self.assertIn('no prepaid compute units left', result['error'])
        cli.assert_not_called()

    def test_free_tier_zero_balance_proceeds_to_provision(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', True), \
             unittest.mock.patch.object(serve, '_colab_account_usage',
                                        return_value={'balance': 0.0, 'rate': 1.15, 'assignments': 2}), \
             unittest.mock.patch.object(serve, '_colab_compute_provision', return_value=(True, 'ok')) as prov, \
             unittest.mock.patch.object(serve, '_colab_cli', return_value=(0, '12\n__COLAB_DONE__\n')):
            result = serve._colab_compute_run('agent-0', 'print(1)', 'probe', [], 60)
        self.assertEqual(result['stdout'], '12')
        prov.assert_called_once()

    def test_empty_and_oversized_code_rejected_without_cli(self):
        with unittest.mock.patch.object(serve, '_colab_cli') as cli:
            self.assertIn('code is required', serve._colab_compute_run('agent-0', '', 'probe', [], 60)['error'])
            big = 'x' * (serve.COLAB_CODE_MAX_CHARS + 1)
            self.assertIn('under', serve._colab_compute_run('agent-0', big, 'probe', [], 60)['error'])
        cli.assert_not_called()

    def test_provisions_gpu_session_with_t4_on_demand(self):
        calls = []
        with unittest.mock.patch.object(serve, '_colab_session_exists', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_cli', side_effect=lambda *a, **k: calls.append((a, k)) or (0, '')):
            ok, msg = serve._colab_compute_provision()
        self.assertTrue(ok)
        new_call = next(a for a, _k in calls if a[0] == 'new')
        self.assertEqual(new_call[1:], ('-s', 'think-tank-gpu', '--gpu', 'T4'),
                         'the dedicated session is a T4 created on demand')

    def test_reuses_existing_session(self):
        with unittest.mock.patch.object(serve, '_colab_session_exists', return_value=True), \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            ok, msg = serve._colab_compute_provision()
        self.assertTrue(ok)
        cli.assert_not_called(), 'an existing session is reused without any CLI call'

    def test_successful_run_returns_stdout_and_accrues_units(self):
        accrued = {}
        with unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_compute_provision', return_value=(True, 'ok')), \
             unittest.mock.patch.object(serve, '_colab_cli', return_value=(0, 'answer=42\n__COLAB_DONE__')) as cli, \
             unittest.mock.patch.object(serve, '_accrue_colab_units',
                                        side_effect=lambda u: accrued.setdefault('units', u)), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._colab_compute_run('agent-0', 'print("answer=42")', 'probe', [], 60)
        self.assertEqual(result['stdout'], 'answer=42')
        self.assertGreaterEqual(result['units'], serve.COLAB_MIN_UNITS_PER_RUN)
        self.assertEqual(accrued['units'], result['units'])
        self.assertEqual(result['session'], 'think-tank-gpu')
        exec_call = cli.call_args.args
        self.assertIn('exec', exec_call)
        self.assertIn('__COLAB_DONE__', cli.call_args.kwargs.get('input', ''))

    def test_host_side_timeout_exceeds_cli_timeout_on_exec(self):
        # The Colab CLI carries its own --timeout for the remote exec, but the
        # host-side _colab_cli subprocess defaults to a 120s timeout -- a 120s
        # default would kill a long transcription (CLI --timeout up to 630s)
        # before the CLI finished or reported. The host timeout must always
        # exceed the CLI's own --timeout, so the CLI (not the host) decides
        # when a remote exec is done.
        calls = []
        def fake_cli(*a, **k):
            calls.append((a, k))
            return (0, 'ok\n__COLAB_DONE__\n')
        with unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_compute_provision', return_value=(True, 'ok')), \
             unittest.mock.patch.object(serve, '_colab_cli', side_effect=fake_cli), \
             unittest.mock.patch.object(serve, '_accrue_colab_units'), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._colab_compute_run('agent-0', 'print(1)', 'transcribe', [], 600)
        self.assertEqual(result['stdout'], 'ok')
        exec_calls = [c for c in calls if c[0][0] == 'exec']
        self.assertTrue(exec_calls, 'a successful run must exec the code')
        for args, kwargs in exec_calls:
            self.assertIn('--timeout', args)
            cli_timeout = int(args[args.index('--timeout') + 1])
            self.assertGreater(kwargs.get('timeout', 0), cli_timeout,
                               'host subprocess timeout must exceed the CLI --timeout or the host kills long execs')

    def test_failed_cli_run_returns_honest_error(self):
        with unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_compute_provision', return_value=(True, 'ok')), \
             unittest.mock.patch.object(serve, '_colab_cli', return_value=(1, 'Traceback')), \
             unittest.mock.patch.object(serve, '_accrue_colab_units'), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._colab_compute_run('agent-0', '1/0', 'probe', [], 60)
        self.assertIn('exit 1', result['error'])

    def test_executor_formats_error_for_the_model(self):
        with unittest.mock.patch.object(serve, '_colab_compute_run',
                                        return_value={'error': 'boom'}):
            executor = content._make_colab_compute_executor('a1')
            out = executor('run_on_colab', {'code': 'print(1)'})
        self.assertIn('__TOOL_ERROR__', out)
        self.assertIn('boom', out)

    def test_redteam_code_is_refused_on_colab(self):
        # Offensive-security/scan work runs in the local
        # Work Room sandbox, NEVER on the player's real Google Colab account.
        for bad in ('nmap -sV example.com', 'msfconsole -q', 'sqlmap -u http://x/page?id=1',
                    'hydra -l admin ssh://host', 'nuclei -u https://victim.dev',
                    'exfiltrate / proc/self/maps over a reverse shell'):
            with unittest.mock.patch.object(serve, '_colab_cli') as cli:
                result = serve._colab_compute_run('agent-0', bad, 'probe', [], 60)
            self.assertIn('refusing to run', result['error'], bad)
            self.assertIn('off-limits', result['error'], bad)
            cli.assert_not_called()

    def test_colab_target_hosts_extracts_urls(self):
        hosts = serve._colab_target_hosts(
            'r = requests.get("https://api.example.com/v1/data")  # https://cdn.example.com/x.png\n',
            'pull from https://finance.example.org quote page')
        self.assertEqual(hosts, {'api.example.com', 'cdn.example.com', 'finance.example.org'})

    def test_colab_gate_urls_allowlisted_host_skips_jev(self):
        with unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_choice_sync') as jev:
            reason = serve._colab_gate_urls('agent-0', 'https://dreyx.com/data', 'pull it')
        self.assertIsNone(reason)
        jev.assert_not_called()

    def test_colab_gate_urls_private_host_is_refused(self):
        with unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=False), \
             unittest.mock.patch.object(serve, '_jev_quorum_choice_sync') as jev:
            reason = serve._colab_gate_urls('agent-0', 'https://10.0.0.5/secret', 'call it')
        self.assertIn('private or internal', reason)
        jev.assert_not_called()

    def test_colab_gate_urls_low_confidence_jev_is_refused(self):
        with unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_choice_sync',
                                        return_value=(None, 1.0, 0.0)):
            reason = serve._colab_gate_urls('agent-0', 'https://example.com/x', 'fetch it')
        self.assertIsNotNone(reason)
        self.assertIn('not approved', reason)

    def test_colab_gate_urls_confident_allow_passes(self):
        calls = {'n': 0}
        def fake_gate(agent_id, action, noun, target, purpose, decision, confidence, cost, authorized, trace_id=None):
            calls['n'] += 1
            return True
        with unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_choice_sync',
                                        return_value=('allow', 0.95, 0.0)), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', side_effect=fake_gate), \
             unittest.mock.patch.object(serve, 'record_browse_success'):
            reason = serve._colab_gate_urls('agent-0', 'https://example.com/x', 'fetch it')
        self.assertIsNone(reason)
        self.assertEqual(calls['n'], 1)

    def test_colab_gate_urls_denial_blocked_by_jev_categories(self):
        # A host Jev says belongs to a blocked category never runs.
        def fake_gate(agent_id, action, noun, target, purpose, decision, confidence, cost, authorized, trace_id=None):
            return False
        with unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_choice_sync',
                                        return_value=('block', 0.9, 0.0)), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', side_effect=fake_gate):
            reason = serve._colab_gate_urls('agent-0', 'https://archive.org/x', 'backup media')
        self.assertIsNotNone(reason)
        self.assertIn('not approved', reason)

    def test_colab_compute_run_refuses_url_gated_job_before_provision(self):
        with unittest.mock.patch.object(serve, '_colab_gate_urls',
                                        return_value='host was blocked'), \
             unittest.mock.patch.object(serve, '_colab_compute_provision') as prov, \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            result = serve._colab_compute_run('agent-0', 'https://example.com/x', 'fetch it', [], 60)
        self.assertIn('refusing to run', result['error'])
        self.assertIn('host was blocked', result['error'])
        prov.assert_not_called(), 'a gated job never provisions a session'
        cli.assert_not_called()

    def test_account_usage_parses_colab_usage_output(self):
        raw = ('Current balance: 4.25 compute units\n'
               'Usage rate: 1.15/hr\n'
               'Active assignments: 2')
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_cli', return_value=(0, raw)):
            usage = serve._colab_account_usage()
        self.assertEqual(usage['balance'], 4.25)
        self.assertEqual(usage['rate'], 1.15)
        self.assertEqual(usage['assignments'], 2)

    def test_budget_exceeded_when_real_account_balance_is_zero(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', False), \
             unittest.mock.patch.object(serve, '_colab_account_usage',
                                        return_value={'balance': 0.0, 'rate': 1.15, 'assignments': 1}):
            self.assertTrue(serve._colab_budget_exceeded(),
                            'a genuinely exhausted paid account gates runs even with no think tank cap')

    def test_free_tier_zero_balance_is_not_a_gate(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', True), \
             unittest.mock.patch.object(serve, '_colab_account_usage',
                                        return_value={'balance': 0.0, 'rate': 1.15, 'assignments': 2}):
            self.assertFalse(serve._colab_budget_exceeded(),
                             'free tier has no prepaid wallet -- 0.00 balance must not block runs')

    def test_budget_not_exceeded_when_balance_exists_and_no_think_tank_cap(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', False), \
             unittest.mock.patch.object(serve, '_colab_account_usage',
                                        return_value={'balance': 4.25, 'rate': 1.15, 'assignments': 2}):
            self.assertFalse(serve._colab_budget_exceeded())
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', False), \
             unittest.mock.patch.object(serve, '_colab_account_usage', return_value=None):
            self.assertFalse(serve._colab_budget_exceeded(),
                             'an unknown balance fails open to the operator cap alone')

    def test_bank_view_seeds_colab_units_row(self):
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 20.0), \
             unittest.mock.patch.object(serve, '_colab_spend_this_month', return_value=3.0), \
             unittest.mock.patch.object(serve, '_colab_account_usage',
                                        return_value={'balance': 4.25, 'rate': 1.15, 'assignments': 2}):
            services = serve._bank_budget_view({})
        row = services[serve.COLAB_LEDGER_KEY]
        self.assertEqual(row['used'], 3.0)
        self.assertEqual(row['cap'], 20.0)
        self.assertEqual(row['left'], 17.0)
        self.assertEqual(row['balance_units'], 4.25)
        self.assertFalse(row['over'])

    def test_budget_cap_reads_units_not_usd(self):
        self.assertEqual(serve._budget_cap_usd(serve.COLAB_LEDGER_KEY),
                         serve.COLAB_MONTHLY_UNITS)


# ---------------------------------------------------------------------------
# Knowledge-rot freshness provenance (graded deliverables carry a Last-reviewed date)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Peer-note writer -- redesigned to be village-natural (2026-09-30)
# ---------------------------------------------------------------------------

class PeerReviewWorkerPickerTests(unittest.TestCase):
    """serve._peer_review_pass was rewritten to be Jev-free and non-deterministic:
    a RANDOM PEER observer watches the real action_log, and when the evidence
    shows a worker genuinely out of line (an underperformer or a standout) a
    weighted-probability pick decides whether THAT watch files a note. No senior
    director always authors every note, no fixed 90s metronome. These tests pin
    the new contract: evidence gates (quiet/nominal village files nothing),
    dedup/staleness coverage, the probability gate, and a random PEER (never
    the target, never the admin/director) authoring the note."""

    def setUp(self):
        # These tests share one global DB, so isolate each one: clear the
        # action_log rows (the evidence the picker reads) and the reports list
        # (the dedup window) so a prior test can't leak its ada/ben activity or
        # its filed reports into the next assertion. Each test then seeds its
        # own exact activity baseline.
        with serve._db() as conn:
            conn.execute('DELETE FROM action_log')
        state = serve.get_state_from_db()
        if state is not None:
            state['reports'] = []
            serve.save_state_to_db(state)

    def _seed_roster_and_activity(self, now):
        state = {
            'agentRoster': [
                {'id': 'maya', 'name': 'Maya', 'isDirector': True, 'isAdmin': False},
                {'id': 'ada', 'name': 'Ada', 'director': 'maya'},
                {'id': 'ben', 'name': 'Ben', 'director': 'maya'},
            ],
            'agents': {'maya': {'id': 'maya'}, 'ada': {'id': 'ada'}, 'ben': {'id': 'ben'}},
            'reports': [],
        }
        serve.save_state_to_db(state)
        # Real action_log rows: ada does plenty of real work, ben does none --
        # a clear, real divergence for a peer to notice. ada = standout
        # (real 5 >= 5 and >= group max), ben = underperformer (real 0 < 2).
        for _ in range(5):
            serve.log_action('ada', 'task_completed', {}, authorized=True)
        return state

    def _force_probability_gate(self):
        """The probability gate files only when random.random() <
        PEER_REVIEW_FILE_PROBABILITY; force the 'file this watch' branch."""
        return unittest.mock.patch.object(serve.random, 'random', return_value=0.0)

    def test_a_random_peer_files_the_note_not_the_director(self):
        state = self._seed_roster_and_activity(time.time())
        with self._force_probability_gate(), \
             unittest.mock.patch.object(serve.random, 'choices',
                                        return_value=[{'id': 'ben', 'name': 'Ben', 'role': '',
                                                       'actions': 0, 'real': 0, 'last': None,
                                                       'already': False}]):
            serve._peer_review_loop_pass()
        saved = serve.get_state_from_db()
        reports = saved.get('reports') or []
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['aboutId'], 'ben')
        # The note is authored by a PEER (a worker), never the senior director
        # (maya), never the admin, and never the target themselves.
        self.assertEqual(reports[0]['fromId'], 'ada')
        self.assertNotEqual(reports[0]['fromId'], 'maya')
        self.assertNotEqual(reports[0]['fromId'], 'ben')

    def test_the_note_is_probabilistic_not_a_fixed_metronome(self):
        # The old design filed a report EVERY cadence (fixed clock). Now the
        # probability gate means a given watch may file nothing even when the
        # evidence is genuine. Patch random.random above the threshold.
        self._seed_roster_and_activity(time.time())
        with unittest.mock.patch.object(serve.random, 'random', return_value=0.99):
            n = serve._peer_review_loop_pass()
        self.assertEqual(n, 0, 'the probability gate must let a genuine watch pass without filing')
        saved = serve.get_state_from_db()
        self.assertEqual(len(saved.get('reports') or []), 0)

    def test_the_probability_gate_still_files_when_it_passes(self):
        # Same evidence, but the watch "happens" -- the note must file.
        self._seed_roster_and_activity(time.time())
        with self._force_probability_gate():
            n = serve._peer_review_loop_pass()
        self.assertEqual(n, 1, 'an evidence-backed watch that passes the gate must file a note')
        saved = serve.get_state_from_db()
        self.assertEqual(len(saved.get('reports') or []), 1)

    def _seed_recent_report(self, about_id, ts_ms=None, severity='major'):
        """Append a report about `about_id` to the stored state. ts_ms defaults
        to 'now' (fresh inside the staleness window)."""
        state = serve.get_state_from_db()
        reports = state.get('reports') or []
        reports.append({
            'id': f'report-seed-{about_id}',
            'aboutId': about_id,
            'fromId': 'maya',
            'quote': 'seeded for the dedup test',
            'note': 'seeded',
            'ts': int(time.time() * 1000) if ts_ms is None else ts_ms,
            'severity': severity,
        })
        state['reports'] = reports
        serve.save_state_to_db(state)

    def test_does_not_re_report_a_worker_within_the_stale_window(self):
        # Both workers covered by a FRESH report -> the loop files nothing.
        # The old behavior fell back to the whole roster and re-flagged the
        # same worker every cycle forever.
        self._seed_roster_and_activity(time.time())
        self._seed_recent_report('ada')
        self._seed_recent_report('ben')
        with self._force_probability_gate():
            n = serve._peer_review_loop_pass()
        self.assertEqual(n, 0, 'an all-covered roster must not file another report')
        saved = serve.get_state_from_db()
        self.assertEqual(len(saved.get('reports') or []), 2)

    def test_a_covered_worker_is_skipped_while_a_fresh_peer_is_flagged(self):
        # ada already fresh-covered; ben is not -> the pool is ben ONLY
        # (freshness-filtered, no `or candidates` fallback) and he gets flagged.
        self._seed_roster_and_activity(time.time())
        self._seed_recent_report('ada')
        with self._force_probability_gate():
            serve._peer_review_loop_pass()
        saved = serve.get_state_from_db()
        reports = saved.get('reports') or []
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[-1]['aboutId'], 'ben',
                         'the fresh candidate, not the already-covered worker, gets the note')

    def test_a_stale_report_allows_a_worker_back_into_the_pool(self):
        # Both workers' only reports are OLDER than the staleness window, so
        # both are fresh again and a new note may be filed.
        self._seed_roster_and_activity(time.time())
        stale_ms = int((time.time() - serve.PEER_REVIEW_REPORT_STALE_S - 60) * 1000)
        self._seed_recent_report('ada', ts_ms=stale_ms)
        self._seed_recent_report('ben', ts_ms=stale_ms)
        with self._force_probability_gate():
            serve._peer_review_loop_pass()
        saved = serve.get_state_from_db()
        self.assertEqual(len(saved.get('reports') or []), 3,
                         'staleness must let a worker back into the pool for a fresh note')

    def test_a_quiet_village_files_no_note_on_a_baseline(self):
        # EVIDENCE GATE: a village where NO ONE has done any real work is just
        # idle -- nobody is underperforming (all equally quiet) and nobody is a
        # standout, so there is nothing legitimate to note. The old behavior
        # auto-filed "Underperforming" every cadence against this baseline,
        # which fabricated evidence. No note must be filed.
        state = {
            'agentRoster': [
                {'id': 'maya', 'name': 'Maya', 'isDirector': True, 'isAdmin': False},
                {'id': 'ada', 'name': 'Ada', 'director': 'maya'},
                {'id': 'ben', 'name': 'Ben', 'director': 'maya'},
            ],
            'agents': {'maya': {'id': 'maya'}, 'ada': {'id': 'ada'}, 'ben': {'id': 'ben'}},
            'reports': [],
        }
        serve.save_state_to_db(state)
        # NOTE: deliberately NO real action_log rows -- a fully quiet village.
        with self._force_probability_gate():
            n = serve._peer_review_loop_pass()
        self.assertEqual(n, 0, 'a fully idle village must not auto-file a peer note')
        saved = serve.get_state_from_db()
        self.assertEqual(len(saved.get('reports') or []), 0)

    def test_a_nominal_village_without_divergence_files_no_note(self):
        # EVIDENCE GATE (target): even when the village is active, a worker at
        # only nominal output (real in [2,4], comparable to peers) is NOT a
        # note -- "for the record" filing was exactly what the user rejected.
        # Only a genuine underperformer (<2 while peers demonstrably worked) or
        # a standout (>=5, the group max) qualifies.
        state = {
            'agentRoster': [
                {'id': 'maya', 'name': 'Maya', 'isDirector': True, 'isAdmin': False},
                {'id': 'ada', 'name': 'Ada', 'director': 'maya'},
                {'id': 'ben', 'name': 'Ben', 'director': 'maya'},
            ],
            'agents': {'maya': {'id': 'maya'}, 'ada': {'id': 'ada'}, 'ben': {'id': 'ben'}},
            'reports': [],
        }
        serve.save_state_to_db(state)
        # Both workers at nominal, comparable real work (2 and 3) -- active but
        # with NO genuine divergence to note.
        for _ in range(2):
            serve.log_action('ada', 'task_completed', {}, authorized=True)
        for _ in range(3):
            serve.log_action('ben', 'task_completed', {}, authorized=True)
        with self._force_probability_gate():
            n = serve._peer_review_loop_pass()
        self.assertEqual(n, 0, 'comparable nominal output is not evidence for a peer note')
        saved = serve.get_state_from_db()
        self.assertEqual(len(saved.get('reports') or []), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)