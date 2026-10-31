"""Scale slice: agentRoster / agents / playerInbox leave the whole-blob
kv_state into per-row tables (the task_archive pattern).

The blob keeps every other section; get_state_from_db merges the rows back
in so every reader keeps its exact shape; save_state_to_db upserts only rows
that changed and drops the reconciled keys from the blob in the same
transaction. A legacy blob that still carries a section migrates on first
save; a failed reconcile keeps the section in the blob (never lose data).
DB-isolated (setUpModule, the test_vpn_exit_endpoints pattern).
"""
import copy
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-state-split-')
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
        'tasks': {},
        'agentRoster': [
            {'id': 'ada', 'role': 'worker', 'director': 'dev'},
            {'id': 'ben', 'role': 'worker', 'director': 'dev'},
        ],
        'agents': {
            'ada': {'id': 'ada', 'x': 1, 'y': 2, 'busy': False},
            'ben': {'id': 'ben', 'x': 3, 'y': 4, 'busy': False},
        },
        'playerInbox': [
            {'id': 'mail-1', 'text': 'hello'},
            {'id': 'card-2', 'text': 'world'},
        ],
    }


def _raw_blob():
    with serve._db() as conn:
        row = conn.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
    return json.loads(row[0]) if row else None


def _reset_extracted_caches():
    # A test that rewrites the DB behind serve's back must not inherit the
    # in-process row caches keyed on the previous DB_PATH contents.
    for cache in serve._EXTRACTED_CACHE.values():
        cache['loaded'] = False
        cache['rows'] = {}


class StateSplitTest(unittest.TestCase):
    def setUp(self):
        _reset_extracted_caches()
        # Re-create any table a sibling test dropped (the failed-reconcile
        # case), then start from empty rows. init_db is idempotent.
        serve.init_db()
        with serve._db() as conn:
            for table in ('roster_agent', 'live_agent', 'inbox_message'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute('DELETE FROM kv_state')
            conn.execute('DELETE FROM state_checksum')
            conn.execute('DELETE FROM task_archive')

    def test_round_trip_preserves_exact_shapes(self):
        state = _base_state()
        serve.save_state_to_db(copy.deepcopy(state))
        back = serve.get_state_from_db()
        for key in ('agentRoster', 'agents', 'playerInbox'):
            self.assertEqual(back[key], state[key])

    def test_blob_drops_extracted_keys_and_rows_carry_them(self):
        serve.save_state_to_db(_base_state())
        blob = _raw_blob()
        for key in ('agentRoster', 'agents', 'playerInbox'):
            self.assertNotIn(key, blob)
        with serve._db() as conn:
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM roster_agent').fetchone()[0], 2)
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM live_agent').fetchone()[0], 2)
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM inbox_message').fetchone()[0], 2)

    def test_diff_save_upserts_only_changed_rows(self):
        serve.save_state_to_db(_base_state())
        with serve._db() as conn:
            before = dict(conn.execute(
                'SELECT id, updated_at FROM roster_agent').fetchall())
        time.sleep(0.02)
        state = serve.get_state_from_db()
        state['agentRoster'][1]['role'] = 'subdirector'
        serve.save_state_to_db(state)
        with serve._db() as conn:
            after = dict(conn.execute(
                'SELECT id, updated_at FROM roster_agent').fetchall())
        self.assertEqual(after['ada'], before['ada'])
        self.assertGreater(after['ben'], before['ben'])

    def test_legacy_blob_migrates_on_first_save(self):
        # Pre-split database: the blob carries the sections, the row tables
        # are empty. Reads serve the blob copy; the first save moves the
        # sections into rows and drops the keys atomically.
        legacy = _base_state()
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO kv_state (id, blob, updated_at) VALUES (1, ?, ?)',
                (json.dumps(legacy), time.time()))
        back = serve.get_state_from_db()
        self.assertEqual(back['agentRoster'], legacy['agentRoster'])
        serve.save_state_to_db(serve.get_state_from_db())
        blob = _raw_blob()
        for key in ('agentRoster', 'agents', 'playerInbox'):
            self.assertNotIn(key, blob)
        again = serve.get_state_from_db()
        self.assertEqual(again['agentRoster'], legacy['agentRoster'])
        self.assertEqual(again['agents'], legacy['agents'])
        self.assertEqual(again['playerInbox'], legacy['playerInbox'])

    def test_removed_agent_row_is_deleted(self):
        serve.save_state_to_db(_base_state())
        state = serve.get_state_from_db()
        state['agentRoster'] = [r for r in state['agentRoster']
                                if r['id'] != 'ben']
        state['agents'].pop('ben')
        serve.save_state_to_db(state)
        with serve._db() as conn:
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM roster_agent').fetchone()[0], 1)
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM live_agent').fetchone()[0], 1)
        back = serve.get_state_from_db()
        self.assertEqual([r['id'] for r in back['agentRoster']], ['ada'])

    def test_failed_reconcile_keeps_section_in_blob(self):
        # Legacy blob (sections still inside it), then the reconcile cannot
        # write its table: the save must keep that section in the blob rather
        # than lose it, while the sections that DID reconcile still leave.
        legacy = _base_state()
        legacy['playerInbox'].append({'id': 'mail-3', 'text': 'later'})
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO kv_state (id, blob, updated_at) VALUES (1, ?, ?)',
                (json.dumps(legacy), time.time()))
        with serve._db() as conn:
            conn.execute('DROP TABLE inbox_message')
        _reset_extracted_caches()
        state = serve.get_state_from_db()
        self.assertEqual(len(state['playerInbox']), 3)
        serve.save_state_to_db(state)
        blob = _raw_blob()
        self.assertIn('playerInbox', blob)
        self.assertEqual(len(blob['playerInbox']), 3)
        self.assertNotIn('agentRoster', blob)
        self.assertNotIn('agents', blob)
        _reset_extracted_caches()
        back = serve.get_state_from_db()
        self.assertEqual(len(back['playerInbox']), 3)
        self.assertEqual([r['id'] for r in back['agentRoster']], ['ada', 'ben'])

    def test_absent_key_clears_the_section(self):
        # A full-state save without playerInbox means the section is EMPTY:
        # the blob write was always a full replace (absent key = dropped
        # key), so the rows must follow the same contract. A partial-state
        # test helper seeding {} therefore clears the rows too -- which is
        # what the supervisor-meeting suite's _seed({}) expects.
        serve.save_state_to_db(_base_state())
        state = serve.get_state_from_db()
        state.pop('playerInbox')
        serve.save_state_to_db(state)
        back = serve.get_state_from_db()
        # No rows survive; the key itself may be absent (the reader-side
        # `or []` convention handles that, exactly as a fresh blob did).
        self.assertFalse(back.get('playerInbox'))

    def test_unrowable_entries_stay_in_blob(self):
        state = _base_state()
        state['agentRoster'].append('not-a-dict')
        serve.save_state_to_db(state)
        blob = _raw_blob()
        self.assertIn('agentRoster', blob)
        back = serve.get_state_from_db()
        self.assertEqual(back['agentRoster'], state['agentRoster'])


if __name__ == '__main__':
    unittest.main()
