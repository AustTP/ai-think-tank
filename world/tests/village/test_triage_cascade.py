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
import time
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


def _seed_interleaved(n=60):
    """Interleaved so the newest-quarter holdout mixes classes -- a
    block-ordered corpus makes the holdout degenerate (one class only)."""
    fill = ('Add a small decorative mascot to the footer of the press '
            'office page. Purely cosmetic, no data, no integration, tiny.')
    for i in range(n):
        _seed_groom('accept' if i % 2 == 0 else 'reject',
                    BUGGY if i % 2 == 0 else fill)


class NightlyRetrain(unittest.TestCase):
    def setUp(self):
        _reset_cache()
        with serve._db() as conn:
            conn.execute('DELETE FROM decision_tape')
            conn.execute("DELETE FROM settings WHERE key LIKE 'triage_%'")
            conn.execute("DELETE FROM action_log WHERE action = "
                         "'triage_nightly_retrain'")

    def test_only_runs_at_night(self):
        out = sim._triage_nightly_retrain(_hour=14)
        self.assertEqual(out, {'ran': False, 'reason': 'not-night'})

    def test_adopts_a_validated_fit_and_logs_it(self):
        _seed_interleaved(60)
        out = sim._triage_nightly_retrain(_hour=3)
        self.assertTrue(out['ran'])
        self.assertTrue(out['adopted'])
        self.assertGreaterEqual(out['accuracy'], sim._TRIAGE_ADOPT_ACCURACY)
        with serve._db() as conn:
            logged = conn.execute(
                "SELECT details FROM action_log WHERE action = "
                "'triage_nightly_retrain'").fetchall()
            stored = conn.execute(
                "SELECT value FROM settings WHERE key = 'triage_last_retrain'"
            ).fetchone()
        self.assertEqual(len(logged), 1)
        self.assertTrue(json.loads(stored[0])['adopted'])
        # The published fit is the full-data fit, primed in the cache.
        self.assertIsNotNone(sim._TRIAGE_CACHE['model'])

    def test_rejects_a_fit_that_cannot_beat_majority(self):
        # Same text, alternating labels: the best possible classifier is a
        # coin flip, so accuracy cannot beat the baseline.
        for i in range(40):
            _seed_groom('accept' if i % 2 == 0 else 'reject', BUGGY)
        out = sim._triage_nightly_retrain(_hour=3)
        self.assertTrue(out['ran'])
        self.assertFalse(out['adopted'])
        with serve._db() as conn:
            details = json.loads(conn.execute(
                "SELECT details FROM action_log WHERE action = "
                "'triage_nightly_retrain'").fetchone()[0])
        self.assertFalse(details['adopted'])

    def test_once_per_night(self):
        _seed_interleaved(60)
        first = sim._triage_nightly_retrain(_hour=3)
        self.assertTrue(first['ran'])
        second = sim._triage_nightly_retrain(_hour=4)
        self.assertEqual(second['reason'], 'already-retrained')
        with serve._db() as conn:
            logged = conn.execute(
                "SELECT COUNT(*) FROM action_log WHERE action = "
                "'triage_nightly_retrain'").fetchone()[0]
        self.assertEqual(logged, 1)

    def test_cold_corpus_reports_without_marking_the_night_done(self):
        _seed_groom('accept', BUGGY)
        out = sim._triage_nightly_retrain(_hour=3)
        self.assertEqual(out['reason'], 'cold-corpus')
        with serve._db() as conn:
            stored = conn.execute(
                "SELECT value FROM settings WHERE key = 'triage_last_retrain'"
            ).fetchone()
        self.assertIsNone(stored)  # retried on the next tick until data exists

    def test_report_carries_the_last_retrain(self):
        _seed_interleaved(60)
        sim._triage_nightly_retrain(_hour=3)
        out = sim._triage_cascade_report()
        self.assertIn('lastRetrain', out)
        self.assertTrue(out['lastRetrain']['adopted'])


if __name__ == '__main__':
    unittest.main()


class NearDuplicateSurfacing(unittest.TestCase):
    """The groom instructions gain a NEAR-DUPLICATE CHECK line when the
    archive of delivered work contains something semantically close -- a
    hint for the groomer, not a verdict."""

    def _groom(self, req):
        state = {
            'agentRoster': [
                {'id': 'd1', 'name': 'Dana', 'director': None},
                {'id': 'sm1', 'name': 'Sam', 'director': 'd1'},
                {'id': 'm1', 'name': 'Mia', 'director': 'd1'},
            ],
            'agents': {
                'd1': {'id': 'd1', 'busy': False, 'offDuty': False},
                'sm1': {'id': 'sm1', 'name': 'Sam', 'busy': False, 'offDuty': False},
                'm1': {'id': 'm1', 'busy': False, 'offDuty': False},
            },
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'backlogRequests': [req], 'issues': {}, 'tasks': {}, 'workQueue': [],
        }
        captured = {}

        def decider(instructions, criteria, kind=None):
            captured['instructions'] = instructions
            return 'accept'

        pending = {'scrumMasterId': 'sm1', 'teamId': 't1', 'reqIds': ['r1'],
                   'people': {}}
        gov = unittest.mock.patch.object(sim, '_log_governance',
                                         lambda *a, **k: None)
        digest = unittest.mock.patch.object(serve, 'log_refinement_digest',
                                            lambda *a, **k: None)
        with gov, digest:
            sim._resolve_refinement(state, pending, 1_000_000, decider=decider)
        return captured['instructions']

    def setUp(self):
        _reset_cache()
        with serve._db() as conn:
            conn.execute('DELETE FROM decision_tape')
            conn.execute("DELETE FROM settings WHERE key LIKE 'triage_%'")
        serve._rag_ensure_table()
        with serve._db() as conn:
            conn.execute('DELETE FROM rag_index')
            conn.execute('DELETE FROM task_archive')
        shutil.rmtree(serve.LIBRARY_DIR, ignore_errors=True)
        os.makedirs(serve.LIBRARY_DIR, exist_ok=True)

    def _seed_deliverable(self, task_id, room, title, body):
        blob = json.dumps({'id': task_id, 'room': room, 'title': title,
                           'deliverableText': body})
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO task_archive (id, blob, updated_at) VALUES (?, ?, ?)',
                (task_id, blob, time.time()))
        serve._rag_index_pass(60)

    def test_near_duplicate_line_names_the_prior_work(self):
        self._seed_deliverable(
            'task-77', 'observatory', 'Weather widget',
            'Weather widget for the observatory: shows the current '
            'conditions and forecast on the wall display.')
        instructions = self._groom({
            'id': 'r1', 'status': 'pending', 'filedBy': 'm1',
            'title': '[STORY] Add a weather widget', 'room': 'observatory',
            'reason': 'The observatory cannot show the current weather.',
            'issueType': 'story', 'deliverable': 'code',
            'acceptanceCriteria': 'Given a load, when the page opens, then '
                                  'the widget shows conditions.',
            'never': 'NEVER: Never call paid APIs',
        })
        self.assertIn('NEAR-DUPLICATE CHECK', instructions)
        self.assertIn('task-77', instructions)
        self.assertIn('Reject it as a duplicate', instructions)

    def test_no_match_no_line(self):
        instructions = self._groom({
            'id': 'r1', 'status': 'pending', 'filedBy': 'm1',
            'title': '[STORY] Add a weather widget', 'room': 'observatory',
            'reason': 'The observatory cannot show the current weather.',
        })
        self.assertNotIn('NEAR-DUPLICATE CHECK', instructions)

    def test_retrieval_failure_never_blocks_grooming(self):
        with unittest.mock.patch.object(serve, '_rag_search',
                                        side_effect=RuntimeError('down')):
            instructions = self._groom({
                'id': 'r1', 'status': 'pending', 'filedBy': 'm1',
                'title': '[STORY] Add a weather widget', 'room': 'observatory',
                'reason': 'weather',
            })
        # Grooming survived: the instructions were built and handed to the
        # decider even though retrieval blew up.
        self.assertIn('Turn it into a real card', instructions)
        self.assertNotIn('NEAR-DUPLICATE CHECK', instructions)


if __name__ == '__main__':
    unittest.main()
