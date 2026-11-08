"""Dispatch stamps a heartbeat at thread entry: a run that started is alive.

The router's hybrid pre-pass is a real model call that can sit in free-pool
retry for minutes under load; before the thread-entry stamp, that window read
as heartbeat-less and the completion loop abandoned live runs with
Severity: HIGH pushes (2026-11-08, 35 in one hour during a 57-worker burst)."""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import sim  # noqa: E402
import serve  # noqa: E402  # warm: the dispatch thread imports serve first


class DispatchHeartbeat(unittest.TestCase):
    def _state(self):
        return {'sim': {'owner': 'server'}, 'agents': {}, 'tasks': {},
                'workQueue': [], 'agentRoster': []}

    def test_thread_entry_stamps_heartbeat_before_executor(self):
        state = self._state()
        task = {'id': 'task-hb', 'title': 'X', 'room': 'observatory',
                'taskType': 'research'}
        seen = {}

        def slow_executor(snapshot, agent_id, task, base_ctx=None):
            # The heartbeat must already be stamped by the time the
            # executor body (which sits behind the hybrid pre-pass) runs.
            seen['hb'] = task.get('_contentHeartbeat')
            seen['reg'] = sim._CONTENT_HEARTBEATS.get('task-hb')

        started = threading.Event()
        orig_executor = slow_executor

        def executor(snapshot, agent_id, task, base_ctx=None):
            started.wait(5)
            orig_executor(snapshot, agent_id, task, base_ctx)

        ok = sim._dispatch_content_work(executor, state, 'cora', task,
                                        time.time())
        self.assertTrue(ok)
        # Give the thread a moment to reach the stamp (thread cold-start
        # plus perception build can take a few seconds).
        for _ in range(500):
            if task.get('_contentHeartbeat') is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(task.get('_contentHeartbeat'))
        self.assertIsNotNone(sim._CONTENT_HEARTBEATS.get('task-hb'))
        started.set()


if __name__ == '__main__':
    unittest.main()
