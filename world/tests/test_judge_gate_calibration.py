"""Self-evolve ports: heterogeneous-judge escalation cross-check + review-grade
calibration (judge-the-judge).

From the codebase eval (self-evolve tools/sie/judges.py + selfdeception.py):
the director's delegated escalation auto-approval trusted ONE decisions model
to stand in for the human admin, and Jev's subjective review-checklist grades
were never measured against the mechanical pipeline truth they claim to
correlate with. Two sharp, contained ideas were pulled in:

  1. Heterogeneous-judge cross-check (serve._escalation_judge_crosscheck): when
     a SECOND, different decisions model (settings `jev_judge_model`, fallback
     env JEV_JUDGE_MODEL) is configured, the director's auto-approval only wins
     if the judge independently agrees. Fail-closed by construction: judge
     unavailable / non-binary / disagreeing / colluding (both models confident
     while the kind is already drifting) leaves the escalation pending and bumps
     a PERSISTED per-kind drift counter (`escalation_judge_drift` settings row);
     at ESCALATION_JUDGE_DRIFT_CIRCUIT the kind becomes human-only and the
     primary call isn't even made. A judge identical to the primary disables the
     gate (a retry is not independent evidence).

  2. Review-grade calibration (serve._insert_review_calibration_sample /
     _review_grade_calibration_pass): a 'code'-type requirement's verdict is
     MECHANICAL ground truth (the live quality pipeline), so a section whose
     code requirements are unanimous is a real anchor for judging the subjective
     'jev' grades in that same section. Agreement between Jev's verdict and the
     anchor calibrates the review-grade confidence bar (settings
     `jev_review_grade_confidence`) exactly like the safety-bar pass -- an
     overconfident grader gets a RAISED bar so fewer unchecked grades slip
     through on weak signal.

Hermetic: pure functions + a mocked _call_openrouter_decision_sync dispatching
on the model slug (the primary goes through _jev_quorum_choice_sync, the judge
through _escalation_judge_crosscheck); no live Jev, no network. DB redirected to
a temp dir like every other serve-touching suite.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402  (import first: serve lazily re-imports content at its
import content  # noqa: E402  # bottom, so content must not be mid-import when serve loads)

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-judge-gate-test-')
    _PATCHER = mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        # Standby off in hermetic modules: single-slug chain keeps the
        # decision breaker unarmed in the shared test process.
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def decision_payload(choice, confidence, cost=0.0):
    """A fake /api/alpha/decisions response shaped like what _jev_choice expects."""
    return {'answers': {'q': {'choice': choice, 'confidence': confidence}},
            'usage': {'cost': cost}}


def _answer(choice, confidence, cost=0.01):
    """Same shape for the content-side grade helpers (answer key 'choice')."""
    return {'answers': {'choice': {'choice': choice, 'confidence': confidence, 'probabilities': {}}},
            'usage': {'cost': cost}}


def _set_models(judge_model='judge-x', primary_model='primary-x'):
    serve._set_setting('jev_model', primary_model)
    serve._set_setting('jev_judge_model', judge_model)


def _clear_setting_keys(*keys):
    with serve._db() as conn:
        for key in keys:
            conn.execute('DELETE FROM settings WHERE key = ?', (key,))


# ---------------------------------------------------------------------------
# Judge model resolution
# ---------------------------------------------------------------------------

class JudgeModelResolutionTests(unittest.TestCase):
    def setUp(self):
        _clear_setting_keys('jev_model', 'jev_judge_model')

    def test_none_when_nothing_configured(self):
        with mock.patch.object(serve, 'JEV_JUDGE_MODEL', None):
            self.assertIsNone(serve._jev_judge_model())

    def test_settings_win_over_env(self):
        _set_models(judge_model='judge-x', primary_model='primary-x')
        with mock.patch.object(serve, 'JEV_JUDGE_MODEL', 'env-judge'):
            self.assertEqual(serve._jev_judge_model(), 'judge-x')

    def test_env_fallback_when_no_settings_row(self):
        serve._set_setting('jev_model', 'primary-x')
        with mock.patch.object(serve, 'JEV_JUDGE_MODEL', 'env-judge'):
            self.assertEqual(serve._jev_judge_model(), 'env-judge')

    def test_judge_identical_to_primary_is_disabled(self):
        # A second judge that is literally the same model is a retry, not
        # independent evidence -- the gate must be disabled, not faked.
        _set_models(judge_model='primary-x', primary_model='primary-x')
        self.assertIsNone(serve._jev_judge_model())


# ---------------------------------------------------------------------------
# Judge crosscheck helper
# ---------------------------------------------------------------------------

class JudgeCrosscheckTests(unittest.TestCase):
    def setUp(self):
        _clear_setting_keys('jev_model', 'jev_judge_model')

    def test_clean_approve_passes_through(self):
        _set_models()
        with mock.patch.object(serve, '_call_openrouter_decision_sync',
                               return_value=decision_payload('approve', 0.9, 0.01)) as call:
            decision, confidence, cost = serve._escalation_judge_crosscheck('instr', {'a': 'A'})
        self.assertEqual((decision, confidence, cost), ('approve', 0.9, 0.01))
        self.assertEqual(call.call_args[0][0], 'judge-x', 'the JUDGE answers, not the primary')

    def test_non_binary_answer_is_unavailable(self):
        _set_models()
        with mock.patch.object(serve, '_call_openrouter_decision_sync',
                               return_value=decision_payload('maybe', 0.9, 0.01)):
            decision, confidence, cost = serve._escalation_judge_crosscheck('instr', {'a': 'A'})
        self.assertEqual((decision, confidence), (None, None))
        self.assertEqual(cost, 0.01, 'the call cost is still surfaced even though the answer is unusable')

    def test_judge_failure_fails_closed_with_no_cost(self):
        _set_models()
        with mock.patch.object(serve, '_call_openrouter_decision_sync',
                               side_effect=RuntimeError('judge down')):
            decision, confidence, cost = serve._escalation_judge_crosscheck('instr', {'a': 'A'})
        self.assertEqual((decision, confidence, cost), (None, None, 0.0))

    def test_disabled_when_no_judge_never_calls(self):
        serve._set_setting('jev_model', 'primary-x')
        with mock.patch.object(serve, '_call_openrouter_decision_sync') as call:
            decision, confidence, cost = serve._escalation_judge_crosscheck('instr', {'a': 'A'})
        self.assertEqual((decision, confidence, cost), (None, None, 0.0))
        call.assert_not_called()


# ---------------------------------------------------------------------------
# Drift circuit persistence
# ---------------------------------------------------------------------------

class JudgeDriftTests(unittest.TestCase):
    def setUp(self):
        _clear_setting_keys('escalation_judge_drift')

    def test_bump_read_reset_round_trip(self):
        self.assertEqual(serve._escalation_judge_drift('ctx'), 0)
        serve._bump_escalation_judge_drift('ctx')
        serve._bump_escalation_judge_drift('ctx')
        self.assertEqual(serve._escalation_judge_drift('ctx'), 2)
        self.assertEqual(serve._escalation_judge_drift_summary().get('ctx'), 2)
        serve._reset_escalation_judge_drift('ctx')
        self.assertEqual(serve._escalation_judge_drift('ctx'), 0)
        self.assertNotIn('ctx', serve._escalation_judge_drift_summary())

    def test_corrupt_row_falls_back_to_zero(self):
        serve._set_setting('escalation_judge_drift', 'not-json')
        self.assertEqual(serve._escalation_judge_drift('ctx'), 0)


# ---------------------------------------------------------------------------
# Director resolver with the judge gate
# ---------------------------------------------------------------------------

class DirectorJudgeGateTests(unittest.TestCase):
    KIND = 'unresolved review requirement'

    def setUp(self):
        _clear_setting_keys('jev_model', 'jev_judge_model', 'escalation_judge_drift',
                            'jev_safety_confidence')
        serve._escalation_jev_errors.reset(self.KIND)

    def _run_resolver(self, esc, side_effect, drift=0, judge_model='judge-x'):
        """Call _resolve_pending_escalations_sync with a single escalation,
        mocking the director/state plumbing and _call_openrouter_decision_sync
        (dispatching on the model slug: the primary path goes through
        _jev_quorum_choice_sync, the judge through _escalation_judge_crosscheck).
        Returns (esc, jev_mock)."""
        pending_id = 'esc-test'
        for name, mock_value in (
            ('_load_escalations', mock.Mock(return_value={pending_id: esc})),
            ('_save_escalations', mock.Mock()),
            ('get_state_from_db', mock.Mock(return_value={
                'agentRoster': [{'id': 'a', 'name': 'Ada', 'isDirector': True, 'isAdmin': False}]})),
        ):
            patcher = mock.patch.object(serve, name, mock_value)
            patcher.start()
            self.addCleanup(patcher.stop)
        _set_models(judge_model=judge_model, primary_model='primary-x')
        serve._set_setting('escalation_judge_drift',
                           json.dumps({(esc.get('kind') or 'unknown'): drift}))
        jev = mock.patch('serve._call_openrouter_decision_sync', side_effect=side_effect)
        jevmock = jev.start()
        self.addCleanup(jev.stop)
        serve._resolve_pending_escalations_sync()
        return esc, jevmock

    def _primary(self, choice, confidence):
        def _f(model, _state, _questions):
            return decision_payload(choice, confidence)
        return _f

    def _both(self, primary, judge):
        def _f(model, _state, _questions):
            if model == 'judge-x':
                return decision_payload(*judge)
            return decision_payload(*primary)
        return _f

    def _judge_raises(self, primary):
        def _f(model, _state, _questions):
            if model == 'judge-x':
                raise RuntimeError('judge down')
            return decision_payload(*primary)
        return _f

    def test_judge_agreement_approves(self):
        esc = {'status': 'pending', 'kind': self.KIND, 'question': 'docs?'}
        esc, _ = self._run_resolver(esc, self._both(('approve', 0.95), ('approve', 0.8)))
        self.assertEqual(esc['status'], 'approved', 'independent agreement clears the cross-check')

    def test_judge_agreement_denies(self):
        esc = {'status': 'pending', 'kind': self.KIND, 'question': 'docs?'}
        esc, _ = self._run_resolver(esc, self._both(('deny', 0.9), ('deny', 0.8)))
        self.assertEqual(esc['status'], 'denied')

    def test_judge_disagreement_fails_closed_and_bumps_drift(self):
        esc = {'status': 'pending', 'kind': self.KIND, 'question': 'docs?'}
        esc, _ = self._run_resolver(esc, self._both(('approve', 0.95), ('deny', 0.8)))
        self.assertEqual(esc['status'], 'pending',
                         'a confident-but-wrong primary must not win against a real objection')
        self.assertEqual(serve._escalation_judge_drift(self.KIND), 1)

    def test_judge_unavailable_fails_closed_and_bumps_drift(self):
        esc = {'status': 'pending', 'kind': self.KIND, 'question': 'docs?'}
        esc, _ = self._run_resolver(esc, self._judge_raises(('approve', 0.95)))
        self.assertEqual(esc['status'], 'pending')
        self.assertEqual(serve._escalation_judge_drift(self.KIND), 1)

    def test_collusion_fails_closed_and_bumps_drift(self):
        # Both models answer the SAME way at high confidence WHILE the kind is
        # already drifting -> suspected collusion ("confident but wrong
        # together"), never auto-approved.
        esc = {'status': 'pending', 'kind': self.KIND, 'question': 'docs?'}
        esc, _ = self._run_resolver(esc, self._both(('approve', 0.95), ('approve', 0.95)),
                                    drift=1)
        self.assertEqual(esc['status'], 'pending')
        self.assertEqual(serve._escalation_judge_drift(self.KIND), 2)

    def test_high_confidence_agreement_at_zero_drift_is_not_collusion(self):
        # The collusion rule only fires while the kind is already drifting --
        # two first-time confident agrees are genuine independent evidence.
        esc = {'status': 'pending', 'kind': self.KIND, 'question': 'docs?'}
        esc, _ = self._run_resolver(esc, self._both(('approve', 0.95), ('approve', 0.95)),
                                    drift=0)
        self.assertEqual(esc['status'], 'approved')
        self.assertEqual(serve._escalation_judge_drift(self.KIND), 0)

    def test_judge_same_as_primary_disables_the_gate(self):
        esc = {'status': 'pending', 'kind': self.KIND, 'question': 'docs?'}
        esc, jev = self._run_resolver(esc, self._primary('approve', 0.95),
                                      judge_model='primary-x')
        self.assertEqual(esc['status'], 'approved')
        self.assertEqual(jev.call_count, 1, 'a judge identical to the primary adds no second call')

    def test_drift_circuit_trips_before_any_primary_call(self):
        # Drift >= ESCALATION_JUDGE_DRIFT_CIRCUIT makes the kind human-only and
        # is checked BEFORE the primary Jev call, so a tripped circuit doesn't
        # burn model spend.
        esc = {'status': 'pending', 'kind': self.KIND, 'question': 'docs?'}
        esc, jev = self._run_resolver(esc, self._both(('approve', 0.95), ('approve', 0.95)),
                                      drift=serve.ESCALATION_JUDGE_DRIFT_CIRCUIT)
        self.assertEqual(esc['status'], 'pending')
        jev.assert_not_called()

    def test_low_confidence_primary_never_reaches_the_judge(self):
        # The composite gate fires first: a weak primary stays pending for the
        # human without ever spending a judge call.
        esc = {'status': 'pending', 'kind': self.KIND, 'question': 'docs?'}
        esc, jev = self._run_resolver(esc, self._both(('approve', 0.5), ('approve', 0.95)))
        self.assertEqual(esc['status'], 'pending')
        models = [c[0][0] for c in jev.call_args_list]
        self.assertNotIn('judge-x', models, 'no judge call on a below-floor primary')


# ---------------------------------------------------------------------------
# Review-grade calibration (judge-the-judge)
# ---------------------------------------------------------------------------

class ReviewGradeCalibrationTests(unittest.TestCase):
    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM review_judge_calibration')
        _clear_setting_keys('jev_review_grade_confidence')

    def _seed(self, n, agree):
        for _ in range(n):
            serve._insert_review_calibration_sample(
                'repo', 'meets', 0.9, 'meets' if agree else 'fails')

    def test_insert_records_agree_flag_and_report_aggregates(self):
        serve._insert_review_calibration_sample('repo', 'meets', 0.9, 'meets')
        serve._insert_review_calibration_sample('repo', 'meets', 0.8, 'fails')
        serve._insert_review_calibration_sample('opening', 'fails', 0.9, 'fails')
        report = serve._review_grade_calibration_report()
        self.assertEqual(report['samples'], 3)
        self.assertEqual(report['agreed'], 2)
        self.assertAlmostEqual(report['agreement_rate'], 0.6667, places=3)
        by = {s['section']: s for s in report['sections']}
        self.assertEqual((by['repo']['samples'], by['repo']['agreed']), (2, 1))
        self.assertEqual((by['opening']['samples'], by['opening']['agreed']), (1, 1))

    def test_effective_confidence_default_and_clamp(self):
        self.assertEqual(serve._effective_review_grade_confidence(), serve.JEV_SAFETY_CONFIDENCE)
        for bad in ('0.99', '0.3', 'not-a-number'):
            serve._set_setting('jev_review_grade_confidence', bad)
            self.assertEqual(serve._effective_review_grade_confidence(), serve.JEV_SAFETY_CONFIDENCE, bad)
        serve._set_setting('jev_review_grade_confidence', '0.7')
        self.assertEqual(serve._effective_review_grade_confidence(), 0.7)

    def test_pass_raises_bar_on_poor_agreement(self):
        # The overconfident-grader case: Jev says MEETS but the pipeline anchor
        # says FAILS 10/10 -> agreement 0.0 -> the bar is RAISED one step.
        self._seed(serve.REVIEW_GRADE_CALIBRATION_MIN_SAMPLES, agree=False)
        new = serve._review_grade_calibration_pass()
        expected = round(serve.JEV_SAFETY_CONFIDENCE + serve.REVIEW_GRADE_CALIBRATION_STEP, 2)
        self.assertEqual(new, expected)
        self.assertEqual(serve._get_setting('jev_review_grade_confidence'), str(expected))
        self.assertEqual(serve._effective_review_grade_confidence(), expected)

    def test_pass_lowers_bar_on_excellent_agreement(self):
        self._seed(serve.REVIEW_GRADE_CALIBRATION_MIN_SAMPLES, agree=True)
        new = serve._review_grade_calibration_pass()
        expected = round(serve.JEV_SAFETY_CONFIDENCE - serve.REVIEW_GRADE_CALIBRATION_STEP, 2)
        self.assertEqual(new, expected)

    def test_pass_noop_below_min_samples(self):
        self._seed(serve.REVIEW_GRADE_CALIBRATION_MIN_SAMPLES - 1, agree=False)
        self.assertIsNone(serve._review_grade_calibration_pass())
        self.assertIsNone(serve._get_setting('jev_review_grade_confidence'))

    def test_pass_noop_within_dead_band(self):
        # 9/10 agreement = 0.90, the target -- inside hysteresis, no move.
        self._seed(serve.REVIEW_GRADE_CALIBRATION_MIN_SAMPLES - 1, agree=True)
        serve._insert_review_calibration_sample('repo', 'meets', 0.9, 'fails')
        self.assertIsNone(serve._review_grade_calibration_pass())


# ---------------------------------------------------------------------------
# Content-side: anchored samples are recorded only from real evidence
# ---------------------------------------------------------------------------

class ReviewCalibrationSamplingTests(unittest.TestCase):
    def test_sample_recorded_for_anchored_definite_jev_verdict(self):
        # A 'code' requirement is mechanical ground truth; when its section is
        # unanimous it anchors the subjective 'jev' grade in the same section.
        checklist = [
            {'id': 'c1', 'question': 'Does the code pass flake8 with no errors?',
             'section': 'repo', 'type': 'code'},
            {'id': 'j1', 'question': 'Is the opening specific?',
             'section': 'repo', 'type': 'jev'},
        ]
        with mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer(content.GRADE_MEETS, 0.9)), \
             mock.patch.object(content._serve, '_insert_review_calibration_sample') as rec:
            content._grade_review_checklist(checklist, 'review', {'ok': True}, 'ada')
        self.assertEqual(rec.call_count, 1)
        section, verdict, conf, anchor = rec.call_args[0]
        self.assertEqual(section, 'repo')
        self.assertEqual(verdict, content.GRADE_MEETS)
        self.assertEqual(anchor, content.GRADE_MEETS)

    def test_no_sample_when_section_has_no_anchor(self):
        checklist = [
            {'id': 'j1', 'question': 'Is the tone right?', 'section': 'tone', 'type': 'jev'},
        ]
        with mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer(content.GRADE_MEETS, 0.9)), \
             mock.patch.object(content._serve, '_insert_review_calibration_sample') as rec:
            content._grade_review_checklist(checklist, 'review', {'ok': True}, 'ada')
        rec.assert_not_called()

    def test_no_sample_for_unsure_grade_even_when_anchored(self):
        # An UNSURE grade is the judge declining, not a wrong answer -- it must
        # not pollute the calibration as a disagreement.
        checklist = [
            {'id': 'c1', 'question': 'Does the code pass flake8 with no errors?',
             'section': 'repo', 'type': 'code'},
            {'id': 'j1', 'question': 'Is the opening specific?',
             'section': 'repo', 'type': 'jev'},
        ]
        with mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer(content.GRADE_MEETS, 0.4)), \
             mock.patch.object(content._serve, '_insert_review_calibration_sample') as rec:
            content._grade_review_checklist(checklist, 'review', {'ok': True}, 'ada')
        rec.assert_not_called()


if __name__ == '__main__':
    unittest.main()
