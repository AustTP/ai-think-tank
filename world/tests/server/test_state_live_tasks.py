"""Stage 1 of the blob rework (2026-11-27): the live working set leaves the
whole-blob kv_state into per-row task_live (the task_archive pattern).

The blob carries NO tasks after the first reconciling save; get_state_from_db
merges task_live rows (live wins) under task_archive so every reader keeps
its exact shape; a save upserts only the tasks that actually changed. A
legacy blob that still carries tasks migrates on first save; a failed
reconcile keeps the tasks in the blob (never lose data); a 'none' reader
still resolves live task ids. DB-isolated (setUpModule, the
test_state_split pattern).
"""
import copy
import json
import os
import shutil
import sqlite3
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-task-live-')
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


def _base_state():
    return {
        'workQueue': [],
        'tasks': {
            't-1': {'id': 't-1', 'title': 'wire the shed', 'status': 'queued',
                    'assignedTo': 'ada'},
            't-2': {'id': 't-2', 'title': 'unstick the door', 'status': 'blocked',
                    'assignedTo': 'ben'},
            't-3': {'id': 't-3', 'title': 'old report', 'status': 'cancelled',
                    'createdAt': int((time.time() - 40 * 86400) * 1000)},
            't-4': {'id': 't-4', 'title': 'finished parser', 'status': 'done',
                    'createdAt': int((time.time() - 40 * 86400) * 1000)},
        },
    }


def _reset_task_caches():
    for cache in (serve._TASK_LIVE_CACHE, serve._TASK_ARCHIVE_CACHE):
        cache['loaded'] = False
        cache['db'] = None
    serve._TASK_ARCHIVE_CACHE['tasks'] = {}
    serve._TASK_LIVE_CACHE['rows'] = {}


def _raw_blob():
    with serve._db() as conn:
        row = conn.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
    return json.loads(row[0]) if row else None


class TaskLiveSplitTest(unittest.TestCase):
    def setUp(self):
        _reset_task_caches()
        serve.init_db()
        with serve._db() as conn:
            conn.execute('DELETE FROM kv_state')
            conn.execute('DELETE FROM state_checksum')
            conn.execute('DELETE FROM task_archive')
            conn.execute('DELETE FROM task_live')

    def test_round_trip_preserves_exact_shapes(self):
        state = _base_state()
        serve.save_state_to_db(copy.deepcopy(state))
        back = serve.get_state_from_db()
        for tid in ('t-1', 't-2', 't-3', 't-4'):
            self.assertEqual(back['tasks'][tid], state['tasks'][tid])

    def test_blob_drops_tasks_and_rows_carry_them(self):
        serve.save_state_to_db(_base_state())
        blob = _raw_blob()
        self.assertNotIn('tasks', blob)
        with serve._db() as conn:
            live_ids = {r[0] for r in conn.execute(
                'SELECT id FROM task_live').fetchall()}
            archive_ids = {r[0] for r in conn.execute(
                'SELECT id FROM task_archive').fetchall()}
        self.assertEqual(live_ids, {'t-1', 't-2'})
        self.assertEqual(archive_ids, {'t-3', 't-4'})

    def test_diff_save_upserts_only_changed_rows(self):
        serve.save_state_to_db(_base_state())
        with serve._db() as conn:
            before = dict(conn.execute(
                'SELECT id, updated_at FROM task_live').fetchall())
        time.sleep(0.02)
        state = serve.get_state_from_db()
        state['tasks']['t-2']['note'] = 'rechecked the hinge'
        serve.save_state_to_db(state)
        with serve._db() as conn:
            after = dict(conn.execute(
                'SELECT id, updated_at FROM task_live').fetchall())
        self.assertEqual(after['t-1'], before['t-1'])
        self.assertGreater(after['t-2'], before['t-2'])

    def test_task_moving_terminal_is_deleted_from_live_rows(self):
        serve.save_state_to_db(_base_state())
        state = serve.get_state_from_db()
        state['tasks']['t-1']['status'] = 'done'
        serve.save_state_to_db(state)
        with serve._db() as conn:
            live_ids = {r[0] for r in conn.execute(
                'SELECT id FROM task_live').fetchall()}
            archive_ids = {r[0] for r in conn.execute(
                'SELECT id FROM task_archive').fetchall()}
        self.assertEqual(live_ids, {'t-2'})
        self.assertIn('t-1', archive_ids)
        back = serve.get_state_from_db()
        self.assertEqual(back['tasks']['t-1']['status'], 'done')

    def test_reference_guarded_terminal_card_stays_live(self):
        serve.save_state_to_db(_base_state())
        state = serve.get_state_from_db()
        state['tasks']['t-3']['status'] = 'cancelled'
        state['tasks']['t-2']['dependsOn'] = ['t-3']
        serve.save_state_to_db(state)
        with serve._db() as conn:
            live_ids = {r[0] for r in conn.execute(
                'SELECT id FROM task_live').fetchall()}
            archive_ids = {r[0] for r in conn.execute(
                'SELECT id FROM task_archive').fetchall()}
        self.assertIn('t-3', live_ids)
        # _reconcile_task_archive upserts and never deletes: t-3's archive
        # row from the first save persists. It is stale but shadowed -- the
        # live merge runs first and the archive merge updates over it, so
        # readers see the live copy, exactly the old blob-wins ordering.
        self.assertIn('t-3', archive_ids)
        back = serve.get_state_from_db()
        self.assertEqual(back['tasks']['t-3']['status'], 'cancelled')
        # Graduation: once the referencing task clears, the guarded card
        # returns to the done set -- deleted from task_live, still in the
        # archive (upsert-only reconcile, diff-vs-cached sees it unchanged).
        state = serve.get_state_from_db()
        del state['tasks']['t-2']['dependsOn']
        serve.save_state_to_db(state)
        with serve._db() as conn:
            live_ids = {r[0] for r in conn.execute(
                'SELECT id FROM task_live').fetchall()}
            archive_ids = {r[0] for r in conn.execute(
                'SELECT id FROM task_archive').fetchall()}
        self.assertNotIn('t-3', live_ids)
        self.assertIn('t-3', archive_ids)
        back = serve.get_state_from_db()
        self.assertEqual(back['tasks']['t-3']['status'], 'cancelled')

    def test_mid_block_rollback_invalidates_row_caches(self):
        # A failure after the task_live phase (the checksum write here) rolls
        # the transaction back; the reconcile caches must not keep claiming
        # rows that never committed, or the retry would diff "no changes
        # needed" and drop 'tasks' from the blob while the DB rows stay
        # stale -- tasks would regress silently on the next restart.
        serve.save_state_to_db(_base_state())
        state = serve.get_state_from_db()
        state['tasks']['t-2']['note'] = 'rechecked the hinge'
        with unittest.mock.patch.object(
                serve, '_state_checksum_write',
                side_effect=sqlite3.OperationalError('boom')):
            with self.assertRaises(sqlite3.OperationalError):
                serve.save_state_to_db(state)
        self.assertFalse(serve._TASK_LIVE_CACHE['loaded'])
        # The retried save re-diffs against the committed rows and lands.
        serve.save_state_to_db(state)
        back = serve.get_state_from_db()
        self.assertEqual(back['tasks']['t-2'].get('note'),
                         'rechecked the hinge')
        blob = _raw_blob()
        self.assertNotIn('tasks', blob)

    def test_legacy_blob_migrates_on_first_save(self):
        # Pre-split database: the blob carries the tasks, task_live is empty.
        # Reads serve the blob copy; the first save moves the live set into
        # rows and drops the key atomically.
        legacy = _base_state()
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO kv_state (id, blob, updated_at) VALUES (1, ?, ?)',
                (json.dumps(legacy), time.time()))
        back = serve.get_state_from_db()
        self.assertEqual(back['tasks'], legacy['tasks'])
        serve.save_state_to_db(serve.get_state_from_db())
        blob = _raw_blob()
        self.assertNotIn('tasks', blob)
        again = serve.get_state_from_db()
        for tid in ('t-1', 't-2', 't-3', 't-4'):
            self.assertEqual(again['tasks'][tid], legacy['tasks'][tid])

    def test_failed_reconcile_keeps_tasks_in_blob(self):
        # The row table cannot be written: the save must keep the tasks in
        # the blob rather than lose them (the same contract the other scale
        # slices honor).
        legacy = _base_state()
        legacy['tasks']['t-5'] = {'id': 't-5', 'title': 'fresh', 'status': 'queued'}
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO kv_state (id, blob, updated_at) VALUES (1, ?, ?)',
                (json.dumps(legacy), time.time()))
        with serve._db() as conn:
            conn.execute('DROP TABLE task_live')
        _reset_task_caches()
        state = serve.get_state_from_db()
        self.assertIn('t-5', state['tasks'])
        serve.save_state_to_db(state)
        blob = _raw_blob()
        self.assertIn('tasks', blob)
        self.assertIn('t-5', blob['tasks'])
        _reset_task_caches()
        back = serve.get_state_from_db()
        self.assertIn('t-5', back['tasks'])

    def test_none_mode_still_sees_live_tasks(self):
        serve.save_state_to_db(_base_state())
        state = serve.get_state_from_db(archive_merge='none')
        self.assertIn('t-1', state['tasks'])
        self.assertIn('t-2', state['tasks'])
        # 'none' means the ARCHIVE is not merged, not that tasks vanish.
        self.assertNotIn('t-4', state['tasks'])

    def test_stale_save_cannot_erase_newer_completion_on_live_task(self):
        # preserve_guidance compares against the PERSISTED view; with tasks
        # in rows, that view must include task_live or a stale whole-state
        # save could roll a live task back (the exact case the guard exists
        # for).
        first = _base_state()
        serve.save_state_to_db(first)
        state = serve.get_state_from_db()
        state['tasks']['t-1']['status'] = 'done'
        state['tasks']['t-1']['completedAt'] = int(time.time() * 1000) + 5000
        serve.save_state_to_db(state)
        stale = serve.get_state_from_db()
        stale['tasks']['t-1']['status'] = 'queued'
        stale['tasks']['t-1'].pop('completedAt', None)
        serve.save_state_to_db(stale)
        back = serve.get_state_from_db()
        self.assertEqual(back['tasks']['t-1']['status'], 'done')
        self.assertTrue(back['tasks']['t-1'].get('completedAt'))


if __name__ == '__main__':
    unittest.main()