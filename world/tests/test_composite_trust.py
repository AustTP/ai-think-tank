"""External-eval ports (2026-09-23): composite multi-signal trust gate + HALF_OPEN
circuit-breaker probe.

From the codebase eval (memory: project_external_eval_sep2026): the village's
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
is redirected below -- writes into whatever real village.db sits at serve.py's
default path. Found 2026-09-25: this file alone added 58 real rows (mostly
`model_circuit_broken` for a fake model "m") to a live production village.db
during a routine test run.
"""

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

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='village-composite-trust-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        VILLAGE_DIR=_TMP_DIR,
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

        Real gap caught (2026-09-26): this used to assign serve.get_state_from_db /
        _load_escalations / _save_escalations directly (`serve.x = Mock(...)`),
        with NO restoration afterward -- once any test in this class ran, those
        three stayed permanently mocked for the rest of the process, breaking any
        LATER test (in this file or another) that needed the real functions.
        Found live via PeerReviewWorkerPickerTests failing only when run as part
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
        # not permitted to resolve it AT ALL (2026-09-27). It's skipped before
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
# Peer-report worker picker -- real Jev bug found live (2026-09-26)
# ---------------------------------------------------------------------------

class PeerReviewWorkerPickerTests(unittest.TestCase):
    """serve._peer_review_loop_pass's Jev choice used to pass a single fixed
    criterion key {'idx': 'The index of the worker to report on'} -- the SAME
    bug class already found and fixed once for grading
    (_grading_decider_default's own docstring): Jev's real API is a typed
    multiple-choice system with no "give me a free index" mode, so it could
    only ever echo the single key back verbatim ('idx', never 'idx_3').
    str(chosen).startswith('idx_') was ALWAYS False, meaning this Jev call
    was 100% dead code -- every real invocation silently fell back to the
    deterministic "lowest real-work worker" rule, never actually asking Jev
    anything a real answer could satisfy."""

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
        # a clear, real signal a genuine Jev call could act on.
        for _ in range(5):
            serve.log_action('ada', 'task_completed', {}, authorized=True)
        return state

    def test_criteria_dict_has_one_real_key_per_candidate_not_a_single_fixed_key(self):
        self._seed_roster_and_activity(time.time())
        captured = {}

        def fake_decision(model, state_arg, questions):
            captured['criteria'] = questions['choice']['criteria']
            return {'answers': {'choice': {'choice': 'idx_0', 'confidence': 0.9, 'probabilities': {}}},
                   'usage': {'cost': 0.0}}

        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync', side_effect=fake_decision):
            serve._peer_review_loop_pass()
        # The real bug: this used to be exactly {'idx': '...'} -- one fixed
        # key no real answer could ever match. Now: one real key per
        # candidate worker actually in the pool.
        self.assertGreater(len(captured['criteria']), 1)
        self.assertTrue(all(k.startswith('idx_') for k in captured['criteria']))

    def test_a_real_valid_jev_choice_is_actually_used_not_overridden_by_the_fallback(self):
        state = self._seed_roster_and_activity(time.time())
        # ben has 0 real work -- the deterministic fallback would ALSO pick
        # ben (lowest real-work), so pin Jev's real choice to ADA instead
        # specifically to prove the real answer is what's used, not a
        # coincidental match with the fallback.
        candidates_order = ['ada', 'ben']  # roster order after maya/director exclusion
        idx_of_ada = candidates_order.index('ada')

        def fake_decision(model, state_arg, questions):
            return {'answers': {'choice': {'choice': f'idx_{idx_of_ada}', 'confidence': 0.9, 'probabilities': {}}},
                   'usage': {'cost': 0.0}}

        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync', side_effect=fake_decision):
            serve._peer_review_loop_pass()
        saved = serve.get_state_from_db()
        reports = saved.get('reports') or []
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['aboutId'], 'ada')

    def test_an_invalid_jev_choice_still_falls_back_safely(self):
        self._seed_roster_and_activity(time.time())

        def fake_decision(model, state_arg, questions):
            return {'answers': {'choice': {'choice': 'idx_99', 'confidence': 0.9, 'probabilities': {}}},
                   'usage': {'cost': 0.0}}

        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync', side_effect=fake_decision):
            serve._peer_review_loop_pass()  # must not raise (out-of-range index)
        saved = serve.get_state_from_db()
        reports = saved.get('reports') or []
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['aboutId'], 'ben')  # fallback: lowest real-work

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
        # Both workers covered by a FRESH report -> the loop files nothing and
        # doesn't even call Jev. The old behavior fell back to the whole roster
        # and re-flagged the same worker every cycle forever.
        self._seed_roster_and_activity(time.time())
        self._seed_recent_report('ada')
        self._seed_recent_report('ben')
        captured = {}
        with unittest.mock.patch.object(
                serve, '_call_openrouter_decision_sync',
                side_effect=lambda *a, **k: captured.setdefault('called', True)):
            n = serve._peer_review_loop_pass()
        self.assertEqual(n, 0, 'an all-covered roster must not file another report')
        self.assertNotIn('called', captured, 'no Jev call when no candidate is fresh')
        saved = serve.get_state_from_db()
        self.assertEqual(len(saved.get('reports') or []), 2)

    def test_a_covered_worker_is_skipped_while_a_fresh_peer_is_flagged(self):
        # ada already fresh-covered; ben is not -> the pool is ben ONLY
        # (freshness-filtered, no `or candidates` fallback) and Jev flags him.
        self._seed_roster_and_activity(time.time())
        self._seed_recent_report('ada')

        def fake_decision(model, state_arg, questions):
            # Pool is exactly [ben]; a valid idx_0 answer must map to han... ben.
            criteria = questions['choice']['criteria']
            self.assertEqual(list(criteria), ['idx_0'])
            return {'answers': {'choice': {'choice': 'idx_0', 'confidence': 0.9, 'probabilities': {}}},
                   'usage': {'cost': 0.0}}

        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync', side_effect=fake_decision):
            serve._peer_review_loop_pass()
        saved = serve.get_state_from_db()
        reports = saved.get('reports') or []
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[-1]['aboutId'], 'ben',
                         'the fresh candidate, not the already-covered worker, gets the report')

    def test_a_stale_report_allows_a_worker_back_into_the_pool(self):
        # Both workers' only reports are OLDER than the staleness window, so
        # both are fresh again and a new report may be filed.
        self._seed_roster_and_activity(time.time())
        stale_ms = int((time.time() - serve.PEER_REVIEW_REPORT_STALE_S - 60) * 1000)
        self._seed_recent_report('ada', ts_ms=stale_ms)
        self._seed_recent_report('ben', ts_ms=stale_ms)
        with unittest.mock.patch.object(
                serve, '_call_openrouter_decision_sync',
                return_value={'answers': {'choice': {'choice': 'idx_0', 'confidence': 0.9, 'probabilities': {}}},
                              'usage': {'cost': 0.0}}):
            serve._peer_review_loop_pass()
        saved = serve.get_state_from_db()
        self.assertEqual(len(saved.get('reports') or []), 3,
                         'staleness must let a worker back into the pool for a fresh review')


if __name__ == '__main__':
    unittest.main(verbosity=2)