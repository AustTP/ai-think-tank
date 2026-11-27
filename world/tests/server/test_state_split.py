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


# Round-2 sections (2026-11-01): the remaining blob-heavyweights.
ROUND2_TABLES = ('growth_plan', 'help_signal', 'tracked_issue',
                 'wake_entry', 'backlog_request', 'completed_deliverable')
ROUND2_KEYS = ('growthPlans', 'helpSignals', 'issues', 'wakeQueue',
               'backlogRequests', 'completedDeliverables')

# Round-3 sections (2026-11-27): the bookkeeping dicts.
STAGE2_TABLES = ('fired_agent', 'story_cost_tracking', 'feedback_note',
                 'card_failure_notified', 'last_email_at',
                 'repair_loop_notified', 'stale_work_replan',
                 'pattern_suppressed_notified')
STAGE2_KEYS = ('firedAgents', 'storyCostTracking', '_feedback',
               '_cardFailureNotified', '_lastEmailAt', '_repairLoopNotified',
               '_staleWorkReplans', '_patternSuppressedNotified')

# Round-4 sections (2026-11-27): what remained of the blob after stage 2.
STAGE3_TABLES = ('memory_ledger', 'work_queue', 'reject_ledger',
                 'repair_failure_log', 'abandoned_card', 'quarantine_log',
                 'used_name', 'sim_pause_window', 'wiki_section',
                 'replan_capped_logged_at', 'agent_failure_memory',
                 'team_lesson', 'scheduled_item', 'workflow_template',
                 'agent_template', 'team', 'work_program',
                 'pattern_fix_state', 'avatar_task_at')
STAGE3_KEYS = ('memoryLedger', 'workQueue', 'rejectLedger',
               '_repairFailureLog', '_abandonedCards', '_quarantineLog',
               '_usedNames', '_simPauseWindows', 'wiki',
               '_replanCappedLoggedAt', 'agentFailureMemory', 'teamLessons',
               'scheduledItems', 'workflowTemplates', 'templates', 'teams',
               'programs', '_patternFixState', '_avatarTaskAt')


def _with_round2(state):
    state['growthPlans'] = {
        'ada': [{'goal': 'build', 'route': 'staffed'}],
        'ben': [{'goal': 'paint', 'route': 'staffed'}],
    }
    state['helpSignals'] = [
        {'id': 'help-1', 'agentId': 'ada'},
        {'id': 'help-2', 'agentId': 'ben'},
    ]
    state['issues'] = {
        'ADA-1': {'id': 'ADA-1', 'text': 'door sticks'},
        'BEN-2': {'id': 'BEN-2', 'text': 'window draft'},
    }
    state['wakeQueue'] = [
        {'id': 'wake-2', 'at': 200},
        {'id': 'wake-1', 'at': 100},
    ]
    state['backlogRequests'] = [
        {'id': 'wrq-1', 'title': 'wire the shed'},
    ]
    state['completedDeliverables'] = [
        {'id': 'task-4', 'title': 'Great parser', 'grade': 8.5},
    ]
    return state


def _with_stage2(state):
    state['firedAgents'] = {
        'liam': {'name': 'Liam', 'firedAt': 1792588474808,
                 'reason': 'never reachable'},
    }
    state['storyCostTracking'] = {
        'stories': {'task-6042': {'used': 1.5, 'calls': 3}},
        'scrumMasters': {'ruth': {'used': 14.58, 'calls': 2}},
    }
    state['_feedback'] = {
        'cora': [{'text': 'Your deliverable failed the rubric',
                  'source': 'pipeline'}],
    }
    state['_cardFailureNotified'] = {
        'observatory|Distill recent think tank k': 1793819623028}
    state['_lastEmailAt'] = {'card_report:task-4': 1791683180424,
                             'spike_done': 1795733599755}
    state['_repairLoopNotified'] = {'repair_loop:[BUG] Re-open task-3': 1793004915876}
    state['_staleWorkReplans'] = {'(Read and analyze all StealthX sites': 1}
    state['_patternSuppressedNotified'] = {'issue:NORA-209': 1793804310553}
    return state


def _with_stage3(state):
    state['memoryLedger'] = [
        {'ts': 1791862892393, 'actor': 'distill',
         'pageId': 'state-of-knowledge',
         'title': 'Think tank knowledge state', 'claim': 'x'},
        {'ts': 1791862892500, 'actor': 'distill',
         'pageId': 'state-of-knowledge',
         'title': 'Think tank knowledge state', 'claim': 'y'},
    ]
    state['workQueue'] = [
        {'title': 'card-a', 'requestId': 'wrq-1', 'taskType': 'code'},
        {'title': 'card-b', 'taskType': 'code'},
        {'title': 'card-c', 'taskType': 'write'},
    ]
    state['rejectLedger'] = [
        {'atMs': 1795194531026, 'taskId': 'task-35448',
         'title': '[GOVERN] review', 'kind': 'review', 'note': 'no',
         'reviewer': 'ben', 'room': 'dev', 'teamId': 'nora',
         'workType': 'code'},
    ]
    state['_repairFailureLog'] = [
        {'key': 'title:Implement fix', 'root': 'Implement fix',
         'taskId': 'task-1', 'ts': 1795194531026},
    ]
    state['_abandonedCards'] = [
        {'at': 1793054650525, 'attempts': 3, 'room': 'pressoffice',
         'taskType': 'code', 'teamId': 'nora', 'title': 'Add door map data'},
    ]
    state['_quarantineLog'] = [
        {'at': 1793054650525, 'by': 'operator', 'cards': ['task-421'],
         'reason': 'spam', 'workQueue': []},
    ]
    state['_usedNames'] = ['ada', 'ben', 'cora']
    state['_simPauseWindows'] = [[1795704958469, 1795705161013]]
    state['wiki'] = {
        'pages': {'state-of-knowledge': {'title': 'Think', 'body': '...'}}}
    state['_replanCappedLoggedAt'] = {'task-24842': 1794937323647}
    state['agentFailureMemory'] = {
        'eli': {'count': 44, 'lastFailedAt': 1795205420188}}
    state['teamLessons'] = {
        "(nora, code)": {'lesson': 'do not re-file the review'}}
    state['scheduledItems'] = [
        {'id': 'sch-rvp-telegram-digest', 'title': 'digest',
         'cadence': 'daily', 'enabled': True, 'instructions': 'x',
         'ownerRole': 'chief', 'lastRunAtMs': 0},
    ]
    state['workflowTemplates'] = {
        'wft-1b66470b996ae6b3': {'id': 'wft-1b66470b996ae6b3',
                                 'name': 'Research and write'}}
    state['templates'] = {
        'Research': {'mission': 'Investigate assigned topics'}}
    state['teams'] = [
        {'id': 'ben', 'name': "The Writers' Room", 'directorId': 'ben',
         'purpose': 'Modernize'},
    ]
    state['programs'] = [
        {'id': 'prog-1', 'title': 'Integrate the dankornas corpus',
         'status': 'active'},
    ]
    state['_patternFixState'] = {
        "(None, 'code', 'rejected')": {'team': None, 'workType': 'code',
                                       'kind': 'rejected'}}
    state['_avatarTaskAt'] = {'marta': 1795552041826}
    return state


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
            for table in ('roster_agent', 'live_agent', 'inbox_message',
                          'sim_agent_snapshot') + ROUND2_TABLES \
                    + STAGE2_TABLES + STAGE3_TABLES:
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

    def test_tick_snapshot_leaves_the_blob_and_diff_writes(self):
        # The tick's sim['agents'] snapshot is row-extracted too: it is
        # rebuilt every tick and persisted on every save, so at scale it must
        # cost only the CHANGED agents per save, not the whole headcount.
        state = _base_state()
        state['sim'] = {'tick': 1, 'owner': 'server', 'agents': {
            'ada': {'x': 1, 'y': 2, 'busy': False, 'inRoom': None,
                    'offDuty': False, 'visible': True, 'dir': 'south',
                    'task': None, 'pathActive': False},
            'ben': {'x': 3, 'y': 4, 'busy': True, 'inRoom': 'workroom',
                    'offDuty': False, 'visible': True, 'dir': 'east',
                    'task': 't9', 'pathActive': False},
        }}
        serve.save_state_to_db(copy.deepcopy(state))
        blob = _raw_blob()
        self.assertNotIn('agents', blob.get('sim') or {})
        self.assertEqual(blob['sim']['tick'], 1)  # sim's other keys survive
        back = serve.get_state_from_db()
        self.assertEqual(back['sim']['agents'], state['sim']['agents'])
        # Second save, one agent moved: only that row is rewritten.
        state['sim']['agents']['ada']['x'] = 9
        state['sim']['tick'] = 2
        serve.save_state_to_db(copy.deepcopy(state))
        with serve._db() as conn:
            rows = {rid: json.loads(b) for rid, b in conn.execute(
                'SELECT id, blob FROM sim_agent_snapshot')}
        self.assertEqual(rows['ada']['x'], 9)
        self.assertEqual(rows['ben']['x'], 3)

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


class Round2StateSplitTest(StateSplitTest):
    """The 2026-11-01 sections: growthPlans (dict of lists), helpSignals,
    issues, wakeQueue, backlogRequests, completedDeliverables."""

    def test_round_trip_preserves_exact_shapes(self):
        state = _with_round2(_base_state())
        serve.save_state_to_db(copy.deepcopy(state))
        back = serve.get_state_from_db()
        for key in ROUND2_KEYS:
            self.assertEqual(back[key], state[key], key)

    def test_blob_drops_round2_keys_and_rows_carry_them(self):
        serve.save_state_to_db(_with_round2(_base_state()))
        blob = _raw_blob()
        for key in ROUND2_KEYS:
            self.assertNotIn(key, blob, key)
        with serve._db() as conn:
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM growth_plan').fetchone()[0], 2)
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM help_signal').fetchone()[0], 2)
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM tracked_issue').fetchone()[0], 2)
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM wake_entry').fetchone()[0], 2)
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM backlog_request').fetchone()[0], 1)
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM completed_deliverable').fetchone()[0], 1)

    def test_wake_queue_order_survives_round_trip(self):
        serve.save_state_to_db(_with_round2(_base_state()))
        back = serve.get_state_from_db()
        self.assertEqual([e['id'] for e in back['wakeQueue']],
                         ['wake-2', 'wake-1'])

    def test_diff_save_rewrites_only_changed_growth_plan_rows(self):
        serve.save_state_to_db(_with_round2(_base_state()))
        with serve._db() as conn:
            before = dict(conn.execute(
                'SELECT id, updated_at FROM growth_plan').fetchall())
        time.sleep(0.02)
        state = serve.get_state_from_db()
        state['growthPlans']['ada'].append(
            {'goal': 'narrower', 'route': 'staffed'})
        serve.save_state_to_db(state)
        with serve._db() as conn:
            after = dict(conn.execute(
                'SELECT id, updated_at FROM growth_plan').fetchall())
        self.assertEqual(after['ben'], before['ben'])
        self.assertNotEqual(after['ada'], before['ada'])

    def test_growth_plan_list_value_is_a_row_blob_not_a_row_value(self):
        serve.save_state_to_db(_with_round2(_base_state()))
        with serve._db() as conn:
            row = conn.execute(
                'SELECT blob FROM growth_plan WHERE id = ?',
                ('ada',)).fetchone()
        self.assertEqual(json.loads(row[0]),
                         [{'goal': 'build', 'route': 'staffed'}])


class Stage2BookkeepingSplitTest(StateSplitTest):
    """The 2026-11-27 sections: the bookkeeping dicts. firedAgents /
    storyCostTracking row with the existing 'dict' shape, _feedback with
    'dict-lists', and the five notify/counter dicts with the new
    'dict-scalar' shape (the row blob is the scalar value itself)."""

    def test_round_trip_preserves_exact_shapes(self):
        state = _with_stage2(_base_state())
        serve.save_state_to_db(copy.deepcopy(state))
        back = serve.get_state_from_db()
        for key in STAGE2_KEYS:
            self.assertEqual(back[key], state[key], key)
        # json round-trips 1 as int and could alias True == 1 in a dict
        # comparison, so assert the type explicitly for a counter value.
        self.assertIsInstance(
            next(iter(back['_staleWorkReplans'].values())), int)

    def test_blob_drops_stage2_keys_and_rows_carry_them(self):
        serve.save_state_to_db(_with_stage2(_base_state()))
        blob = _raw_blob()
        for key in STAGE2_KEYS:
            self.assertNotIn(key, blob, key)
        with serve._db() as conn:
            for table, count in (('fired_agent', 1),
                                 ('story_cost_tracking', 2),
                                 ('feedback_note', 1),
                                 ('card_failure_notified', 1),
                                 ('last_email_at', 2),
                                 ('repair_loop_notified', 1),
                                 ('stale_work_replan', 1),
                                 ('pattern_suppressed_notified', 1)):
                self.assertEqual(
                    conn.execute(f'SELECT COUNT(*) FROM {table}')
                    .fetchone()[0], count, table)

    def test_scalar_row_blob_is_the_scalar_itself(self):
        serve.save_state_to_db(_with_stage2(_base_state()))
        with serve._db() as conn:
            blob = conn.execute(
                'SELECT blob FROM last_email_at WHERE id = ?',
                ('spike_done',)).fetchone()
        self.assertEqual(blob[0], '1795733599755')

    def test_legacy_blob_migrates_on_first_save(self):
        # The state this slice exists for: production's blob still carries
        # all eight bookkeeping keys with empty row tables. Reads serve the
        # blob copy; the first save moves the keys into rows atomically.
        legacy = _with_stage2(_base_state())
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO kv_state (id, blob, updated_at) VALUES (1, ?, ?)',
                (json.dumps(legacy), time.time()))
        back = serve.get_state_from_db()
        self.assertEqual(back['_lastEmailAt'], legacy['_lastEmailAt'])
        serve.save_state_to_db(serve.get_state_from_db())
        blob = _raw_blob()
        for key in STAGE2_KEYS:
            self.assertNotIn(key, blob, key)
        again = serve.get_state_from_db()
        self.assertEqual(again['_lastEmailAt'], legacy['_lastEmailAt'])
        self.assertEqual(again['_cardFailureNotified'],
                         legacy['_cardFailureNotified'])
        self.assertEqual(again['_feedback'], legacy['_feedback'])
        self.assertEqual(again['firedAgents'], legacy['firedAgents'])

    def test_diff_save_rewrites_only_changed_scalar_rows(self):
        serve.save_state_to_db(_with_stage2(_base_state()))
        with serve._db() as conn:
            before = dict(conn.execute(
                'SELECT id, updated_at FROM last_email_at').fetchall())
        time.sleep(0.02)
        state = serve.get_state_from_db()
        state['_lastEmailAt']['spike_done'] += 1
        serve.save_state_to_db(state)
        with serve._db() as conn:
            after = dict(conn.execute(
                'SELECT id, updated_at FROM last_email_at').fetchall())
        self.assertEqual(after['card_report:task-4'], before['card_report:task-4'])
        self.assertGreater(after['spike_done'], before['spike_done'])

    def test_unrowable_scalar_value_stays_in_blob(self):
        # A notify dict whose value is a dict (shape drift at write time)
        # cannot be rowed fail-closed: the blob keeps its copy, the rows
        # stay -- no information is lost either way.
        state = _with_stage2(_base_state())
        state['_lastEmailAt']['weird'] = {'used': 1.0}
        serve.save_state_to_db(state)
        blob = _raw_blob()
        self.assertIn('_lastEmailAt', blob)
        back = serve.get_state_from_db()
        self.assertEqual(back['_lastEmailAt'], state['_lastEmailAt'])

    def test_absent_key_clears_the_section(self):
        serve.save_state_to_db(_with_stage2(_base_state()))
        state = serve.get_state_from_db()
        state.pop('_lastEmailAt')
        serve.save_state_to_db(state)
        with serve._db() as conn:
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM last_email_at').fetchone()[0], 0)
        back = serve.get_state_from_db()
        self.assertFalse(back.get('_lastEmailAt'))


class Stage3TailSplitTest(StateSplitTest):
    """The 2026-11-27 round 4: what remained of the blob after stage 2.
    memoryLedger rows by its unique per-entry ts ('list-ts'); the no-id
    lists (workQueue, rejectLedger, _repairFailureLog, _abandonedCards,
    _quarantineLog, _usedNames, _simPauseWindows) row by POSITION
    ('list-seq'); the id-carrying lists and dict sections use the
    existing shapes."""

    def test_round_trip_preserves_exact_shapes(self):
        state = _with_stage3(_base_state())
        serve.save_state_to_db(copy.deepcopy(state))
        back = serve.get_state_from_db()
        for key in STAGE3_KEYS:
            self.assertEqual(back[key], state[key], key)
        # list-seq entries are not limited to dicts: strings and lists row.
        self.assertIsInstance(back['_usedNames'][0], str)
        self.assertIsInstance(back['_simPauseWindows'][0], list)

    def test_blob_drops_stage3_keys_and_rows_carry_them(self):
        serve.save_state_to_db(_with_stage3(_base_state()))
        blob = _raw_blob()
        for key in STAGE3_KEYS:
            self.assertNotIn(key, blob, key)
        with serve._db() as conn:
            for table, count in (('memory_ledger', 2), ('work_queue', 3),
                                 ('reject_ledger', 1),
                                 ('repair_failure_log', 1),
                                 ('abandoned_card', 1),
                                 ('quarantine_log', 1), ('used_name', 3),
                                 ('sim_pause_window', 1),
                                 ('wiki_section', 1),
                                 ('replan_capped_logged_at', 1),
                                 ('agent_failure_memory', 1),
                                 ('team_lesson', 1), ('scheduled_item', 1),
                                 ('workflow_template', 1),
                                 ('agent_template', 1), ('team', 1),
                                 ('work_program', 1),
                                 ('pattern_fix_state', 1),
                                 ('avatar_task_at', 1)):
                self.assertEqual(
                    conn.execute(f'SELECT COUNT(*) FROM {table}')
                    .fetchone()[0], count, table)

    def test_legacy_blob_migrates_on_first_save(self):
        legacy = _with_stage3(_base_state())
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO kv_state (id, blob, updated_at) VALUES (1, ?, ?)',
                (json.dumps(legacy), time.time()))
        back = serve.get_state_from_db()
        self.assertEqual(back['workQueue'], legacy['workQueue'])
        serve.save_state_to_db(serve.get_state_from_db())
        blob = _raw_blob()
        for key in STAGE3_KEYS:
            self.assertNotIn(key, blob, key)
        again = serve.get_state_from_db()
        self.assertEqual(again['memoryLedger'], legacy['memoryLedger'])
        self.assertEqual(again['workQueue'], legacy['workQueue'])
        self.assertEqual(again['_usedNames'], legacy['_usedNames'])

    def test_append_writes_only_the_new_position(self):
        serve.save_state_to_db(_with_stage3(_base_state()))
        with serve._db() as conn:
            before = dict(conn.execute(
                'SELECT id, updated_at FROM reject_ledger').fetchall())
        time.sleep(0.02)
        state = serve.get_state_from_db()
        state['rejectLedger'].append(
            {'atMs': 1795194531999, 'taskId': 'task-99', 'title': 'late',
             'kind': 'review', 'note': '', 'reviewer': 'ben', 'room': 'dev',
             'teamId': 'nora', 'workType': 'code'})
        serve.save_state_to_db(state)
        with serve._db() as conn:
            after = dict(conn.execute(
                'SELECT id, updated_at FROM reject_ledger').fetchall())
        self.assertEqual(after['0'], before['0'])
        self.assertNotIn('1', before)
        self.assertIn('1', after)

    def test_mid_list_removal_rewrites_the_shifted_tail(self):
        serve.save_state_to_db(_with_stage3(_base_state()))
        with serve._db() as conn:
            before = dict(conn.execute(
                'SELECT id, updated_at FROM work_queue').fetchall())
        time.sleep(0.02)
        state = serve.get_state_from_db()
        state['workQueue'].pop(0)
        serve.save_state_to_db(state)
        with serve._db() as conn:
            after = dict(conn.execute(
                'SELECT id, updated_at FROM work_queue').fetchall())
        # Every surviving position now holds different content, so both
        # rows rewrite and the tail row is deleted.
        self.assertNotIn('2', after)
        self.assertGreater(after['0'], before['0'])
        self.assertGreater(after['1'], before['1'])
        back = serve.get_state_from_db()
        self.assertEqual([c['title'] for c in back['workQueue']],
                         ['card-b', 'card-c'])

    def test_memory_ledger_rows_by_ts_and_diffs_by_content(self):
        serve.save_state_to_db(_with_stage3(_base_state()))
        with serve._db() as conn:
            before = dict(conn.execute(
                'SELECT id, updated_at FROM memory_ledger').fetchall())
        time.sleep(0.02)
        state = serve.get_state_from_db()
        # Same ts, new content: the row identity is ts, so this REWRITES
        # that one row.
        state['memoryLedger'][0]['claim'] = 'x2'
        # New ts: appends one row.
        state['memoryLedger'].append(
            {'ts': 1791862899999, 'actor': 'distill',
             'pageId': 'state-of-knowledge', 'title': 'Think tank',
             'claim': 'z'})
        serve.save_state_to_db(state)
        with serve._db() as conn:
            after = dict(conn.execute(
                'SELECT id, updated_at FROM memory_ledger').fetchall())
        self.assertGreater(after['1791862892393'], before['1791862892393'])
        self.assertEqual(after['1791862892500'], before['1791862892500'])
        self.assertIn('1791862899999', after)
        back = serve.get_state_from_db()
        self.assertEqual(back['memoryLedger'][0]['claim'], 'x2')
        self.assertEqual(len(back['memoryLedger']), 3)

    def test_duplicate_ts_fails_closed(self):
        # Two entries sharing a ts would collapse into one row and silently
        # lose an entry; the rower must refuse the section instead.
        state = _with_stage3(_base_state())
        state['memoryLedger'].append(dict(state['memoryLedger'][0]))
        serve.save_state_to_db(state)
        blob = _raw_blob()
        self.assertIn('memoryLedger', blob)

    def test_unrowable_list_ts_entry_stays_in_blob(self):
        state = _with_stage3(_base_state())
        state['memoryLedger'][0].pop('ts')
        serve.save_state_to_db(state)
        blob = _raw_blob()
        self.assertIn('memoryLedger', blob)
        back = serve.get_state_from_db()
        self.assertEqual(back['memoryLedger'], state['memoryLedger'])

    def test_absent_key_clears_the_section(self):
        serve.save_state_to_db(_with_stage3(_base_state()))
        state = serve.get_state_from_db()
        state.pop('workQueue')
        serve.save_state_to_db(state)
        with serve._db() as conn:
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM work_queue').fetchone()[0], 0)
        back = serve.get_state_from_db()
        self.assertFalse(back.get('workQueue'))

    def test_one_stale_section_bulk_loads_every_section(self):
        # The cold-read fix: a single stale section cache triggers one
        # UNION ALL round trip that fills ALL section caches, not just the
        # one asked for. Spot-check every shape family afterwards.
        expected = _with_stage3(_base_state())
        serve.save_state_to_db(expected)
        _reset_extracted_caches()
        with serve._EXTRACTED_LOCK:
            serve._extracted_rows_load_locked(
                'memory_ledger', serve._EXTRACTED_CACHE['memoryLedger'])
        for key, cache in serve._EXTRACTED_CACHE.items():
            self.assertTrue(cache['loaded'], key)
            self.assertEqual(cache['db'], serve.DB_PATH, key)
        back = serve.get_state_from_db()
        for key in ('memoryLedger', 'workQueue', '_usedNames',
                    '_avatarTaskAt', '_patternFixState', 'templates'):
            self.assertEqual(back[key], expected[key], key)
