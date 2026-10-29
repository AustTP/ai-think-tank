"""Temporal backfill for historical grade rows.

Going forward every grade carries meta._taskRef; the historical corpus
doesn't. A grade fires in the same tick as its task's completion, and
action_log records that completion with the task id -- so title + agent
name + a tight window resolves most of the rest, with a room
cross-check against the archived task. Anything ambiguous stays
unresolved: a false join would rate the judge on someone else's
outcome.
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-grade-backfill-')
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


ROSTER = {'ada': {'name': 'Ada', 'role': 'worker'},
          'nora': {'name': 'Nora', 'role': 'director'}}


def _wipe():
    serve._ensure_grade_outcomes()
    serve._grade_temporal_backfill(0)  # creates grade_backfill cheaply
    with serve._db() as conn:
        for table in ('decision_tape', 'action_log', 'grade_outcomes',
                      'task_archive'):
            conn.execute(f'DELETE FROM {table}')
        conn.execute('DELETE FROM grade_backfill')


def _seed_unjoinable_grade(title, room, name, age_s=10 * 86400, choice='8'):
    prompt = (f"Grade the deliverable '{title}' landed in the {room} room by "
              f"{name} (worker). Did it meet the work order?")
    serve._append_decision_tape('grade', 'test-model', prompt,
                                '{"8": "solid"}', choice, 0.9, 0.02, '{}', True)
    with serve._db() as conn:
        tape_id = conn.execute(
            'SELECT id FROM decision_tape ORDER BY id DESC LIMIT 1').fetchone()[0]
        conn.execute('UPDATE decision_tape SET ts = ? WHERE id = ?',
                     (time.time() - age_s, tape_id))
    return tape_id


def _seed_completion(task_id, title, agent_id, age_s, room='observatory'):
    with serve._db() as conn:
        conn.execute(
            'INSERT INTO action_log (ts, agent_id, action, details, authorized) '
            'VALUES (?, ?, ?, ?, ?)',
            (time.time() - age_s, agent_id, 'task_completed',
             json.dumps({'taskId': task_id, 'title': title}), None))
        blob = json.dumps({'id': task_id, 'room': room, 'title': title,
                           'status': 'done'})
        conn.execute(
            'INSERT OR REPLACE INTO task_archive (id, blob, updated_at) '
            'VALUES (?, ?, ?)', (task_id, blob, time.time()))


class Backfill(unittest.TestCase):
    def setUp(self):
        _wipe()
        patcher = unittest.mock.patch.object(serve, '_agent_name_to_id',
                                             return_value={'ada': 'ada',
                                                           'nora': 'nora'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unique_match_is_adopted(self):
        tape_id = _seed_unjoinable_grade('weather widget', 'observatory', 'Ada')
        _seed_completion('task-500', 'weather widget', 'ada', age_s=10 * 86400 - 5)
        out = serve._grade_temporal_backfill(120)
        self.assertEqual(out['matched'], 1)
        with serve._db() as conn:
            ref = conn.execute('SELECT task_ref, method FROM grade_backfill '
                               'WHERE tape_id = ?', (tape_id,)).fetchone()
        self.assertEqual(ref[0], 'task-500')
        self.assertEqual(ref[1], 'temporal')

    def test_reconcile_uses_the_backfilled_ref(self):
        tape_id = _seed_unjoinable_grade('weather widget', 'observatory', 'Ada')
        _seed_completion('task-501', 'weather widget', 'ada', age_s=10 * 86400 - 5)
        serve._grade_temporal_backfill(120)
        # A rework on task-501 after the grade must now rate this grade row.
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO action_log (ts, agent_id, action, details, authorized) '
                'VALUES (?, ?, ?, ?, ?)',
                (time.time() - 3 * 86400, 'nora', 'quality_gate_reject',
                 json.dumps({'taskId': 'task-501'}), None))
        serve._judge_outcome_reconcile(30)
        with serve._db() as conn:
            outcome = conn.execute(
                'SELECT outcome FROM grade_outcomes WHERE tape_id = ?',
                (tape_id,)).fetchone()[0]
        self.assertEqual(outcome, 'rework')

    def test_ambiguous_window_stays_unresolved(self):
        tape_id = _seed_unjoinable_grade('report draft', 'pressoffice', 'Ada')
        _seed_completion('task-600', 'report draft', 'ada', age_s=10 * 86400 - 5)
        _seed_completion('task-601', 'report draft', 'ada', age_s=10 * 86400 - 10)
        out = serve._grade_temporal_backfill(120)
        self.assertEqual(out['unresolved'], 1)
        with serve._db() as conn:
            row = conn.execute('SELECT task_ref FROM grade_backfill '
                               'WHERE tape_id = ?', (tape_id,)).fetchone()
        self.assertIsNone(row)

    def test_room_mismatch_rejects_the_match(self):
        _seed_unjoinable_grade('crawler fix', 'observatory', 'Ada')
        _seed_completion('task-700', 'crawler fix', 'ada',
                         age_s=10 * 86400 - 5, room='library')
        out = serve._grade_temporal_backfill(120)
        self.assertEqual(out['unresolved'], 1)

    def test_wrong_agent_rejects_the_match(self):
        _seed_unjoinable_grade('solo build', 'observatory', 'Ada')
        _seed_completion('task-800', 'solo build', 'nora', age_s=10 * 86400 - 5)
        out = serve._grade_temporal_backfill(120)
        self.assertEqual(out['unresolved'], 1)

    def test_idempotent(self):
        _seed_unjoinable_grade('weather widget', 'observatory', 'Ada')
        _seed_completion('task-900', 'weather widget', 'ada', age_s=10 * 86400 - 5)
        first = serve._grade_temporal_backfill(120)
        second = serve._grade_temporal_backfill(120)
        self.assertEqual(first['matched'], 1)
        self.assertEqual(second['matched'], 0)
        self.assertEqual(second['unresolved'], 0)

    def test_joinable_rows_are_not_backfilled(self):
        # A row the regex can resolve is left to the structured channel.
        _seed_unjoinable_grade('spike task-950 cleanup', 'observatory', 'Ada')
        _seed_completion('task-950', 'spike task-950 cleanup', 'ada',
                         age_s=10 * 86400 - 5)
        out = serve._grade_temporal_backfill(120)
        self.assertEqual(out['matched'], 0)


class AgentNameResolution(unittest.TestCase):
    def test_roster_from_state(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value={'agents': ROSTER}):
            roster = serve._agent_name_to_id()
        self.assertEqual(roster.get('ada'), 'ada')

    def test_roster_failure_is_empty(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        side_effect=RuntimeError('db')):
            self.assertEqual(serve._agent_name_to_id(), {})


if __name__ == '__main__':
    unittest.main()
