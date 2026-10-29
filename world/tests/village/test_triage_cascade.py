"""Cascade triage for the grooming ceremony.

A zero-dependency Naive Bayes fitted from the tape's own groom decisions
stands in front of the JEV decider; the judge is spent only when the
corpus is cold, the posterior is low, or the operator's live confidence
bar says distrust. Every auto-decision is taped (model 'cascade-nb') so
the audit stream stays continuous.
DB-isolated (setUpModule below, the test_vpn_exit_endpoints pattern).
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402

import json  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-triage-cascade-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()
    serve.JEV_SAFETY_CONFIDENCE = 0.6
    if hasattr(serve, '_calibration_bar_override'):
        serve._calibration_bar_override = None


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


BUGGY = ('The queue-runner crashes when a task is cancelled mid-run and the '
         'retry child then double-spends the budget ledger. Reproducible in '
         'the observatory every time; here is the failing trace and the two '
         'offending code paths.')


def _seed_groom(choice, text=BUGGY):
    serve._append_decision_tape(
        'groom', 'test-model', f'Groom this request: "{text}" fill',
        '["accept", "reject"]', choice, 0.9, 0.0, '{}', True)


def _seed_corpus(n_bug=30, n_filler=30):
    for _ in range(n_bug):
        _seed_groom('accept', BUGGY)
    filler = ('Add a small decorative mascot to the footer of the press '
              'office page. Purely cosmetic, no data, no integration, tiny.')
    for _ in range(n_filler):
        _seed_groom('reject', filler)


def _reset_cache():
    sim._TRIAGE_CACHE.update(model=None, fitted_at=0.0, rows=0)


def _settings(**over):
    base = {'triage_cascade_enabled': 'on', 'triage_cascade_threshold': '0.9'}
    base.update(over)
    return unittest.mock.patch.object(
        serve, '_get_setting',
        side_effect=lambda k, d=None: base.get(k, d))


class Tokens(unittest.TestCase):
    def test_stable_across_calls(self):
        self.assertEqual(sim._triage_tokens('alpha beta'),
                         sim._triage_tokens('alpha beta'))

    def test_shape(self):
        # alpha twice; single-char 'b' is dropped.
        counts = sim._triage_tokens('Alpha! alpha, b')
        self.assertEqual(list(counts.values()), [2])

    def test_empty(self):
        self.assertEqual(sim._triage_tokens(''), {})
        self.assertEqual(sim._triage_tokens(None), {})


class Classifier(unittest.TestCase):
    def test_separates_clear_classes(self):
        _seed_corpus()
        _reset_cache()
        model, trained = sim._triage_resolve_model()
        self.assertGreaterEqual(trained, sim._TRIAGE_MIN_TRAIN)
        choice, posterior = sim._triage_classify(
            model, sim._triage_tokens(BUGGY))
        self.assertEqual(choice, 'accept')
        self.assertGreater(posterior, 0.9)
        choice, posterior = sim._triage_classify(
            model, sim._triage_tokens(
                'decorative mascot footer cosmetic tiny no data'))
        self.assertEqual(choice, 'reject')

    def test_empty_model_returns_none(self):
        _reset_cache()
        self.assertEqual(sim._triage_classify({}, {'x': 1}), (None, 0.0))


class Cascade(unittest.TestCase):
    def setUp(self):
        _reset_cache()
        with serve._db() as conn:
            for table in ('decision_tape',):
                conn.execute(f'DELETE FROM {table}')

    def test_cold_corpus_stands_down(self):
        _seed_groom('accept', BUGGY)  # far below _TRIAGE_MIN_TRAIN
        self.assertIsNone(sim._triage_cascade_choice('groom this', []))

    def test_confident_corpus_fires_without_the_judge(self):
        _seed_corpus()
        with _settings():
            called = []

            def never(instructions, criteria, kind=None):
                called.append(1)
                return 'reject'

            choice = sim._triage_cascade_choice(
                f'Groom: {BUGGY}',
                [{'id': 'accept', 'description': BUGGY},
                 {'id': 'reject', 'description': 'duplicate'}])
            self.assertEqual(choice, 'accept')
            self.assertEqual(called, [])
        with serve._db() as conn:
            row = conn.execute(
                "SELECT model, choice, confidence, meta FROM decision_tape "
                "WHERE model = 'cascade-nb'").fetchone()
        self.assertEqual(row[1], 'accept')
        self.assertGreaterEqual(row[2], 0.9)
        self.assertTrue(json.loads(row[3]).get('cascade'))

    def test_low_posterior_signals_fall_through(self):
        # A balanced corpus with a lexically novel request scores ~0.5 per
        # class; below the threshold the cascade's contract is to return
        # None -- the call site's if/else then spends the real decider.
        _seed_corpus()
        with _settings(triage_cascade_threshold='0.99'):
            choice = sim._triage_cascade_choice(
                'Groom: something genuinely novel and unlike the corpus '
                'altogether xylophone', [])
        self.assertIsNone(choice)

    def test_kill_switch(self):
        _seed_corpus()
        with _settings(triage_cascade_enabled='off'):
            self.assertIsNone(sim._triage_cascade_choice(BUGGY, []))

    def test_stands_down_under_operator_distrust(self):
        _seed_corpus()
        with _settings(), \
             unittest.mock.patch.object(serve, '_effective_safety_confidence',
                                        return_value=0.95):
            self.assertIsNone(sim._triage_cascade_choice(BUGGY, []))

    def test_low_bar_lets_it_fire(self):
        _seed_corpus()
        with _settings(), \
             unittest.mock.patch.object(serve, '_effective_safety_confidence',
                                        return_value=0.6):
            self.assertEqual(sim._triage_cascade_choice(BUGGY, []), 'accept')

    def test_cascade_rows_never_retrain_themselves(self):
        _seed_corpus()
        serve._append_decision_tape(
            'groom', 'cascade-nb', BUGGY, '[]', 'accept_generous', 0.99,
            0.0, '{}', True, meta={'cascade': True})
        rows = sim._triage_train_rows()
        self.assertTrue(all(c in sim._TRIAGE_CLASSES for _, c in rows))
        _reset_cache()
        model, _ = sim._triage_resolve_model()
        # The planted cascade row's generous choice must not shift the
        # decision on the same text.
        choice, _ = sim._triage_classify(model, sim._triage_tokens(BUGGY))
        self.assertEqual(choice, 'accept')

    def test_report(self):
        _seed_corpus()
        _reset_cache()
        out = sim._triage_cascade_report()
        self.assertTrue(out['armed'])
        self.assertGreaterEqual(out['trainedRows'], sim._TRIAGE_MIN_TRAIN)


if __name__ == '__main__':
    unittest.main()
