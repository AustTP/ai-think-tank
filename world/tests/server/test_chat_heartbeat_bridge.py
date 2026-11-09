"""The /api/chat in-flight heartbeat bridge (2026-11-08).

A chat call carrying a taskId IS live work on that task. The upstream call
can queue behind the free pool for minutes -- longer than the content
heartbeat's stale threshold -- and executor-level keepalives do not cover
every call path (the hybrid pre-pass, tool-side calls). The transport now
keeps the task's content heartbeat fresh for exactly the request's duration,
so an in-flight run waiting on a queued model call is never abandoned as
heartbeat-less."""
import os
import sys
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
import sim  # noqa: E402
import test_serve_helpers as _helpers  # noqa: E402


def setUpModule():
    _helpers.setUpModule()
    _helpers._AUTH_PATCHER = mock.patch.object(
        serve, '_valid_agent_key_presented', return_value=True)
    _helpers._AUTH_PATCHER.start()


def tearDownModule():
    _helpers._AUTH_PATCHER.stop()
    _helpers.tearDownModule()


def _payload():
    return {'choices': [{'message': {'content': 'a reply'},
                         'finish_reason': 'stop'}],
            'usage': {'cost': 0.001}}


class ChatHeartbeatBridge(unittest.TestCase):
    def setUp(self):
        self._patches = mock.patch.multiple(
            serve,
            verify_agent_key=mock.Mock(return_value=True),
            OPENROUTER_API_KEY='sk-test',
            credits_pause_active=mock.Mock(return_value=False),
            log_action=mock.Mock(),
            check_rate_limit=mock.Mock(return_value=True),
            _task_budget_exhausted=mock.Mock(return_value=False),
            _task_unpriced_blocked=mock.Mock(return_value=False),
            _agent_spend_cap_exceeded=mock.Mock(return_value=False),
            _director_budget_gate=mock.Mock(return_value=None),
        )
        self._patches.start()
        self.client = TestClient(serve.app)

    def tearDown(self):
        self._patches.stop()

    def test_chat_call_keeps_task_heartbeat_fresh(self):
        tid = 'task-hbbridge'
        seen_during = {}

        def slow_call(*a, **k):
            # Simulate the upstream queue wait. The keepalive (30s interval)
            # bumps at entry and on exit; the registry entry must be fresh
            # WHILE the call is still in flight.
            deadline = time.time() + 0.4
            while time.time() < deadline:
                ts = sim._CONTENT_HEARTBEATS.get(tid)
                if ts is not None:
                    seen_during['ts'] = ts
                time.sleep(0.05)
            return _payload()

        sim._CONTENT_HEARTBEATS.pop(tid, None)
        with mock.patch.object(serve, '_call_openrouter_sync', slow_call):
            r = self.client.post('/api/chat', json={
                'model': 'x', 'messages': [{'role': 'user', 'content': 'hi'}],
                'agentId': 'cora', 'taskId': tid, 'max_tokens': 20,
            })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(seen_during, 'heartbeat was never bumped during the call')
        # And the keepalive stopped after the request: no further bumps.
        after = sim._CONTENT_HEARTBEATS.get(tid)
        time.sleep(0.15)
        self.assertEqual(sim._CONTENT_HEARTBEATS.get(tid), after)

    def test_chat_without_task_id_never_touches_the_registry(self):
        before = dict(sim._CONTENT_HEARTBEATS)
        with mock.patch.object(serve, '_call_openrouter_sync',
                               mock.Mock(return_value=_payload())):
            r = self.client.post('/api/chat', json={
                'model': 'x', 'messages': [{'role': 'user', 'content': 'hi'}],
                'agentId': 'cora', 'max_tokens': 20,
            })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(dict(sim._CONTENT_HEARTBEATS), before)


if __name__ == '__main__':
    unittest.main()
