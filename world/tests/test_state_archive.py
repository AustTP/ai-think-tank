"""Tests for splitting completed tasks out of the whole-blob kv_state write.

The live blob had grown to multi-MB of `done` tasks that were serialized on
every tick; only a handful of tasks were ever active. Completed tasks now live
in the task_archive table and are merged back on read, so state['tasks'] keeps
its exact shape. These tests pin that round-trip, the blob shrinking, and the
agent-directory sync throttle.
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

import serve  # noqa: E402


class _StateCase(unittest.TestCase):
    # Subclasses that call save_state_to_db set this so the real agents/ tree
    # is never written; the throttle tests need the real function, so they
    # leave it False.
    patch_sync = False

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-archive-')
        self._db_patch = unittest.mock.patch.object(
            serve, 'DB_PATH', os.path.join(self._tmp, 'test.db'))
        self._db_patch.start()
        serve.init_db()
        serve._TASK_ARCHIVE_CACHE.update({'db': None, 'loaded': False, 'tasks': {}})
        serve._LAST_AGENT_DIR_SYNC_AT = 0.0
        serve._LAST_AGENT_DIR_FINGERPRINT = None
        self._ttl_patch = None
        if getattr(self, 'patch_ttl', False):
            self._ttl_patch = unittest.mock.patch.object(serve, 'DECISION_CACHE_TTL_S', 3600)
            self._ttl_patch.start()
        self._sync_patch = None
        if self.patch_sync:
            self._sync_patch = unittest.mock.patch.object(serve, 'sync_agent_directories')
            self._sync_patch.start()

    def tearDown(self):
        if self._sync_patch:
            self._sync_patch.stop()
        if self._ttl_patch:
            self._ttl_patch.stop()
        self._db_patch.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _state(self):
        return {
            'tasks': {
                't-active': {'id': 't-active', 'status': 'working', 'title': 'live'},
                't-done': {'id': 't-done', 'status': 'done', 'title': 'finished',
                           'note': 'a big note'},
            },
            'agents': {},
            'agentRoster': [],
        }

    def _blob_tasks(self):
        with serve._db() as conn:
            row = conn.execute('SELECT blob FROM kv_state WHERE id = 1').fetchone()
        return json.loads(row[0])['tasks']


class TaskArchive(_StateCase):
    patch_sync = True

    def test_round_trip_preserves_active_and_done(self):
        serve.save_state_to_db(self._state())
        loaded = serve.get_state_from_db()
        self.assertEqual(set(loaded['tasks']), {'t-active', 't-done'})
        self.assertEqual(loaded['tasks']['t-done']['status'], 'done')

    def test_done_tasks_are_not_written_to_the_blob(self):
        serve.save_state_to_db(self._state())
        blob_tasks = self._blob_tasks()
        self.assertEqual(set(blob_tasks), {'t-active'})
        with serve._db() as conn:
            archived = dict(conn.execute('SELECT id, blob FROM task_archive').fetchall())
        self.assertEqual(set(archived), {'t-done'})
        self.assertEqual(json.loads(archived['t-done'])['title'], 'finished')

    def test_archive_survives_a_fresh_process_cache(self):
        serve.save_state_to_db(self._state())
        # Simulate a new process: drop the in-process cache.
        serve._TASK_ARCHIVE_CACHE.update({'db': None, 'loaded': False, 'tasks': {}})
        loaded = serve.get_state_from_db()
        self.assertEqual(set(loaded['tasks']), {'t-active', 't-done'})

    def test_mutation_of_a_done_task_is_rearchived(self):
        serve.save_state_to_db(self._state())
        state = serve.get_state_from_db()
        state['tasks']['t-done']['note'] = 'edited after done'
        serve.save_state_to_db(state)
        loaded = serve.get_state_from_db()
        self.assertEqual(loaded['tasks']['t-done']['note'], 'edited after done')

    def test_un_done_task_overrides_the_archived_copy(self):
        serve.save_state_to_db(self._state())
        state = serve.get_state_from_db()
        state['tasks']['t-done']['status'] = 'working'  # reopened
        serve.save_state_to_db(state)
        loaded = serve.get_state_from_db()
        self.assertEqual(loaded['tasks']['t-done']['status'], 'working')

    def test_blob_shrinks_when_many_done_tasks_archive(self):
        state = {'tasks': {f'big-{i}': {'id': f'big-{i}', 'status': 'done',
                                        'note': 'x' * 2000} for i in range(200)},
                 'agents': {}, 'agentRoster': []}
        state['tasks']['live'] = {'id': 'live', 'status': 'working'}
        serve.save_state_to_db(state)
        with serve._db() as conn:
            blob_len = conn.execute('SELECT length(blob) FROM kv_state WHERE id = 1').fetchone()[0]
        # 200 * 2000 bytes of notes must not be in the blob.
        self.assertLess(blob_len, 5000)
        self.assertEqual(len(serve.get_state_from_db()['tasks']), 201)


class SyncThrottle(_StateCase):
    def test_throttled_sync_skips_within_the_interval(self):
        with unittest.mock.patch.object(serve, 'AGENT_DIR_SYNC_INTERVAL_S', 999), \
             unittest.mock.patch.object(serve, '_write_file') as wf:
            serve._LAST_AGENT_DIR_SYNC_AT = 0.0
            state = {'agents': {'a': {'name': 'A', 'role': 'r'}},
                     'agentRoster': [{'id': 'a'}], 'reports': []}
            serve.sync_agent_directories(state, throttle=True)
            first = wf.call_count
            self.assertGreater(first, 0)
            serve.sync_agent_directories(state, throttle=True)
            self.assertEqual(wf.call_count, first)  # second call throttled out

    def test_unthrottled_sync_always_runs(self):
        with unittest.mock.patch.object(serve, 'AGENT_DIR_SYNC_INTERVAL_S', 999), \
             unittest.mock.patch.object(serve, '_write_file') as wf:
            serve._LAST_AGENT_DIR_SYNC_AT = time.time()
            state = {'agents': {'a': {'name': 'A', 'role': 'r'}},
                     'agentRoster': [{'id': 'a'}], 'reports': []}
            serve.sync_agent_directories(state)  # default: no throttle
            first = wf.call_count
            serve.sync_agent_directories(state)
            self.assertEqual(wf.call_count, 2 * first)


class DecisionKeyInstrumentation(_StateCase):
    def test_records_and_reports_repeats(self):
        q = {'choice': {'instructions': 'judge X', 'criteria': {'a': 'A'}}}
        serve._record_decision_keys('m', {'messages': [], 'signals': {}}, q)
        serve._record_decision_keys('m', {'messages': [], 'signals': {}}, q)
        serve._record_decision_keys('m', {'messages': [], 'signals': {}},
                                    {'choice': {'instructions': 'other'}})
        rep = serve.decision_key_report('q')
        self.assertEqual(rep['calls'], 3)
        self.assertEqual(rep['distinct_keys'], 2)
        self.assertEqual(rep['repeat_calls'], 1)
        self.assertGreater(rep['repeat_pct'], 0.0)

    def test_question_and_full_request_keys_differ_on_state(self):
        q = {'choice': {'instructions': 'x'}}
        serve._record_decision_keys('m', {'messages': [], 'signals': {'a': 1}}, q)
        serve._record_decision_keys('m', {'messages': [], 'signals': {'a': 2}}, q)
        # Same question, different state: question-only repeats, full does not.
        self.assertEqual(serve.decision_key_report('q')['repeat_calls'], 1)
        self.assertEqual(serve.decision_key_report('r')['repeat_calls'], 0)

    def test_endpoint_reports_both_families(self):
        from starlette.testclient import TestClient
        serve._record_decision_keys('m', {}, {'choice': {'instructions': 'x'}})
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        r = c.get('/api/jev/decision-keys')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn('question_only', body)
        self.assertIn('full_request', body)
        self.assertEqual(body['question_only']['calls'], 1)


class DecisionCache(_StateCase):
    """The Jev decision cache: an identical request is served from decision_cache
    (no network, no spend, still taped as cached), different questions are not
    collapsed, TTL=0 disables it, and the report reflects real hits."""

    patch_ttl = True

    def _jev_response(self, choice='fire', confidence=0.9, cost=0.0001):
        return {'answers': {'q': {'choice': choice, 'confidence': confidence}},
                'usage': {'cost': cost}}

    def _questions(self, instruction):
        return {'choice': {'type': 'choice', 'instructions': instruction,
                           'criteria': {'fire': 'drop them', 'keep': 'retain'}}}

    def test_identical_request_is_served_from_cache(self):
        with unittest.mock.patch('serve._urlopen_with_resilience',
                                 return_value=json.dumps(self._jev_response()).encode()) as net:
            first = serve._call_openrouter_decision_sync('typesafe/jev-1.13', {}, self._questions('Hire or fire?'))
            second = serve._call_openrouter_decision_sync('typesafe/jev-1.13', {}, self._questions('Hire or fire?'))
        self.assertEqual(net.call_count, 1, 'the repeat must not hit the network')
        self.assertIsNotNone(first.pop('trace_id', None))
        self.assertNotIn('cached', first, 'a fresh miss is not marked cached')
        self.assertEqual(first, self._jev_response())
        second.pop('trace_id', None)
        self.assertEqual(second.pop('cached', None), True)
        self.assertEqual(second, self._jev_response())
        with serve._db() as conn:
            rows = conn.execute(
                'SELECT ok, cost, model FROM decision_tape ORDER BY id').fetchall()
        self.assertEqual(len(rows), 2, 'both the miss and the hit are taped')
        self.assertEqual(rows[0][0], 1)
        self.assertEqual(rows[1][0], 1)
        self.assertEqual(rows[1][1], 0.0, 'a cached decision accrues no cost')
        self.assertEqual(rows[1][2], 'typesafe/jev-1.13')

    def test_cached_hit_accrues_no_spend(self):
        with unittest.mock.patch('serve._urlopen_with_resilience',
                                 return_value=json.dumps(self._jev_response()).encode()):
            serve._call_openrouter_decision_sync('m', {}, self._questions('x'))
            with unittest.mock.patch.object(serve, '_accrue_spend') as spend:
                data = serve._call_openrouter_decision_sync('m', {}, self._questions('x'))
        spend.assert_not_called()
        self.assertTrue(data.get('cached'))

    def test_different_questions_do_not_hit(self):
        with unittest.mock.patch('serve._urlopen_with_resilience',
                                 return_value=json.dumps(self._jev_response()).encode()) as net:
            serve._call_openrouter_decision_sync('m', {}, self._questions('question A'))
            serve._call_openrouter_decision_sync('m', {}, self._questions('question B'))
        self.assertEqual(net.call_count, 2)

    def test_ttl_zero_disables_the_cache(self):
        with unittest.mock.patch.object(serve, 'DECISION_CACHE_TTL_S', 0):
            with unittest.mock.patch('serve._urlopen_with_resilience',
                                     return_value=json.dumps(self._jev_response()).encode()) as net:
                serve._call_openrouter_decision_sync('m', {}, self._questions('x'))
                serve._call_openrouter_decision_sync('m', {}, self._questions('x'))
            self.assertEqual(net.call_count, 2)

    def test_report_reflects_hits(self):
        with unittest.mock.patch('serve._urlopen_with_resilience',
                                 return_value=json.dumps(self._jev_response()).encode()):
            serve._call_openrouter_decision_sync('m', {}, self._questions('repeat me'))
            serve._call_openrouter_decision_sync('m', {}, self._questions('repeat me'))
        rep = serve.decision_cache_report()
        self.assertGreaterEqual(rep['hits'], 1)
        self.assertGreater(rep['hit_pct'], 0.0)
        self.assertEqual(rep['ttl_s'], 3600)


class StaleReplanKeyIsJsonSafe(unittest.TestCase):
    """The stale-work sweep keys its replan counter map by (title, room). That
    map lives in kv_state, so a tuple key made every subsequent save fail with
    'keys must be str... not tuple'. The keys must be JSON-safe strings."""

    def test_sweep_produces_string_keys_and_state_round_trips(self):
        import sim
        now = time.time()
        now_ms = int(now * 1000)
        state = {
            'tasks': {'t1': {'id': 't1', 'status': 'walking', 'title': 'wedge',
                             'room': 'lab', 'openedAt': now_ms - 10 ** 12}},
            'agents': {'a': {'id': 'a', 'task': 't1', 'busy': True}},
            'agentRoster': [{'id': 'a', 'role': 'dev'}],
            'workQueue': [], 'teams': [], 'reports': [],
        }
        sim._stale_work_step(state, now, now_ms)
        keys = list(state['_staleWorkReplans'].keys())
        self.assertTrue(keys)
        for k in keys:
            self.assertIsInstance(k, str)
        json.dumps(state)  # must not raise


class FreeSpikeWeekKeysSurviveRoundTrip(unittest.TestCase):
    """The free-spike counters freeSpikeGlobal / freeSpikeRooms are keyed by
    integer week numbers, but every save/load JSON round-trips object keys to
    strings, so _file_free_spike's `k < week - 1` age comparison raised on
    str-vs-int. The maps must be coerced back to int keys at entry."""

    def test_stringified_week_keys_do_not_break_free_spike(self):
        import sim
        now = time.time()
        now_ms = int(now * 1000)
        week = sim._free_spike_week(now_ms)
        state = {
            'agents': {'ada': {'id': 'ada', 'offDuty': False, 'busy': False,
                               'task': None}},
            'tasks': {},
            'freeSpikeGlobal': {str(week): 2, str(week - 2): 1},  # string keys
            'freeSpikeRooms': {str(week - 2): ['observatory']},
            'freeSpikeUsed': {'ada': [week, 0]},  # round-tripped tuple -> list
        }
        r = sim._file_free_spike(state, 'ada', {'room': 'observatory',
                                                'title': 'x'}, now_ms)
        self.assertEqual(r, 1)
        self.assertEqual(state['freeSpikeGlobal'][week], 3)
        self.assertIsInstance(state['freeSpikeGlobal'][week], int)
        self.assertIn('observatory', state['freeSpikeRooms'][week])
        self.assertNotIn(week - 2, state['freeSpikeGlobal'],
                         'stale week buckets are pruned after coercion')
        self.assertNotIn(week - 2, state['freeSpikeRooms'])


if __name__ == '__main__':
    unittest.main()
