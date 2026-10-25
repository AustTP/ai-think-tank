"""Pi Durable conformance: the durability promises the tank actually makes."""
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve
import sim
import core.tool_policy as tool_policy


class DurabilityContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='durability-test-')
        self.patcher = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_external_write_replays_completed_and_refuses_pending(self):
        calls = []

        def fn():
            calls.append(1)
            return {'ok': True}, None

        first = serve.external_write_once('test-kind', 'one', fn)
        second = serve.external_write_once('test-kind', 'one', fn)
        self.assertEqual(first, ({'ok': True}, None, False))
        self.assertEqual(second, ({'ok': True}, None, True))
        self.assertEqual(len(calls), 1)
        with serve._db() as conn:
            conn.execute('INSERT OR REPLACE INTO external_writes '
                         '(kind, key, result, created_at) VALUES (?, ?, ?, ?)',
                         ('test-kind', 'pending', '', time.time()))
        result, error, replayed = serve.external_write_once(
            'test-kind', 'pending', lambda: ({'ok': True}, None))
        self.assertIsNone(result)
        self.assertIn('already in progress', error)
        self.assertFalse(replayed)

    def test_task_checkpoint_roundtrip_ttl_and_clear(self):
        self.assertTrue(serve.save_task_checkpoint('t1', 'execute', {'plan': 'p', 'pending': []}))
        loaded = serve.load_task_checkpoint('t1')
        self.assertEqual(loaded['phase'], 'execute')
        self.assertEqual(loaded['plan'], 'p')
        with serve._db() as conn:
            conn.execute('UPDATE task_checkpoints SET updated_at=0 WHERE task_id=?', ('t1',))
        self.assertIsNone(serve.load_task_checkpoint('t1', max_age_s=1))
        self.assertTrue(serve.clear_task_checkpoint('t1'))
        self.assertIsNone(serve.load_task_checkpoint('t1'))

    def test_tool_tiers_fail_closed(self):
        self.assertEqual(tool_policy.tool_trust_tier('read_library_file'), 'research')
        self.assertEqual(tool_policy.tool_trust_tier('browser_step'), 'prepare')
        self.assertEqual(tool_policy.tool_trust_tier('create_calendar_event'), 'commit')
        self.assertEqual(tool_policy.tool_trust_tier('totally_new_tool'), 'commit')
        self.assertEqual(tool_policy.tool_replay_safety('totally_new_tool'), 'unsafe')

    def test_commit_tool_call_replays_and_errors_are_not_cached(self):
        calls = []

        def raw(name, args):
            calls.append(name)
            return 'ok'

        key = f'task-{time.time()}'
        first = serve.idempotent_tool_call('a', key, raw, 'create_calendar_event', {'x': 1})
        second = serve.idempotent_tool_call('a', key, raw, 'create_calendar_event', {'x': 1})
        self.assertEqual(first, 'ok')
        self.assertIn('replayed from requestId', second)
        self.assertEqual(calls, ['create_calendar_event'])
        err_key = f'task-err-{time.time()}'

        def fails(name, args):
            calls.append(name)
            return 'Could not create the event'

        serve.idempotent_tool_call('a', err_key, fails, 'create_calendar_event', {'x': 2})
        serve.idempotent_tool_call('a', err_key, fails, 'create_calendar_event', {'x': 2})
        self.assertEqual(calls.count('create_calendar_event'), 3)

    def test_delegated_card_carries_fork_provenance(self):
        state = {'workQueue': []}
        sim.queue_work(state, [{'title': 'fork', 'parentTaskId': 'supervisor-card'}])
        self.assertEqual(state['workQueue'][0]['parentTaskId'], 'supervisor-card')


if __name__ == '__main__':
    unittest.main()
