"""Judge calibration: grade -> downstream outcome ledger (2026-10-29).

The operator's three commitments after reviewing the Gamma / Norona
agent-harness interview: (1) grades are checked against what happened next
(rework or held), (2) the held-up rate is the owner-accepted-without-rework
north star, (3) the labeled rows are the training set for a specialized
judge model. These tests pin the join, the idempotent reconcile, the
strict rework signals, the operator gate, and the new `_taskRef` tape
metadata channel.

DB-isolated (setUpModule below, the test_vpn_exit_endpoints pattern).
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

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-judge-cal-')
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


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _wipe():
    serve._ensure_grade_outcomes()
    with serve._db() as conn:
        for table in ('grade_outcomes', 'decision_tape', 'action_log'):
            conn.execute(f'DELETE FROM {table}')


def _seed_grade(task_ref=None, prompt_extra='', choice='8', confidence=0.9,
                age_s=10 * 86400, meta=True):
    subject = f'deliverable {task_ref}' if task_ref else 'a deliverable'
    prompt = f'Ada just finished a time-boxed spike on "{subject}" {prompt_extra}'
    questions = {'choice': {'type': 'choice', 'instructions': prompt,
                            'criteria': {'8': 'solid'}},
                 '_kind': 'grade'}
    if task_ref and meta:
        questions['_taskRef'] = task_ref
    serve._append_decision_tape(
        'grade', 'test-model', prompt,
        json.dumps(questions['choice']['criteria']), choice, confidence,
        0.0, '{}', True)
    # _append_decision_tape returns nothing; single-threaded fixture, so the
    # newest row is the one just written.
    with serve._db() as conn:
        tape_id = conn.execute(
            'SELECT id FROM decision_tape ORDER BY id DESC LIMIT 1').fetchone()[0]
        conn.execute('UPDATE decision_tape SET ts = ? WHERE id = ?',
                     (time.time() - age_s, tape_id))
    return tape_id


def _seed_action(action, details, age_s=3 * 86400):
    with serve._db() as conn:
        conn.execute(
            'INSERT INTO action_log (ts, agent_id, action, details, authorized) '
            'VALUES (?, ?, ?, ?, ?)',
            (time.time() - age_s, 'nora', action, json.dumps(details), None))


def _client():
    from starlette.testclient import TestClient
    c = TestClient(serve.app)
    c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
    return c


class Reconcile(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_classification_hold_rework_and_pending(self):
        _seed_grade('task-201', choice='8', confidence=0.9)
        _seed_grade('task-202', choice='10', confidence=0.97)
        _seed_grade('task-203', choice='6', confidence=0.8, age_s=2 * 86400)
        _seed_action('quality_gate_reject',
                     {'taskId': 'task-202', 'reason': 'wrong shape'})
        report = serve._judge_calibration_report(30)
        self.assertEqual(report['labeled'], 2)
        self.assertEqual(report['pending'], 1)
        self.assertEqual(report['holdRate'], 0.5)
        by_grade = {row['grade']: row for row in report['byGrade']}
        self.assertEqual(by_grade['8']['held'], 1)
        self.assertEqual(by_grade['10']['rework'], 1)
        self.assertEqual(report['overconfident'], 1)  # the confident 10 that broke

    def test_regex_fallback_when_no_meta(self):
        _seed_grade(None, prompt_extra='follow-up to task-777 for task-777',
                    meta=False)
        serve._judge_outcome_reconcile(30)
        with serve._db() as conn:
            row = conn.execute(
                'SELECT task_ref FROM grade_outcomes').fetchone()
        self.assertEqual(row[0], 'task-777')

    def test_boundary_no_cross_task_match(self):
        # task-123's grade must not be condemned by task-1234's reject.
        _seed_grade('task-123')
        _seed_action('quality_gate_reject', {'taskId': 'task-1234'})
        report = serve._judge_calibration_report(30)
        self.assertEqual(report['holdRate'], 1.0)

    def test_reconcile_is_idempotent_and_matures(self):
        tape_id = _seed_grade('task-301')
        first = serve._judge_outcome_reconcile(30)
        second = serve._judge_outcome_reconcile(30)
        with serve._db() as conn:
            count = conn.execute(
                'SELECT COUNT(*) FROM grade_outcomes WHERE tape_id = ?',
                (tape_id,)).fetchone()[0]
            outcome = conn.execute(
                'SELECT outcome FROM grade_outcomes WHERE tape_id = ?',
                (tape_id,)).fetchone()[0]
        self.assertEqual(first, 1)
        self.assertEqual(second, 1)
        self.assertEqual(count, 1)
        self.assertEqual(outcome, 'held')
        # A late adverse event flips held -> rework on the next pass; the
        # ledger never flips back on its own. The event lands 7 days after
        # the grade, inside the 14-day follow-up window.
        _seed_action('task_review_requeued', {'taskId': 'task-301'})
        serve._judge_outcome_reconcile(30)
        with serve._db() as conn:
            outcome = conn.execute(
                'SELECT outcome FROM grade_outcomes WHERE tape_id = ?',
                (tape_id,)).fetchone()[0]
        self.assertEqual(outcome, 'rework')

    def test_unparseable_grades_are_skipped(self):
        _seed_grade(None, prompt_extra='no task id here', meta=False)
        self.assertEqual(serve._judge_outcome_reconcile(30), 0)

    def test_review_decisions_do_not_rate_the_deliverable_judge(self):
        # kind='grade' also tapes peer-review decisions ('actionable',
        # 'allow', 'file_bug'); only the 0-10 score buckets feed the ledger,
        # and rows ledgered before the filter existed are purged.
        prompt = 'Review nora task-811 the report reads well, actionable items clear'
        serve._append_decision_tape('grade', 'test-model', prompt,
                                    '{"allow": "ok"}', 'allow', 0.95, 0.0,
                                    '{}', True)
        _seed_grade('task-812')
        serve._judge_outcome_reconcile(30)
        with serve._db() as conn:
            refs = [r[0] for r in conn.execute(
                'SELECT task_ref FROM grade_outcomes').fetchall()]
        self.assertEqual(refs, ['task-812'])


class Export(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_export_is_labeled_jsonl(self):
        _seed_grade('task-401')
        _seed_action('quality_gate_reject', {'taskId': 'task-401'})
        serve._judge_outcome_reconcile(30)
        lines = [json.loads(line)
                 for line in serve._judge_calibration_export(30).splitlines()]
        self.assertEqual(len(lines), 1)
        row = lines[0]
        self.assertEqual(row['taskRef'], 'task-401')
        self.assertEqual(row['outcome'], 'rework')
        self.assertIn('task-401', row['prompt'])


class Endpoint(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_requires_operator(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        response = c.get('/api/judge/calibration')
        self.assertEqual(response.status_code, 401)

    def test_report_and_export_for_the_player(self):
        c = _client()
        _seed_grade('task-501')
        response = c.get('/api/judge/calibration?days=30')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn('holdRate', body)
        self.assertIn('byGrade', body)
        exported = c.get('/api/judge/calibration?days=30&export=1')
        self.assertEqual(exported.status_code, 200)
        row = json.loads(exported.text.splitlines()[0])
        self.assertEqual(row['taskRef'], 'task-501')

    def test_days_is_clamped(self):
        c = _client()
        self.assertEqual(c.get('/api/judge/calibration?days=999').status_code, 200)
        self.assertEqual(c.get('/api/judge/calibration?days=0').status_code, 200)


class TapeMeta(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_underscore_keys_persist_and_stay_off_the_wire(self):
        captured = {}

        def fake_request(slug, state, questions):
            captured['questions'] = questions
            raise RuntimeError('stop before the network')

        with unittest.mock.patch.object(serve, '_decision_request', fake_request), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'):
            with self.assertRaises(Exception):
                serve._call_openrouter_decision_sync(
                    'test-model', {'messages': [], 'signals': {}},
                    {'choice': {'type': 'choice', 'instructions': 'pick one',
                                'criteria': {'a': 'x', 'b': 'y'}},
                     '_kind': 'grade', '_taskRef': 'task-601'})
        wire = json.dumps(captured['questions'])
        self.assertNotIn('_taskRef', wire)
        self.assertNotIn('_kind', wire)
        with serve._db() as conn:
            meta, kind = conn.execute(
                'SELECT meta, kind FROM decision_tape ORDER BY id DESC LIMIT 1'
            ).fetchone()
        self.assertEqual(json.loads(meta), {'_taskRef': 'task-601'})
        self.assertEqual(kind, 'grade')

    def test_grade_call_site_threads_the_task_ref(self):
        captured = {}

        def fake_call(model, state, questions, village_id=None):
            captured['questions'] = questions
            return {'answers': {'q': {'choice': '8', 'confidence': 0.9}},
                    'usage': {'cost': 0.0}}

        with unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        fake_call):
            import sim
            score = sim._grading_decider_default({}, 'grade this', 'title',
                                                 'workroom', 'task-701')
        self.assertEqual(score, 8.0)
        self.assertEqual(captured['questions'].get('_taskRef'), 'task-701')

    def test_legacy_three_arg_stub_still_grades(self):
        # The TypeError fallback in _grade_completed_task keeps injected
        # stubs that predate the task-ref parameter grading normally.
        import sim

        def old_stub(state, instructions, task_title, room):
            return 6.0

        state = {'agents': {'ada': {'name': 'Ada', 'role': 'worker'}}}
        task = {'id': 'task-801', 'room': 'observatory',
                'title': 'Build the widget', 'taskType': 'code'}
        with unittest.mock.patch.object(sim, '_grading_decider', old_stub), \
             unittest.mock.patch.object(sim, 'team_policy',
                                        lambda s, t, k: 0.0):
            sim._grade_completed_task(state, 'ada', task, 123)
        entry = state['completedDeliverables'][0]
        self.assertEqual(entry['grade'], 6.0)
        self.assertEqual(entry['id'], 'task-801')


if __name__ == '__main__':
    unittest.main()
